#!/usr/bin/env python3
"""
End-to-end pipeline: parse → enrich in one command.

Single input:
    python pipeline.py --image 6.png
    python pipeline.py --url "https://x.com/DarkWebInformer/status/..."
    python pipeline.py --text listing.txt

Batch:
    python pipeline.py --batch examples/        # parse all images in folder
    python pipeline.py --batch examples/ --output results/

Output:
    parsing_results/<stem>.json — parsed output
    <stem>_enriched.json         — enriched slim output
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path


import sys

# Fix Windows console encoding
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).parent
SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def run(cmd: list, label: str) -> bool:
    print(f"\n{'-'*50}")
    print(f"[>] {label}")
    print(f"{'-'*50}")
    result = subprocess.run(cmd)
    return result.returncode == 0


def pipeline_single(source_arg: str, source_type: str, output_dir: Path | None, force: bool, debug_raw: bool) -> bool:
    """Parse one input, then enrich. Returns True on full success."""
    output_dir = output_dir or SCRIPT_DIR

    # Determine output stem
    if source_type == "url":
        import re
        m = re.search(r"/status/(\d+)", source_arg)
        stem = f"tweet_{m.group(1)}" if m else "tweet"
    elif source_type in ("image", "text"):
        stem = Path(source_arg).stem
    else:
        stem = "listing"

    output_dir.mkdir(parents=True, exist_ok=True)
    parsing_dir = output_dir / "parsing_results"
    parsing_dir.mkdir(parents=True, exist_ok=True)
    parsed_path = parsing_dir / f"{stem}.json"
    enriched_path = output_dir / f"{stem}_enriched.json"

    if enriched_path.exists() and not force:
        print(f"[~] Enriched output exists, skipping: {enriched_path.name}")
        return True

    # Step 1: Parse → output/parsing_results
    parse_cmd = [sys.executable, str(SCRIPT_DIR / "post_parser.py"),
                 f"--{source_type}", source_arg,
                 "--output", str(parsed_path)]
    if not run(parse_cmd, f"Step 1 — Parse ({source_type}: {source_arg})"):
        print(f"[x] Parse failed. Stopping.")
        return False

    if not parsed_path.exists():
        print(f"[x] Parsed file not found.")
        return False

    # Step 2: Enrich
    enrich_cmd = [sys.executable, str(SCRIPT_DIR / "company_enrich.py"),
                  "--json", str(parsed_path),
                  "--output", str(enriched_path)]
    if debug_raw:
        enrich_cmd.append("--debug-raw")
    if not run(enrich_cmd, f"Step 2 — Enrich"):
        print(f"[x] Enrich failed.")
        return False

    print(f"\n[OK] Pipeline complete:")
    print(f"     Parsed:   {parsed_path}")
    print(f"     Enriched: {enriched_path}")
    return True


def pipeline_batch(input_dir: Path, output_dir: Path, force: bool, debug_raw: bool):
    """Parse all images in folder, then enrich each."""
    output_dir.mkdir(parents=True, exist_ok=True)
    parsing_dir = output_dir / "parsing_results"
    parsing_dir.mkdir(parents=True, exist_ok=True)
    images = sorted([f for f in input_dir.iterdir() if f.suffix.lower() in SUPPORTED_IMAGES])

    if not images:
        print(f"[!] No images found in {input_dir}")
        sys.exit(0)

    print(f"[>] Batch pipeline: {len(images)} images in '{input_dir}' → '{output_dir}'\n")

    ok = failed = skipped_parse = skipped_enrich = 0

    for img in images:
        parsed_path = parsing_dir / f"{img.stem}.json"
        enriched_path = output_dir / f"{img.stem}_enriched.json"

        if enriched_path.exists() and not force:
            print(f"[~] Skip (exists): {enriched_path.name}")
            skipped_parse += 1
            continue

        parse_cmd = [sys.executable, str(SCRIPT_DIR / "post_parser.py"),
                     "--image", str(img),
                     "--output", str(parsed_path),
                     "--quiet"]
        print(f"[>] Parsing: {img.name}")
        r = subprocess.run(parse_cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"    [x] Parse failed: {img.name}")
            if r.stderr:
                print(f"        {r.stderr.strip()}")
            failed += 1
            continue

        # Enrich
        enrich_cmd = [sys.executable, str(SCRIPT_DIR / "company_enrich.py"),
                      "--json", str(parsed_path),
                      "--output", str(enriched_path)]
        if debug_raw:
            enrich_cmd.append("--debug-raw")
        print(f"[>] Enriching: {img.name}")
        r = subprocess.run(enrich_cmd)
        if r.returncode != 0:
            print(f"    [x] Enrich failed: {img.name}")
            failed += 1
            continue
        print(f"    [OK] {enriched_path.name}")
        ok += 1

    print(f"\n── Done ─────────────────────────────────────")
    print(f"  Completed: {ok}")
    print(f"  Skipped:   {skipped_parse}  (use --force to re-run)")
    print(f"  Failed:    {failed}")


def pipeline_url_list(url_file: Path, output_dir: Path, force: bool, debug_raw: bool):
    """Read URLs from a text file (one per line) and process each."""
    lines = [l.strip() for l in url_file.read_text(encoding="utf-8").splitlines()]
    urls = [l for l in lines if l and not l.startswith("#")]

    if not urls:
        print(f"[!] No URLs found in {url_file}")
        sys.exit(0)

    print(f"[>] Found {len(urls)} URLs in '{url_file.name}'\n")

    ok = failed = skipped = 0

    for i, url in enumerate(urls, 1):
        print(f"\n{'═'*55}")
        print(f"  [{i}/{len(urls)}] {url}")
        print(f"{'═'*55}")
        success = pipeline_single(url, "url", output_dir, force, debug_raw)
        if success:
            ok += 1
        else:
            failed += 1

    print(f"\n── Done ─────────────────────────────────────")
    print(f"  Completed: {ok}")
    print(f"  Failed:    {failed}")


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s" if m else f"{s}s"


def main():
    ap = argparse.ArgumentParser(description="Parse + Enrich pipeline (single or batch)")
    ig = ap.add_mutually_exclusive_group(required=True)
    ig.add_argument("--image",    type=str,  help="Single image file")
    ig.add_argument("--url",      type=str,  help="Single Twitter/X tweet URL")
    ig.add_argument("--url-list", type=Path, help="Text file with one Twitter/X URL per line")
    ig.add_argument("--text",     type=str,  help="Single text file")
    ig.add_argument("--batch",    type=Path, help="Folder of images (batch mode)")

    ap.add_argument("--output", type=Path, default=None, help="Output folder (default: current dir)")
    ap.add_argument("--force",  action="store_true",     help="Re-run even if outputs exist")
    ap.add_argument("--debug-raw", action="store_true", help="Pass --debug-raw to company_enrich.py")
    args = ap.parse_args()

    output_dir = args.output or SCRIPT_DIR
    t0 = time.time()

    if args.batch:
        if not args.batch.is_dir():
            print(f"[x] Folder not found: {args.batch}")
            sys.exit(1)
        pipeline_batch(args.batch, output_dir, args.force, args.debug_raw)
    elif args.url_list:
        if not args.url_list.is_file():
            print(f"[x] File not found: {args.url_list}")
            sys.exit(1)
        pipeline_url_list(args.url_list, output_dir, args.force, args.debug_raw)
    elif args.image:
        success = pipeline_single(args.image, "image", output_dir, args.force, args.debug_raw)
        print(f"\n  Total time: {_fmt(time.time() - t0)}")
        sys.exit(0 if success else 1)
    elif args.url:
        success = pipeline_single(args.url, "url", output_dir, args.force, args.debug_raw)
        print(f"\n  Total time: {_fmt(time.time() - t0)}")
        sys.exit(0 if success else 1)
    elif args.text:
        success = pipeline_single(args.text, "text", output_dir, args.force, args.debug_raw)
        print(f"\n  Total time: {_fmt(time.time() - t0)}")
        sys.exit(0 if success else 1)

    print(f"\n  Total time: {_fmt(time.time() - t0)}")


if __name__ == "__main__":
    main()
