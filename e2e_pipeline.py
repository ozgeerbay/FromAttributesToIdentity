#!/usr/bin/env python3
"""
End-to-End Threat Intelligence Pipeline
========================================
Listing → Parse → Extract Attributes → LLM Identification (Borda) → OSINT Stage

Usage:
  python e2e_pipeline.py --image 6.png
  python e2e_pipeline.py --url "https://x.com/DarkWebInformer/status/..."
  python e2e_pipeline.py --text listing.txt

  # Specify LLM model for candidate identification
  python e2e_pipeline.py --image 6.png --model "google/gemini-2.5-flash"

  # Skip OSINT stage (only parse + identify)
  python e2e_pipeline.py --image 6.png --no-osint

  # Use pre-parsed JSON directly (skip Stage 1)
  python e2e_pipeline.py --parsed parsed_output.json

Output:
  outputs/e2e/<stem>/
    ├── parsed.json           — raw parser output
    ├── attributes.json       — extracted attribute JSON (5 attrs + tech)
    ├── borda_results.json    — full Borda iteration results + ranked candidates
    └── candidate_list.json   — top 5 candidates for OSINT
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests
import yaml

# Fix Windows console encoding
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).parent

# ── ANSI Colors ────────────────────────────────────────────────────────────────
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
MAGENTA = "\033[95m"
BLUE = "\033[94m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"

# ── Configuration ──────────────────────────────────────────────────────────────
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Configuration defaults
DEFAULT_MODEL = "openai/gpt-4.1"

NUM_ITERATIONS = 8
BORDA_POINTS = [5, 4, 3, 2, 1]


def load_config() -> dict:
    cfg_path = SCRIPT_DIR / "config.yaml"
    if not cfg_path.exists():
        return {}
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def banner(text: str, char: str = "═", width: int = 70):
    print(f"\n{char * width}")
    print(f"  {BOLD}{CYAN}{text}{RESET}")
    print(f"{char * width}")


def sub_banner(text: str, char: str = "─", width: int = 60):
    print(f"\n{char * width}")
    print(f"  {text}")
    print(f"{char * width}")


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 1: Parse listing via post_parser.py
# ══════════════════════════════════════════════════════════════════════════════

def stage_parse(source_type: str, source_value: str, output_path: Path, provider: str = None) -> bool:
    """Run post_parser.py and save the parsed JSON."""
    banner("STAGE 1: Parse Listing")

    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "post_parser.py"),
        f"--{source_type}",
        source_value,
        "--output",
        str(output_path),
    ]
    if provider:
        cmd.extend(["--provider", provider])

    print(f"  [>] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"  {RED}[x] Parse failed with exit code {result.returncode}{RESET}")
        return False

    if not output_path.exists():
        print(f"  {RED}[x] Parsed output file not found: {output_path}{RESET}")
        return False

    print(f"  {GREEN}[OK] Parsed output saved to: {output_path}{RESET}")
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 2: Extract attribute JSON from parsed output
# ══════════════════════════════════════════════════════════════════════════════

def _deep_get(data: dict, dotpath: str, default=None):
    """Safely traverse nested dict by dot-separated path."""
    keys = dotpath.split(".")
    node = data
    for k in keys:
        if isinstance(node, dict):
            node = node.get(k, default)
        else:
            return default
    return node


def stage_extract_attributes(parsed_path: Path, attributes_path: Path) -> dict:
    """Extract the 5 standard attributes + technology_products from parsed JSON."""
    banner("STAGE 2: Extract Attributes")

    with open(parsed_path, encoding="utf-8") as f:
        parsed = json.load(f)

    # ── Country ──
    country = (
        _deep_get(parsed, "target.interpreted.country")
        or _deep_get(parsed, "search_payload.country")
    )

    # ── Sector ──
    sector = (
        _deep_get(parsed, "target.interpreted.sector")
        or _deep_get(parsed, "search_payload.sector")
    )
    # Flatten list to first element string if needed
    if isinstance(sector, list):
        sector = sector[0] if sector else None

    # ── Revenue ──
    rev_obj = _deep_get(parsed, "target.interpreted.company_size_indicators.estimated_revenue_usd", {})
    revenue_values = []
    if rev_obj and isinstance(rev_obj, dict):
        raw_rev = rev_obj.get("raw")
        val_rev = rev_obj.get("value")
        if raw_rev and raw_rev.lower() not in ("unknown", "n/a", "null", ""):
            entry = {"raw": raw_rev}
            if val_rev:
                entry["value_usd"] = val_rev
            year = rev_obj.get("year")
            if year:
                entry["year"] = year
            revenue_values.append(entry)
    # Also check search_payload.revenue_usd_approx
    rev_approx = _deep_get(parsed, "search_payload.revenue_usd_approx")
    if rev_approx and not revenue_values:
        revenue_values.append({"raw": str(rev_approx), "value_usd": rev_approx})

    # ── Employees ──
    employees = _deep_get(parsed, "search_payload.employee_count_approx")

    # ── Headquarters ──
    headquarters = _deep_get(parsed, "target.raw.location_text")

    # ── Technology Products (for OSINT stage only) ──
    tech_products = []
    # From technology_text
    tech_text = _deep_get(parsed, "target.raw.technology_text", "")
    if tech_text:
        for line in tech_text.replace(",", "\n").split("\n"):
            # Strip labels like "OS: " or "Device: "
            cleaned = re.sub(r"^[A-Za-z_]+:\s*", "", line.strip())
            if cleaned and cleaned not in tech_products:
                tech_products.append(cleaned)
    # From security_stack
    sec_stack = parsed.get("security_stack") or []
    for item in sec_stack:
        for field in ("raw", "vendor", "product"):
            val = item.get(field)
            if val and val not in tech_products:
                tech_products.append(val)

    # Build the attribute dict
    # The 5 standard attributes (sent to LLM for identification)
    attributes = {
        "country": country,
        "sector": sector,
        "revenue_values": revenue_values if revenue_values else [],
        "employees": employees,
        "headquarters": headquarters,
    }

    # Full attributes (includes tech for OSINT stage)
    full_attributes = dict(attributes)
    full_attributes["technology_products"] = tech_products

    # Save full attributes
    with open(attributes_path, "w", encoding="utf-8") as f:
        json.dump(full_attributes, f, indent=2, ensure_ascii=False)

    # Print summary
    print(f"  Country:      {GREEN}{country or 'N/A'}{RESET}")
    print(f"  Sector:       {GREEN}{sector or 'N/A'}{RESET}")
    print(f"  Revenue:      {GREEN}{revenue_values or 'N/A'}{RESET}")
    print(f"  Employees:    {GREEN}{employees or 'N/A'}{RESET}")
    print(f"  Headquarters: {GREEN}{headquarters or 'N/A'}{RESET}")
    print(f"  Tech Stack:   {YELLOW}{', '.join(tech_products) if tech_products else 'None'}{RESET}")
    print(f"\n  {GREEN}[OK] Attributes saved to: {attributes_path}{RESET}")

    return full_attributes


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 3: LLM Identification with Borda Count
# ══════════════════════════════════════════════════════════════════════════════

def build_identification_prompt(attributes: dict) -> str:
    """Build the prompt using only the 5 standard attributes (no tech)."""
    # Build a clean JSON with only the 5 standard attributes
    llm_attrs = {
        "country": attributes.get("country"),
        "sector": attributes.get("sector"),
        "revenue_values": attributes.get("revenue_values", []),
        "employees": attributes.get("employees"),
        "headquarters": attributes.get("headquarters"),
    }
    json_content = json.dumps(llm_attrs, indent=2, ensure_ascii=False)

    return (
        f"You are a company identification expert. Given the following company profile attributes, "
        f"guess which company they belong to.\n\n"
        f"Attributes:\n{json_content}\n\n"
        f"Based on these attributes (country, sector, revenue, employees, headquarters), "
        f"provide your best 5 guesses for the company name. Think carefully about the sector, "
        f"location, revenue size, and number of employees.\n\n"
        f"Respond with ONLY a numbered list:\n"
        f"1. [Most likely company]\n"
        f"2. [Second most likely]\n"
        f"3. [Third most likely]\n"
        f"4. [Fourth most likely]\n"
        f"5. [Fifth most likely]"
    )


def query_openrouter(prompt: str, api_key: str, model: str) -> str:
    """Send a prompt to OpenRouter and return the response text with retries."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "http://localhost:3000",
        "X-Title": "Thesis E2E Pipeline",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "provider": {"allow_fallbacks": False},
    }

    max_retries = 5
    backoff = 3.0

    for attempt in range(max_retries):
        try:
            resp = requests.post(
                OPENROUTER_URL, headers=headers, json=payload, timeout=60
            )
            if resp.status_code == 429:
                print(
                    f"    [RATE LIMIT] 429 hit. Waiting {backoff}s before retry {attempt+1}/{max_retries}..."
                )
                time.sleep(backoff)
                backoff *= 2
                continue

            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"].get("content")
            if content and content.strip():
                return content
            else:
                print(
                    f"    [WARN] Empty response. Retrying {attempt+1}/{max_retries} in {backoff}s..."
                )
                time.sleep(backoff)
                backoff *= 2

        except Exception as e:
            print(
                f"    [ERROR] OpenRouter request failed (Attempt {attempt+1}/{max_retries}): {e}. Retrying in {backoff}s..."
            )
            time.sleep(backoff)
            backoff *= 2

    print(f"  {RED}[x] All retries exhausted.{RESET}")
    return ""


def parse_company_list(response_text: str) -> list[str]:
    """Parse a numbered list of companies from the LLM response."""
    companies = []
    matches = re.findall(r"^\s*\d+[\.)\]]\s*(.+)", response_text, re.MULTILINE)
    for m in matches:
        name = m.strip().rstrip(".")
        # Remove trailing explanation after dash or parenthesis
        name = re.split(r"\s*[-–—(]", name)[0].strip()
        if name and len(companies) < 5:
            companies.append(name)

    # Fallback
    if len(companies) < 2:
        companies = []
        for line in response_text.strip().split("\n"):
            line = line.strip()
            if line and not line.lower().startswith(
                ("here", "based", "the", "note", "these")
            ):
                cleaned = re.sub(r"^[\d\.\)\-\*]+\s*", "", line).strip()
                if cleaned and len(companies) < 5:
                    companies.append(cleaned)

    return companies[:5]


def stage_identify(
    attributes: dict, api_key: str, model: str, output_dir: Path
) -> list[str]:
    """Run LLM identification with Borda count aggregation (8 iterations)."""
    banner(f"STAGE 3: LLM Identification (Borda Count)")
    print(f"  Model: {MAGENTA}{model}{RESET}")
    print(f"  Iterations: {NUM_ITERATIONS}")
    print(f"  Borda points: {BORDA_POINTS}")

    prompt = build_identification_prompt(attributes)
    borda_scores = defaultdict(int)
    all_iterations = []

    for iteration in range(1, NUM_ITERATIONS + 1):
        print(f"\n  [{iteration}/{NUM_ITERATIONS}] Querying {model}...", end=" ")
        response = query_openrouter(prompt, api_key, model)

        if not response:
            print(f"{RED}FAILED{RESET}")
            all_iterations.append({"iteration": iteration, "companies": [], "raw": ""})
            continue

        companies = parse_company_list(response)
        print(f"{GREEN}OK{RESET} → {companies[:3]}{'...' if len(companies) > 3 else ''}")

        for rank, company in enumerate(companies):
            if rank < len(BORDA_POINTS):
                borda_scores[company] += BORDA_POINTS[rank]

        all_iterations.append(
            {"iteration": iteration, "companies": companies, "raw": response}
        )

        # Small delay between iterations
        if iteration < NUM_ITERATIONS:
            time.sleep(1.0)

    # Sort by Borda score descending
    sorted_borda = sorted(borda_scores.items(), key=lambda x: -x[1])

    # Top 5 candidates
    top5 = [name for name, score in sorted_borda[:5]]

    # Print Borda results
    sub_banner(f"{BOLD}Borda Count Results{RESET}")
    for i, (name, score) in enumerate(sorted_borda[:5], 1):
        bar = "█" * (score // 2)
        print(f"    #{i}  {name:<40} {score:>3} pts  {CYAN}{bar}{RESET}")

    if len(sorted_borda) > 5:
        print(f"    ... and {len(sorted_borda) - 5} more candidates")

    # Save full results
    borda_results_path = output_dir / "borda_results.json"
    borda_payload = {
        "model": model,
        "num_iterations": NUM_ITERATIONS,
        "borda_points": BORDA_POINTS,
        "timestamp": datetime.now().isoformat(),
        "iterations": all_iterations,
        "borda_ranking": [{"name": n, "score": s} for n, s in sorted_borda],
        "top5_candidates": top5,
    }
    with open(borda_results_path, "w", encoding="utf-8") as f:
        json.dump(borda_payload, f, indent=2, ensure_ascii=False)

    # Save candidate list
    candidate_list_path = output_dir / "candidate_list.json"
    with open(candidate_list_path, "w", encoding="utf-8") as f:
        json.dump(top5, f, indent=2, ensure_ascii=False)

    print(f"\n  {GREEN}[OK] Borda results saved to: {borda_results_path}{RESET}")
    print(f"  {GREEN}[OK] Candidate list saved to: {candidate_list_path}{RESET}")

    return top5


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 4: OSINT Assessment via osint_stage.py
# ══════════════════════════════════════════════════════════════════════════════

def stage_osint(
    candidate_list_path: Path, parsed_path: Path, output_dir: Path
) -> bool:
    """Run osint_stage.py with the candidate list and victim profile."""
    banner("STAGE 4: OSINT Assessment")

    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "osint_stage.py"),
        "--candidate-list",
        str(candidate_list_path),
        "--victim-json",
        str(parsed_path),
    ]
    print(f"  [>] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"  {RED}[x] OSINT stage failed with exit code {result.returncode}{RESET}")
        return False

    print(f"\n  {GREEN}[OK] OSINT assessment complete.{RESET}")
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description="End-to-End Threat Intelligence Pipeline: Parse → Identify → OSINT",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Input group
    ig = ap.add_mutually_exclusive_group(required=True)
    ig.add_argument("--image", type=str, help="Image file of the listing")
    ig.add_argument("--url", type=str, help="Twitter/X tweet URL")
    ig.add_argument("--text", type=str, help="Text file of the listing")
    ig.add_argument(
        "--parsed", type=Path, help="Pre-parsed JSON file (skip Stage 1)"
    )

    # Options
    ap.add_argument(
        "--model",
        type=str,
        default=None,
        help=f"OpenRouter model name (default: {DEFAULT_MODEL})",
    )
    ap.add_argument(
        "--no-osint",
        action="store_true",
        help="Skip OSINT stage (only parse + identify)",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output directory (default: outputs/e2e/<stem>/)",
    )

    args = ap.parse_args()

    # Load config
    cfg = load_config()
    api_keys = cfg.get("api_keys", {})
    openrouter_key = (api_keys.get("openrouter", "") or "").strip()
    if not openrouter_key:
        openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()

    if not openrouter_key:
        print(
            f"{RED}[x] Missing OpenRouter API key in config.yaml (api_keys.openrouter){RESET}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Resolve parser provider
    parser_provider = cfg.get("provider")

    # Resolve model for Borda identification (Stage 3)
    model = args.model
    if not model:
        model = cfg.get("models", {}).get("openrouter") or cfg.get("models", {}).get(parser_provider)
        if model and "/" not in model:
            if "gemini" in model:
                model = f"google/{model}"
            elif "gpt" in model:
                model = f"openai/{model}"
            elif "claude" in model:
                model = f"anthropic/{model}"
        if not model:
            model = "google/gemini-2.5-flash-lite"

    # Determine stem and output dir
    if args.image:
        stem = Path(args.image).stem
        source_type, source_value = "image", args.image
    elif args.url:
        m = re.search(r"/status/(\d+)", args.url)
        stem = f"tweet_{m.group(1)}" if m else "tweet"
        source_type, source_value = "url", args.url
    elif args.text:
        stem = Path(args.text).stem
        source_type, source_value = "text", args.text
    elif args.parsed:
        stem = args.parsed.stem
        source_type, source_value = None, None

    output_dir = args.output or (SCRIPT_DIR / "outputs" / "e2e" / stem)
    output_dir.mkdir(parents=True, exist_ok=True)

    parsed_path = output_dir / "parsed.json"
    attributes_path = output_dir / "attributes.json"

    t0 = time.time()

    banner("END-TO-END THREAT INTELLIGENCE PIPELINE", "█", 70)
    print(f"  Stem:     {stem}")
    print(f"  Model:    {model}")
    print(f"  OSINT:    {'Enabled' if not args.no_osint else 'Disabled'}")
    print(f"  Output:   {output_dir}")

    # ── STAGE 1: Parse ─────────────────────────────────────────────────────────
    if args.parsed:
        # Skip parsing, use pre-parsed file
        print(f"\n  [~] Using pre-parsed JSON: {args.parsed}")
        # Copy to output dir if not already there
        if args.parsed.resolve() != parsed_path.resolve():
            import shutil
            shutil.copy2(args.parsed, parsed_path)
    else:
        if not stage_parse(source_type, source_value, parsed_path, provider=parser_provider):
            sys.exit(1)

    # ── STAGE 2: Extract Attributes ────────────────────────────────────────────
    attributes = stage_extract_attributes(parsed_path, attributes_path)

    # ── STAGE 3: LLM Identification ───────────────────────────────────────────
    top5_candidates = stage_identify(attributes, openrouter_key, model, output_dir)

    if not top5_candidates:
        print(f"\n  {RED}[x] No candidates identified. Pipeline stopped.{RESET}")
        sys.exit(1)

    # ── STAGE 4: OSINT Assessment ──────────────────────────────────────────────
    if not args.no_osint:
        candidate_list_path = output_dir / "candidate_list.json"
        stage_osint(candidate_list_path, parsed_path, output_dir)

    # ── Summary ────────────────────────────────────────────────────────────────
    elapsed = time.time() - t0
    m, s = divmod(int(elapsed), 60)

    banner("PIPELINE COMPLETE", "█", 70)
    print(f"  Total time:   {m}m {s}s")
    print(f"  Output dir:   {output_dir}")
    print(f"  Top 5 candidates:")
    for i, name in enumerate(top5_candidates, 1):
        print(f"    {BOLD}#{i}{RESET}  {GREEN}{name}{RESET}")
    print()


if __name__ == "__main__":
    main()
