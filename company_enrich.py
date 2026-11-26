#!/usr/bin/env python3
"""
Company Enrichment
==================
Enriches company info via web search and model query. Returns: country, sector, revenue.

All settings from config.yaml (no manual API key input).
Web search backends: gemini | ollama AND searxng
  - gemini: api_keys.gemini (Gemini + google_search tool)
  - ollama: api_keys.ollama or OLLAMA_API_KEY (Ollama cloud web search, free tier)
  - searxng: self-hosted meta search (searxng_base_url in config)

Input modes:
  1. post_parser JSON (--json) — uses search_payload + target
  2. Raw text (--text / --stdin) — extracts clues via LLM, then searches

Usage:
    python company_enrich.py --json 23.json
    python company_enrich.py --text news.txt
    python company_enrich.py --json 2.json --search searxng
"""

import json
import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone

SCRIPT_DIR = Path(__file__).parent

# Lazy import for optional gemini (avoids load when using ollama/anthropic only)
_genai_module = None


def _get_genai():
    """Import google.genai once, cache. Used by call_llm, web_search_gemini."""
    global _genai_module
    if _genai_module is None:
        from google import genai
        from google.genai import types
        _genai_module = (genai, types)
    return _genai_module


def load_config() -> dict:
    import yaml
    cfg_path = SCRIPT_DIR / "config.yaml"
    if not cfg_path.exists():
        return {}
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def call_llm(
    provider: str,
    model: str,
    api_key: str,
    prompt: str,
    max_tokens: int = 512,
    temperature: float | None = None,
) -> str:
    """Call LLM (text-only) using configured provider.

    Pass temperature=0 for extraction / structured output to reduce run-to-run variance.
    When temperature is None, provider default is used.
    """
    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if temperature is not None:
            kwargs["temperature"] = temperature
        r = client.messages.create(**kwargs)
        return r.content[0].text
    if provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if temperature is not None:
            kwargs["temperature"] = temperature
        r = client.chat.completions.create(**kwargs)
        return r.choices[0].message.content
    if provider == "openrouter":
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if temperature is not None:
            kwargs["temperature"] = temperature
        r = client.chat.completions.create(**kwargs)
        return r.choices[0].message.content
    if provider == "groq":
        from groq import Groq
        client = Groq(api_key=api_key)
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if temperature is not None:
            kwargs["temperature"] = temperature
        r = client.chat.completions.create(**kwargs)
        return r.choices[0].message.content
    if provider == "gemini":
        genai, types = _get_genai()
        client = genai.Client(api_key=api_key)
        cfg_kw: dict = {"max_output_tokens": max_tokens}
        if temperature is not None:
            cfg_kw["temperature"] = temperature
        r = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(**cfg_kw),
        )
        return r.text
    if provider == "ollama":
        import ollama
        kw = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        if temperature is not None:
            kw["options"] = {"temperature": temperature}
        r = ollama.chat(**kw)
        return (r.get("message", {}).get("content") or "").strip()
    raise ValueError(f"Unknown provider: {provider}")


def web_search_gemini(query: str, api_key: str, model: str, financial_focus: bool = False, debug_raw: bool = False) -> tuple[str, list[str]]:
    """
    Gemini with google_search tool. Returns (text, list of source URLs from grounding).
    """
    genai, types = _get_genai()
    client = genai.Client(api_key=api_key)
    if financial_focus:
        prompt = f"""Search for: {query}

Be EXHAUSTIVE. Do NOT summarize or truncate. Include:
- Every revenue figure (annual, quarterly, with currency and year)
- Net profit, operating income, EBITDA
- All growth rates (YoY, QoQ)
- Operational metrics (employees, subscribers, market share)
- List every number explicitly. Format like: "Q4 2025 revenue: R$ X billion"
- Include sources/URLs."""
    else:
        prompt = f"Search and return real data: {query}. Include numbers, dates, URLs, key facts. Be comprehensive."
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(
        tools=[grounding_tool],
        max_output_tokens=4096,
    )
    r = client.models.generate_content(model=model, contents=prompt, config=config)
    if debug_raw:
        print("\n--- [DEBUG] Gemini web_search RAW RESPONSE ---", file=sys.stderr)
        print(f"Query: {query[:80]}...", file=sys.stderr)
        print(f"Response text ({len(r.text or '')} chars):\n{r.text}", file=sys.stderr)
        try:
            for i, c in enumerate(getattr(r, "candidates", []) or []):
                gm = getattr(c, "grounding_metadata", None) or getattr(c, "groundingMetadata", None)
                if gm:
                    print(f"Candidate {i} grounding_metadata: {gm}", file=sys.stderr)
        except Exception as e:
            print(f"grounding_metadata: {e}", file=sys.stderr)
        print("--- [DEBUG] END ---\n", file=sys.stderr)
    text = (r.text or "").strip()
    urls = []
    try:
        for c in getattr(r, "candidates", []) or []:
            gm = getattr(c, "grounding_metadata", None) or getattr(c, "groundingMetadata", None)
            if not gm:
                continue
            chunks = getattr(gm, "grounding_chunks", None) or getattr(gm, "groundingChunks", None) or []
            for ch in chunks:
                web = getattr(ch, "web", None) or (ch.get("web") if isinstance(ch, dict) else None)
                if web:
                    uri = getattr(web, "uri", None) or (web.get("uri") if isinstance(web, dict) else None)
                    if uri and uri.startswith("http"):
                        urls.append(uri)
    except Exception:
        pass
    return text, urls




def gemini_enrich_direct(company: str, api_key: str, model: str, sectors_list: list[str] | None = None, debug_raw: bool = False) -> str:
    """
    Gemini model direct call (no web search tool). Returns raw text response.
    Gemini answers from its own knowledge about the company.
    """
    genai, types = _get_genai()
    client = genai.Client(api_key=api_key)
    if sectors_list:
        sectors_str = ", ".join(f'"{s}"' for s in sectors_list[:400])
        prompt = f"""For company "{company}", enrich these 3 fields.

SECTOR: Pick exactly ONE from this list (no variation, no new sectors):
{sectors_str}

COUNTRY: Country name or null if unknown.
REVENUE: Annual revenue (e.g. "$5M") or null if unknown.

Return ONLY valid JSON, no markdown, no explanation:
{{"sector": "<from list above>", "country": "<or null>", "revenue": "<or null>", "description": "<short company description>"}}"""
    else:
        prompt = f"Enrich these 3 fields about {company}: sector, country, revenue. Return ONLY valid JSON. Use null for unknown."
    r = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(max_output_tokens=1024, temperature=0.0),
    )
    raw = (r.text or "").strip()
    if debug_raw:
        print("\n--- [DEBUG] Gemini model direct RAW ---", file=sys.stderr)
        print(f"Response: {raw}", file=sys.stderr)
        print("--- [DEBUG] END ---\n", file=sys.stderr)
    return raw


def _load_sectors_list(path: Path | None = None) -> list[str]:
    """Load sectors from Zoominfo-style JSON. Returns flattened list of all sector names."""
    p = path or SCRIPT_DIR / "sectors.json"
    if not p.exists():
        return []
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    sectors = []
    for parent, children in (data or {}).items():
        if parent and parent not in sectors:
            sectors.append(parent)
        for c in (children if isinstance(children, list) else []):
            if c and c not in sectors:
                sectors.append(c)
    return sorted(sectors)


def web_search_ollama(query: str, api_key: str | None, max_results: int = 8) -> str:
    """Ollama cloud web search (free tier). Set OLLAMA_API_KEY or api_keys.ollama in config."""
    import ollama
    try:
        resp = ollama.web_search(query, max_results=max_results)
    except Exception as e:
        raise RuntimeError(f"Ollama web search failed (set OLLAMA_API_KEY or api_keys.ollama): {e}")
    results = []
    if hasattr(resp, "results"):
        results = list(resp.results) if resp.results else []
    elif isinstance(resp, dict) and "results" in resp:
        results = resp["results"]
    elif isinstance(resp, list):
        results = resp
    parts = []
    for r in results:
        row = r if isinstance(r, dict) else {}
        title = row.get("title", row.get("name", ""))
        url = row.get("url", row.get("link", ""))
        content = row.get("content", row.get("snippet", row.get("body", "")))
        parts.append(f"Title: {title}\nURL: {url}\n{content}")
    return "\n\n".join(parts) if parts else ""



def web_search_searxng(query: str, base_url: str, max_results: int = 8) -> tuple[str, list[dict]]:
    """
    SearXNG meta search via JSON API.
    Set searxng_base_url in config or SEARXNG_BASE_URL env var.
    """
    import urllib.request
    import urllib.parse
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("Missing SearXNG base URL")
    search_url = base if base.endswith("/search") else f"{base}/search"
    params = {"q": query, "format": "json"}
    req_url = f"{search_url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        req_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; CompanyEnrich/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    results = data.get("results") or []
    parts = []
    raw_results = []
    for i, r in enumerate(results[:max_results], 1):
        title = r.get("title", "")
        url_val = r.get("url", "")
        body = r.get("content") or r.get("snippet") or r.get("description") or r.get("body") or ""
        block = [f"[Result {i}]", f"Title: {title}", f"URL: {url_val}", f"Description: {body}"]
        parts.append("\n".join(block))
        raw_results.append({"title": title, "url": url_val, "description": body})
    formatted = "\n\n".join(parts) if parts else ""
    return formatted, raw_results


def _get_path(obj: dict, path: str):
    """Get nested value: 'target.interpreted.country' -> obj['target']['interpreted']['country']."""
    for key in path.split("."):
        obj = (obj or {}).get(key)
        if obj is None:
            return None
    return obj


def extract_listing_data(parsed: dict, field_config: dict) -> dict:
    """
    Extract enrichment-ready data from parsed listing. Config-driven.
    Returns {field: value} for fields where listing has data. Listing wins over research.
    """
    out = {}
    for field, cfg in (field_config or {}).items():
        paths = cfg.get("paths") or []
        trusted = cfg.get("trusted_sources")
        for path in paths:
            val = _get_path(parsed, path)
            if val is None or val == "" or (isinstance(val, list) and not val):
                continue
            # For revenue_usd, check source when trusted_sources is set
            if trusted and field == "revenue_usd":
                rev_obj = _get_path(parsed, "target.interpreted.company_size_indicators.estimated_revenue_usd")
                src = (rev_obj or {}).get("source") if isinstance(rev_obj, dict) else None
                raw_text = _get_path(parsed, "target.raw.revenue_text")
                if src not in trusted and not raw_text:
                    continue
            elif trusted and field == "revenue_raw":
                rev_obj = _get_path(parsed, "target.interpreted.company_size_indicators.estimated_revenue_usd")
                src = (rev_obj or {}).get("source") if isinstance(rev_obj, dict) else None
                raw_text = _get_path(parsed, "target.raw.revenue_text")
                if src not in trusted and not raw_text:
                    continue
            out[field] = val
            break
    return out


def build_slim_output(parsed: dict | None, enriched_block: dict) -> dict:
    raw_vals = enriched_block.get("revenue_values") or []
    cleaned_vals = []
    for val in raw_vals:
        if not isinstance(val, dict):
            continue
        cleaned_val = {}
        # Keep raw
        if "raw" in val and val["raw"] is not None:
            cleaned_val["raw"] = val["raw"]
        elif "raw_normalized" in val and val["raw_normalized"] is not None:
            cleaned_val["raw"] = val["raw_normalized"]
        
        # Keep year (only if not null)
        if "year" in val and val["year"] is not None:
            cleaned_val["year"] = val["year"]
            
        cleaned_vals.append(cleaned_val)

    out = {
        "country": enriched_block.get("country"),
        "sector": enriched_block.get("sector"),
        "revenue_values": cleaned_vals,
        "employees": enriched_block.get("employees"),
        "headquarters": enriched_block.get("headquarters"),
    }
    return out


# Enriched block: required fields always present. Order fixed.
ENRICHED_FIELDS_ORDER = [
    "country", "sector", "revenue_usd", "revenue_annual_original", "revenue_year", "revenue_source",
    "revenue_selection_reasoning", "revenue_entity_ambiguous", "revenue_values", "employees", "headquarters",
    "search_sources", "companies",
]


def build_enriched_block(data: dict, listing_data: dict, country_h: str, sector_h: str) -> dict:
    """
    Canonical enriched block. Required: country, sector, revenue.
    Optional (when available): employees, headquarters.
    All fields always present, order fixed.
    """
    out = dict(data or {})
    out = apply_listing_overrides(out, listing_data)
    out.setdefault("country", (country_h or "").strip() or None)
    out.setdefault("sector", (sector_h or "").strip() or None)
    out.setdefault("revenue_usd", None)
    out.setdefault("revenue_annual_original", None)
    out.setdefault("revenue_year", None)
    out.setdefault("revenue_source", None)
    out.setdefault("revenue_selection_reasoning", None)
    out.setdefault("revenue_entity_ambiguous", None)
    out.setdefault("revenue_values", [])
    out.setdefault("employees", None)
    out.setdefault("headquarters", None)
    out.setdefault("search_sources", [])
    out.setdefault("companies", None)
    if not out.get("headquarters"):
        out["headquarters"] = f"Unknown, {out['country']}" if out.get("country") else "Unknown"
    ordered = {k: out[k] for k in ENRICHED_FIELDS_ORDER if k in out}
    extra = {k: v for k, v in out.items() if k not in ENRICHED_FIELDS_ORDER}
    return {**ordered, **extra}


def ensure_required_fields(enriched: dict, country_h: str, sector_h: str) -> dict:
    """Fill required fields (when build_enriched_block not used)."""
    out = dict(enriched or {})
    out.setdefault("country", (country_h or "").strip() or None)
    out.setdefault("sector", (sector_h or "").strip() or None)
    out.setdefault("employees", None)
    out.setdefault("headquarters", None)
    if not out.get("headquarters"):
        out["headquarters"] = f"Unknown, {out['country']}" if out.get("country") else "Unknown"
    return out


def apply_listing_overrides(enriched: dict, listing_data: dict) -> dict:
    """
    Apply listing data over research. Listing wins when present.
    Handles country, sector, revenue, employees, headquarters.
    """
    out = dict(enriched or {})
    ld = listing_data or {}
    if ld.get("country"):
        out["country"] = ld["country"]
    if ld.get("sector"):
        s = ld["sector"]
        out["sector"] = ", ".join(s) if isinstance(s, list) else s
    if ld.get("employees") is not None:
        emp = ld["employees"]
        out["employees"] = emp[0] if isinstance(emp, list) and emp else emp
    if ld.get("headquarters"):
        out["headquarters"] = ld["headquarters"]
    if ld.get("revenue_usd") is not None:
        rev_usd = float(ld["revenue_usd"])
        rev_raw = ld.get("revenue_raw") or f"${rev_usd:,.0f}"
        out["revenue_usd"] = rev_usd
        out["revenue_annual_original"] = rev_raw
        out["revenue_year"] = None
        out["revenue_source"] = "listing"
        out["revenue_selection_reasoning"] = "From listing. Research not used for this field."
        entry = {
            "value_usd": rev_usd,
            "currency": "USD",
            "year": None,
            "raw": rev_raw,
            "source": "listing",
            "line_type": "operating_revenue",
            "entity_match": "target",
        }
        existing = out.get("revenue_values") or []
        out["revenue_values"] = [entry] + [v for v in existing if v.get("source") != "listing"][:14]
    return out


def clues_from_parsed(parsed: dict, cfg: dict | None = None) -> dict:
    """Build clues from post_parser output."""
    payload = parsed.get("search_payload") or {}
    target = parsed.get("target") or {}
    company = target.get("company_name") or {}
    raw = target.get("raw") or {}
    interp = target.get("interpreted") or {}

    name = company.get("value") or payload.get("company_name_hint")
    mentioned = company.get("mentioned")
    if isinstance(mentioned, list) and len(mentioned) > 1:
        company_names = [str(m).strip() for m in mentioned if m]
    else:
        company_names = [name] if name else []

    sectors = payload.get("sector") or interp.get("sector") or []
    sector_h = ", ".join(sectors) if isinstance(sectors, list) else str(sectors)

    revenue_val = payload.get("revenue_usd_approx")
    rev_obj = (interp.get("company_size_indicators") or {}).get("estimated_revenue_usd") or {}
    revenue_val = revenue_val or rev_obj.get("value")
    revenue_h = raw.get("revenue_text") or (f"~${revenue_val:,.0f}" if revenue_val else "")

    field_config = (cfg or {}).get("enrichment_fields") or {}
    listing_data = extract_listing_data(parsed, field_config)

    return {
        "company_name": name or None,
        "company_names": company_names,
        "website": payload.get("website") or raw.get("website") or None,
        "country_hint": payload.get("country") or interp.get("country") or raw.get("location_text"),
        "sector_hint": sector_h or None,
        "revenue_hint": revenue_h or None,
        "listing_data": listing_data,
        "other_clues": payload.get("raw_description") or company.get("raw") or raw.get("misc"),
    }


def extract_clues(text: str, provider: str, model: str, api_key: str) -> dict:
    """Extract company clues from raw text via LLM."""
    prompt = f"""This text is a cyber attack / data breach news article. Extract ALL clues about the company.

TEXT:
---
{text[:4000]}
---

Extract (use null if not found):
- company_name: Explicitly mentioned company name
- website: Company website (domain)
- country_hint: Country hint (e.g. "French", "Turkish", "US-based")
- sector_hint: Sector hint (e.g. insurance, healthcare, retail)
- revenue_hint: Revenue hint (e.g. "250M", "millions")
- other_clues: Other identifying information

Return ONLY JSON, no explanation:
{{"company_name": "...", "website": "...", "country_hint": "...", "sector_hint": "...", "revenue_hint": "...", "other_clues": "..."}}
"""
    try:
        raw = call_llm(provider, model, api_key, prompt)
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw[start:end+1])
    except Exception as e:
        print(f"[!] Extract failed: {e}", file=sys.stderr)
    return {}


def _is_truncated_revenue_dollar_snippet(match_text: str) -> bool:
    """
    Snippets like "Revenue: $83", "Revenue: $84.7", "Revenue: $431" with no M/B/million/billion
    are usually truncated SERP cards; do NOT treat as full USD magnitude.
    """
    import re
    t = (match_text or "").strip()
    if not t or re.search(r"(?i)(million|billion|thousand|\bm\b|\bb\b|\bk\b)", t):
        return False
    num = r"[\d,.]+"
    return bool(
        re.match(rf"(?i)^revenue\s*:?\s*\${num}\s*$", t)
        or re.match(rf"(?i)^total\s+revenue\s*:?\s*\${num}\s*$", t)
        or re.match(rf"(?i)^annual\s+revenue\s*:?\s*\${num}\s*$", t)
    )


def _raw_has_explicit_revenue_scale(raw: str | None) -> bool:
    """
    True if raw text states magnitude (million/billion/M/B) or full unambiguous amount ($70,000,000).
    False for bare 'Revenue: $83' (handled separately via truncation) or ambiguous short amounts.
    """
    import re
    if not raw:
        return False
    t = str(raw).strip()
    if _is_truncated_revenue_dollar_snippet(t):
        return False
    if re.search(r"(?i)(million|billion|thousand|\bm\b|\bb\b|\bk\b|mn\b|bn\b)", t):
        return True
    # Full amounts with grouped thousands — magnitude is explicit
    if re.search(r"[\$€£]\s*\d{1,3}(?:,\d{3})+(?:\.\d+)?\b", t):
        return True
    return False


def _looks_like_employee_size_band_raw(raw: str | None) -> bool:
    """LinkedIn-style '€5,000,000+' company size without revenue wording — not operating revenue."""
    import re
    if not raw:
        return False
    t = str(raw).strip()
    if re.search(r"(?i)(revenue|annual\s+sales|turnover|sales\s*\()", t):
        return False
    if re.search(r"(?i)[\$€£][\d,.+]+\s*\+", t):
        return True
    if re.search(r"(?i)company\s+size|employees?\s*at\s*this\s+range", t):
        return True
    return False


def _normalize_revenue_row_fields(v: dict | None) -> None:
    """Normalize LLM/regex optional fields in-place."""
    if not v:
        return
    if v.get("line_type") is not None:
        v["line_type"] = str(v["line_type"]).lower().strip()
    if v.get("entity_match") is not None:
        v["entity_match"] = str(v["entity_match"]).lower().strip()


def _any_row_attributed_to_target(entries: list[dict] | None) -> bool:
    for v in entries or []:
        if (v.get("entity_match") or "").lower().strip() == "target":
            return True
    return False


# line_type values that must never win scalar primary (LLM-labeled non-operating / unknown)
_REVENUE_LINE_TYPE_EXCLUDE_PRIMARY = frozenset({
    "m_and_a",
    "ma",
    "acquisition",
    "funding_valuation",
    "funding",
    "valuation",
    "employee_or_size_band",
    "employee_band",
    "company_size_band",
    "threshold_only",
    "other_financial",
    "unclear",
})


def _is_primary_revenue_candidate(v: dict, *, has_target_attributed_row: bool) -> bool:
    """
    Row may win scalar primary: not garbage, operating revenue, correct entity policy, explicit scale in raw.
    """
    if v.get("value_usd") is None or _is_garbage_revenue_row(v):
        return False
    lt = (v.get("line_type") or "").lower().strip()
    if lt in _REVENUE_LINE_TYPE_EXCLUDE_PRIMARY:
        return False
    em = (v.get("entity_match") or "").lower().strip()
    if em == "other_entity":
        return False
    if em == "target":
        pass
    elif has_target_attributed_row:
        # When LLM attributed at least one row to target, ignore unclear/other-source rows for primary
        return False
    else:
        # Regex-only or legacy: no target rows — allow unclear/missing entity only with structural scale
        pass
    raw = v.get("raw") or ""
    if _looks_like_employee_size_band_raw(raw):
        return False
    if not _raw_has_explicit_revenue_scale(raw):
        return False
    return True


def _revenue_usd_spread_contradictory(usds: list[float], ratio: float) -> bool:
    """True if max/min among positive values exceeds ratio (multiple orders of magnitude conflict)."""
    pos = [float(u) for u in usds if u is not None and float(u) > 0]
    if len(pos) < 2:
        return False
    lo, hi = min(pos), max(pos)
    if lo <= 0:
        return False
    return (hi / lo) >= ratio


def _revenue_context_rejected(text: str, start: int, end: int) -> bool:
    """
    Return True if this match should be rejected based on context.
    - Acquisition price (not revenue)
    - Lower bound: "revenue over X", "above X", "exceeds X" (threshold, not actual figure)
    """
    import re
    ctx_before = text[max(0, start - 80) : start].lower()
    ctx_after = text[end : min(len(text), end + 80)].lower()
    ctx = ctx_before + " " + ctx_after
    # Acquisition / purchase / M&A — not operating revenue
    if re.search(
        r"acquisition\s+(?:price|cost|deal|for)|acquired\s+(?:for|at)|purchase\s+(?:price|for)|"
        r"sold\s+for|bought\s+(?:for|at)|buyout|deal\s+(?:worth|valued|at)|"
        r"paid\s+\$|paid\s+usd|merger\s+(?:for|at)|takeover\s+(?:for|at)",
        ctx,
    ):
        return True
    # Lower bound — threshold, not actual revenue ("revenue over 100M" = min, not figure)
    if re.search(r"revenue\s+(?:over|above|exceeds?|more\s+than|in\s+excess\s+of|at\s+least)\s*$", ctx_before):
        return True
    if re.search(r"(?:over|above|exceeds?|>\s*)\s*[\$€£]?\s*[\d,.]+", ctx_before):
        return True
    return False


def extract_revenue_patterns(text: str) -> list[dict]:
    """
    Regex fallback ONLY: explicit scale/currency in the matched text (million/billion/M/B, TL, €…).
    No inferring billion vs million from bare numbers — that is left to the extraction LLM.
    Excludes: acquisition/M&A context, lower bounds ("revenue over X").
    """
    import re
    results = []
    seen = set()

    # Patterns with explicit unit: (regex, currency, rate). Group 1=number, Group 2=unit
    patterns_with_unit = [
        (r"\$\s*([\d,.]+)\s*(billion|B|million|M)\b", "USD", 1.0),
        (r"([\d,.]+)\s*(billion|B)\s*(?:USD|dollars)\b", "USD", 1.0),
        (r"([\d,.]+)\s*(million|M)\s*(?:USD|dollars)\b", "USD", 1.0),
        (r"revenue\s+(?:of\s+)?[\$R]?\s*([\d,.]+)\s*(billion|b|million|m)\b", "USD", 1.0),
        (r"Revenue\s+R\$\s*([\d,.]+)\s*b\b", "BRL", 0.2),
        (r"Revenue\s+[\$R]?\s*([\d,.]+)\s*(billion|b|million|m)\b", "USD", 1.0),
        (r"R\$\s*([\d,.]+)\s*(billion|b|million|m)\b", "BRL", 0.2),
        (r"R\$\s*([\d,.]+)\s*b\b", "BRL", 0.2),
        (r"([\d,.]+)\s*(billion|b)\s*(reais|BRL)\b", "BRL", 0.2),
        (r"([\d,.]+)\s*(million|m)\s*(reais|BRL)\b", "BRL", 0.2),
        (r"([\d,.]+)\s*(billion|b)\s+reais\b", "BRL", 0.2),
        (r"BRL\s+([\d,.]+)\s*(billion|b)\b", "BRL", 0.2),
        (r"revenue\s*(?:of|:)?\s*[\$R]?\s*([\d,.]+)\s*(billion|b|million|m)\b", "USD", 1.0),
        (r"Revenue[:\s]+[\$R]?\s*([\d,.]+)\s*(billion|b|million|m)\b", "USD", 1.0),
        (r"total\s+revenue[:\s]+[\$R]?\s*([\d,.]+)\s*(billion|b|million|m)\b", "USD", 1.0),
        (r"annual\s+revenue[:\s]+[\$R]?\s*([\d,.]+)\s*(billion|b|million|m)\b", "USD", 1.0),
        (r"€\s*([\d,.]+)\s*(billion|b|million|m)\b", "EUR", 1.08),
        (r"£\s*([\d,.]+)\s*(billion|b|million|m)\b", "GBP", 1.27),
    ]

    for pat, curr, rate in patterns_with_unit:
        for m in re.finditer(pat, text, re.IGNORECASE):
            try:
                num_str = m.group(1).replace(",", "")
                num = float(num_str)
                u = (m.group(2) or "billion").lower()
                if u in ("million", "m"):
                    num *= 1e6
                else:
                    num *= 1e9
                val_usd = round(num * rate, 0) if rate else round(num, 0)
                key = (round(num, -6), curr)
                if key in seen:
                    continue
                if _revenue_context_rejected(text, m.start(), m.end()):
                    continue
                seen.add(key)
                year = None
                ctx = text[max(0, m.start() - 20) : m.end() + 80]
                ym = re.search(r"20[12]\d", ctx)
                if ym:
                    year = ym.group(0)
                results.append({
                    "value": round(num, 0),
                    "value_usd": val_usd,
                    "currency": curr,
                    "unit": "billion" if num >= 1e9 else "million",
                    "year": year,
                    "raw": m.group(0).strip()[:100],
                })
            except (ValueError, IndexError):
                continue

    return results


def _normalize_revenue_raw(raw: str, value_usd: float) -> str:
    """Normalize raw display for huge values (e.g. $112097.4 Million -> $112.1B)."""
    if not raw or value_usd is None:
        return raw or ""
    if value_usd >= 1e12:
        return f"${value_usd/1e12:.2f}T"
    if value_usd >= 1e9:
        return f"${value_usd/1e9:.2f}B"
    if value_usd >= 1e6:
        return f"${value_usd/1e6:.2f}M"
    if value_usd >= 1e3:
        return f"${value_usd/1e3:.2f}K"
    return raw


def _revenue_source_tier(source: str | None) -> int:
    """Lower = higher priority for primary revenue (matches extraction prompt intent)."""
    s = (source or "").lower()
    if "zoominfo" in s:
        return 0
    if "getlatka" in s or "latka" in s:
        return 1
    if s == "listing":
        return 2
    if "http://" in s or "https://" in s:
        return 3
    if s == "llm":
        return 4
    return 5


def _revenue_year_numeric(year) -> int:
    """Year for sorting: higher = newer; unknown -> -1 so they sort after dated figures when using -year."""
    if year is None:
        return -1
    if isinstance(year, int):
        return year
    st = str(year).strip()
    if len(st) >= 4 and st[:4].isdigit():
        return int(st[:4])
    return -1


def _revenue_entry_sort_key(v: dict) -> tuple:
    """Deterministic ordering: tier, then newest year, then highest USD, then stable string ties."""
    tier = _revenue_source_tier(v.get("source"))
    y = _revenue_year_numeric(v.get("year"))
    usd = float(v.get("value_usd") or 0)
    raw = str(v.get("raw") or v.get("raw_normalized") or "")
    src = str(v.get("source") or "")
    return (tier, -y, -usd, raw, src)


def _primary_revenue_source_label(best: dict) -> str:
    """Short label for top-level revenue_source from the winning row."""
    s = str(best.get("source") or "").lower()
    if "zoominfo" in s:
        return "zoominfo"
    if "getlatka" in s or "latka" in s:
        return "getlatka"
    if s == "listing":
        return "listing"
    if s == "llm":
        return "llm"
    return "search_regex"


def _is_garbage_revenue_row(v: dict) -> bool:
    """Drop truncated SERP junk so it cannot win primary (e.g. 'Revenue: $83' → fake $83B)."""
    import re
    raw = str(v.get("raw") or "").strip()
    if _is_truncated_revenue_dollar_snippet(raw):
        return True
    usd = v.get("value_usd")
    if usd is None:
        return False
    u = float(usd)
    # Huge USD but raw line never says million/billion — likely truncated
    if u >= 1e11 and len(raw) < 45 and not re.search(r"(?i)(billion|\bb\b|million|\bm\b|thousand|\bk\b)", raw):
        return True
    return False


def select_primary_revenue_entry(entries: list[dict] | None) -> dict | None:
    """
    Pick exactly one primary revenue from merged candidates (stable across runs).
    Uses source tier (ZoomInfo > GetLatka > listing > URL-tagged > llm > other),
    then newest year, then highest value_usd, then (raw, source) lexicographic tie-break.
    """
    vals = [
        v
        for v in (entries or [])
        if v.get("value_usd") is not None and not _is_garbage_revenue_row(v)
    ]
    if not vals:
        return None
    vals_sorted = sorted(vals, key=_revenue_entry_sort_key)
    return vals_sorted[0]


def _reorder_revenue_values_primary_first(all_vals: list[dict], primary: dict | None) -> list[dict]:
    """Put the matching primary row first (use object from list when possible); preserve order of the rest."""
    if not all_vals:
        return []
    if not primary:
        return list(all_vals)
    pk = _revenue_entry_sort_key(primary)
    rest: list[dict] = []
    first_row = None
    for v in all_vals:
        if first_row is None and _revenue_entry_sort_key(v) == pk:
            first_row = v
        else:
            rest.append(v)
    if first_row is None:
        return [primary] + list(all_vals)
    return [first_row] + rest


def apply_deterministic_primary_revenue(block: dict | None, cfg: dict | None = None) -> None:
    """
    Set revenue_usd / revenue_annual_original / revenue_source from merged revenue_values
    using entity + line_type + explicit-scale gates, then source tier / year / value tie-breaks.
    Skips scalar primary when revenue_entity_ambiguous or magnitudes contradict (config ratio).
    Listing-sourced revenue is left untouched when already set.
    """
    if not block:
        return
    cfg = cfg or {}
    try:
        ratio = float(cfg.get("revenue_contradiction_ratio", 40) or 40)
    except (TypeError, ValueError):
        ratio = 40.0
    ratio = max(ratio, 2.0)

    all_vals = list(block.get("revenue_values") or [])
    for v in all_vals:
        _normalize_revenue_row_fields(v)

    listing_lock = (block.get("revenue_source") == "listing") and block.get("revenue_usd") is not None
    if listing_lock:
        lst = [v for v in all_vals if str(v.get("source") or "").lower() == "listing"]
        other = [v for v in all_vals if str(v.get("source") or "").lower() != "listing"]
        block["revenue_values"] = lst + other if lst else all_vals
        print("  [Revenue] listing lock — keeping revenue_usd from listing")
        return

    amb = block.get("revenue_entity_ambiguous")
    ambiguous_entity = amb is True or (isinstance(amb, str) and str(amb).lower() in ("true", "yes", "1"))
    has_target = _any_row_attributed_to_target(all_vals)
    pool = [v for v in all_vals if _is_primary_revenue_candidate(v, has_target_attributed_row=has_target)]
    usds = [float(v["value_usd"]) for v in pool if v.get("value_usd") is not None]

    if ambiguous_entity and pool:
        block["revenue_usd"] = None
        block["revenue_annual_original"] = None
        block["revenue_year"] = None
        block["revenue_source"] = None
        block["revenue_selection_reasoning"] = (
            "Sources describe multiple distinct organizations with this name (revenue_entity_ambiguous). "
            "No single scalar revenue — inspect revenue_values (line_type, entity_match, raw, source)."
        )
        block["revenue_values"] = all_vals
        print("  [Revenue] no primary: entity ambiguous (homonyms / mixed profiles)")
        return

    if len(usds) >= 2 and _revenue_usd_spread_contradictory(usds, ratio):
        block["revenue_usd"] = None
        block["revenue_annual_original"] = None
        block["revenue_year"] = None
        block["revenue_source"] = None
        block["revenue_selection_reasoning"] = (
            f"Primary-eligible rows disagree by more than {ratio:.0f}x in USD (mixed entities or bad snippets). "
            "No single revenue_usd — inspect revenue_values and sources."
        )
        block["revenue_values"] = all_vals
        print("  [Revenue] no primary: contradictory magnitudes among eligible rows")
        return

    best = select_primary_revenue_entry(pool)
    if not best:
        block["revenue_usd"] = None
        block["revenue_annual_original"] = None
        block["revenue_year"] = None
        block["revenue_source"] = None
        block["revenue_selection_reasoning"] = (
            "No revenue row passed checks (target attribution, operating_revenue, explicit scale, not truncated). "
            "See revenue_values for raw snippets."
        )
        block["revenue_values"] = all_vals
        print("  [Revenue] no primary: no eligible row after entity/scale filters")
        return

    block["revenue_usd"] = best["value_usd"]
    block["revenue_annual_original"] = best.get("raw") or best.get("raw_normalized")
    block["revenue_year"] = best.get("year")
    block["revenue_source"] = _primary_revenue_source_label(best)
    block["revenue_selection_reasoning"] = (
        f"Primary: {_primary_revenue_source_label(best)} after entity_match=target (or regex-only), "
        f"line_type=operating_revenue, explicit scale in raw, no contradiction. "
        f"Picked: {best.get('raw') or best.get('raw_normalized')} (year={best.get('year')})."
    )
    block["revenue_values"] = _reorder_revenue_values_primary_first(all_vals, best)
    usd = best.get("value_usd")
    usd_s = f"${float(usd):,.0f}" if usd is not None else "?"
    print(
        f"  [Revenue] deterministic primary: {block['revenue_source']} "
        f"→ {block.get('revenue_annual_original')} ({usd_s})"
    )


def _is_revenue_query(source: str) -> bool:
    """Block source (e.g. 'SearXNG|X zoominfo employee size') — only revenue queries should feed revenue extraction."""
    q = source.split("|", 1)[-1].lower() if "|" in source else source.lower()
    return "revenue" in q


def _is_employee_query(source: str) -> bool:
    """Block from employee/company size query — for employee extraction."""
    q = source.split("|", 1)[-1].lower() if "|" in source else source.lower()
    return "employee" in q or "company size" in q


def extract_employees_from_blocks(research_text: str) -> int | None:
    """
    Extract employee count from employee-related blocks only.
    Patterns: "500 employees", "50-100 employees", "1,000+ staff", "employee count: 500"
    """
    import re
    blocks = research_text.split("\n\n---\n\n")
    candidates = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        first_nl = block.find("\n")
        header = block[:first_nl].strip() if first_nl > 0 else block
        source = header[1 : header.index("]")].strip() if header.startswith("[") and "]" in header else ""
        if not _is_employee_query(source):
            continue
        body = block[first_nl + 1 :].strip() if first_nl > 0 else block
        for pat in [
            r"(?:employee count|employees?|staff|workforce)[:\s]+([\d,]+)\s*[-–]?\s*([\d,]+)?",
            r"([\d,]+)\s*[-–]\s*([\d,]+)\s+employees",
            r"([\d,]+)\+?\s+employees",
            r"([\d,]+)\s+staff",
        ]:
            for m in re.finditer(pat, body, re.I):
                nums = [int(re.sub(r"\D", "", g) or 0) for g in m.groups() if g]
                if nums:
                    candidates.append(max(nums))
    return max(candidates) if candidates else None


def extract_revenue_from_blocks(research_text: str, company_search: str | None = None) -> list[dict]:
    """
    Extract revenue from research split by blocks. Each block has [Backend|Query] header.
    CRITICAL: Only process blocks from revenue-related queries (query contains "revenue").
    Employee size, industry, headquarters etc. must NOT contribute to revenue_values.
    """
    blocks = research_text.split("\n\n---\n\n")
    all_revs = []
    seen = set()
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        first_nl = block.find("\n")
        if first_nl < 0:
            body, source = block, "unknown"
        else:
            header = block[:first_nl].strip()
            body = block[first_nl + 1 :].strip()
            source = header[1 : header.index("]")].strip() if header.startswith("[") and "]" in header else "unknown"
        if not _is_revenue_query(source):
            continue
        revs = extract_revenue_patterns(body)
        for r in revs:
            key = (round(r.get("value_usd") or 0, -6), r.get("raw"))
            if key in seen:
                continue
            seen.add(key)
            out = {k: v for k, v in r.items()}
            out["source"] = source
            out.setdefault("line_type", "operating_revenue")
            out.setdefault("entity_match", "unclear")
            if company_search:
                out["company_search"] = company_search
            raw_norm = _normalize_revenue_raw(r.get("raw", ""), r.get("value_usd"))
            if raw_norm != r.get("raw"):
                out["raw_normalized"] = raw_norm
            all_revs.append(out)
    return all_revs


def _listing_has_revenue(listing_data: dict | None) -> bool:
    """
    True → skip dedicated revenue queries (ZoomInfo revenue, \"... revenue\", etc.).

    Only when listing_data already has a numeric revenue_usd from enrichment_fields paths.
    We intentionally do NOT use revenue_hint / payload approx / raw text alone: those are
    often vague while ZoomInfo/regional snippets add real figures (e.g. TL).
    """
    ld = listing_data or {}
    return ld.get("revenue_usd") is not None


def _run_search(search_target: str, country_h: str, sector_h: str, cfg: dict, model: str,
                backends: list, gemini_key: str | None,
                ollama_key: str | None, debug_raw: bool = False,
                skip_revenue_queries: bool = False, get_gemini_key_fn=None) -> tuple[str, list[str], list]:
    """Run web search for one company. Returns (research_text, source_urls, search_raw)."""
    # Add country_h only if not already in search_target (avoid "Vivo Brazil + Brazil" duplication)
    # Keep leading space when country context is appended.
    # Using .strip() here breaks queries like '"Pellenc"France site:zoominfo.com'.
    _ctx = f" {country_h}" if country_h and country_h.lower() not in (search_target or "").lower() else ""
    _qt = f'"{search_target}"'

    # --- Natural-language queries (Gemini / Ollama) ---
    # No "zoominfo"/"linkedin" keywords here — Gemini confuses them with the
    # companies themselves (e.g. returns ZoomInfo Inc. financials instead of
    # the target's ZoomInfo profile).  Site-specific look-ups belong in SearXNG.
    rev_q = f"{search_target}{_ctx} revenue".strip()
    hq_q = f"{search_target} headquarters location"
    emp_q = f"{search_target}{_ctx} number of employees".strip()
    country_q = f"{search_target} company country headquartered in"
    country_q2 = f"{search_target} country"
    sector_q = f"{search_target} company industry sector"
    sector_q2 = f"{search_target} industry"
    sector_q3 = f"{search_target} sector"

    # --- Quoted + site: queries (SearXNG — exact match for search engines) ---
    sx_rev_q = f"{_qt}{_ctx} revenue".strip()
    sx_hq_q = f"{_qt} headquarters location"
    sx_zi_rev = f"{_qt}{_ctx} site:zoominfo.com revenue".strip()
    sx_country_q = f"{_qt} company country headquartered in"
    sx_country_q2 = f"{_qt} country"
    sx_sector_q = f"{_qt} company industry sector"
    sx_sector_q2 = f"{_qt} industry"
    sx_sector_q3 = f"{_qt} sector"

    # -- Gemini/Ollama query list (natural language, no quotes/site:) --
    search_queries = [
        (search_target, False),
        (f"{search_target} company", False),
    ]
    if not country_h:
        search_queries += [
            (country_q, False),
            (country_q2, False),
        ]
    if not sector_h:
        search_queries += [
            (sector_q, False),
            (sector_q2, False),
            (sector_q3, False),
        ]
    if not skip_revenue_queries:
        search_queries += [
            (rev_q, True),
        ]
    search_queries += [
        (hq_q, False),
        (emp_q, False),
    ]

    # -- SearXNG query list (Minimal version + vital zoominfo revenue dork) --
    sx_queries = [
        (search_target, False),
        (f"{_qt} company", False),
    ]
    if not country_h:
        sx_queries += [
            (sx_country_q2, False),
            (sx_country_q, False),
        ]
    if not sector_h:
        sx_queries += [
            (sx_sector_q, False),
            (sx_sector_q2, False),
            (sx_sector_q3, False),
        ]
    if not skip_revenue_queries:
        sx_queries += [
            (sx_zi_rev, True),
            (sx_rev_q, True),
        ]
    sx_queries += [
        (sx_hq_q, False),
    ]
    from concurrent.futures import ThreadPoolExecutor, as_completed

    parts_lock_data: list[tuple[int, str]] = []  # (index, text) to preserve order
    urls: list[str] = []
    search_raw: list[dict] = []

    tasks = []

    # Build Gemini tasks
    if "gemini" in backends and gemini_key:
        for idx, (q, financial_focus) in enumerate(search_queries):
            ctx = []
            if not (financial_focus and country_h) and country_h:
                ctx.append(f"Country: {country_h}")
            if sector_h:
                ctx.append(f"Sector: {sector_h}")
            full_q = f"{q}. {' '.join(ctx)}." if ctx else q
            tasks.append(("gemini", idx, full_q, financial_focus))

    # Build SearXNG tasks (uses quoted sx_queries for exact match)
    import os as _os
    base_url = ""
    max_res = cfg.get("searxng_max_results", 15)
    if "searxng" in backends:
        base_url = (cfg.get("searxng_base_url", "") or _os.getenv("SEARXNG_BASE_URL", "")).strip()
        for idx, (q, _) in enumerate(sx_queries):
            tasks.append(("searxng", idx + len(search_queries), q, False))

    # Build Ollama tasks
    if "ollama" in backends and ollama_key:
        _os.environ.setdefault("OLLAMA_API_KEY", ollama_key)
        for idx, (q, financial_focus) in enumerate(search_queries):
            extra = [] if (financial_focus and country_h) else ([country_h] if country_h else [])
            if sector_h:
                extra.append(sector_h)
            full_q = f"{q} {' '.join(extra)}".strip() if extra else q
            tasks.append(("ollama", idx + 2 * len(search_queries), full_q, False))

    def _run_task(task):
        backend, idx, q, financial_focus = task
        try:
            if backend == "gemini":
                current_key = get_gemini_key_fn() if get_gemini_key_fn else gemini_key
                res, gu = web_search_gemini(q, current_key, model, financial_focus=financial_focus, debug_raw=debug_raw)
                import time
                time.sleep(5) # Delay after successful request
                return (backend, idx, q, res, gu, None)
            elif backend == "searxng":
                res, raw = web_search_searxng(q, base_url, max_results=max_res)
                import time
                time.sleep(3) # Delay to prevent IP bans from Google/DuckDuckGo
                return (backend, idx, q, res, [], raw)
            elif backend == "ollama":
                res = web_search_ollama(q, ollama_key, max_results=8)
                return (backend, idx, q, res, [], None)
        except Exception as exc:
            print(f"  [Search] {backend} FAILED for q={q!r}: {type(exc).__name__}: {exc}")
            
            import time
            if backend == "gemini":
                time.sleep(5)
            return (backend, idx, q, "", [], None)

    # Split tasks: Gemini/Ollama run in parallel, SearXNG runs sequentially
    # (local SearXNG chokes under concurrent requests)
    gemini_ollama_tasks = [t for t in tasks if t[0] != "searxng"]
    searxng_tasks = [t for t in tasks if t[0] == "searxng"]

    results_by_idx: dict[int, tuple] = {}

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {executor.submit(_run_task, t): t for t in gemini_ollama_tasks}
        for future in as_completed(futures):
            r = future.result()
            if r:
                backend, idx, q, res, gu, raw = r
                results_by_idx[idx] = (backend, q, res, gu, raw)

    for t in searxng_tasks:
        r = _run_task(t)
        if r:
            backend, idx, q, res, gu, raw = r
            results_by_idx[idx] = (backend, q, res, gu, raw)

    for idx in sorted(results_by_idx):
        backend, q, res, gu, raw = results_by_idx[idx]
        if res:
            label = "Gemini" if backend == "gemini" else ("SearXNG" if backend == "searxng" else "Ollama")
            parts_lock_data.append((idx, f"[{label}|{q}]\n{res}"))
            urls.extend(gu or [])
        # Always keep SearXNG query audit trail, even when it returned 0 results.
        # Otherwise "query not present in raw_json" is indistinguishable from "query never executed".
        if backend == "searxng":
            search_raw.append({"query": q, "results": raw or []})

    parts = [text for _, text in sorted(parts_lock_data)]
    return "\n\n---\n\n".join(parts) if parts else "", urls, search_raw


def _parse_json_from_llm(raw: str) -> dict | None:
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return None
    candidate = raw[start:end+1]
    # Try strict parse first
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # Fallback: json_repair for malformed LLM output
    try:
        from json_repair import repair_json
        repaired = repair_json(candidate)
        result = json.loads(repaired)
        return result if isinstance(result, dict) else None
    except Exception:
        return None


def search_and_enrich(clues: dict, cfg: dict, provider: str, model: str, api_key: str) -> dict:
    """Search web and extract country, sector, revenue."""
    name = clues.get("company_name") or ""
    company_names = clues.get("company_names") or []
    if not company_names and name:
        company_names = [name]
    if not company_names:
        company_names = [clues.get("other_clues") or "unknown"]

    website = clues.get("website") or ""
    country_h = clues.get("country_hint") or ""
    sector_h = clues.get("sector_hint") or ""
    revenue_h = clues.get("revenue_hint") or ""
    listing_data = clues.get("listing_data") or {}
    other = clues.get("other_clues") or ""

    api_keys = cfg.get("api_keys", {})
    gemini_key = (api_keys.get("gemini", "") or "").strip() or (api_key if provider == "gemini" else "")
    ollama_key = (api_keys.get("ollama", "") or "").strip() or None

    gemini_keys = [k.strip() for k in gemini_key.split(",") if k.strip()]
    import threading
    _gemini_idx_lock = threading.Lock()
    _gemini_idx = [0]
    def _get_gemini_key():
        if not gemini_keys: return ""
        with _gemini_idx_lock:
            k = gemini_keys[_gemini_idx[0] % len(gemini_keys)]
            _gemini_idx[0] += 1
            return k

    # Evaluate search_backend exactly as written in config.yaml
    search_backend = str(cfg.get("search_backend", "searxng,gemini")).strip().lower()
    if search_backend == "all":
        backends = ["searxng"]
        if gemini_key: backends.append("gemini")
        if ollama_key: backends.append("ollama")
    elif "," in search_backend:
        backends = [b.strip().lower() for b in search_backend.split(",") if b.strip()]
    else:
        backends = [search_backend]

    # Multi-company: search each separately for accurate per-company revenue
    if len(company_names) > 1:
        print(f"  [Search] {len(company_names)} companies: {', '.join(company_names)}... (each searched separately)")
        companies_results = []
        all_revs = []
        search_sources = []
        research_parts = []
        all_search_raw = []
        for c in company_names:
            c = str(c).strip()
            if not c:
                continue
            print(f"    -> {c}...")
            research, urls, sraw = _run_search(c, country_h, sector_h, cfg, model,
                                                backends, gemini_key, ollama_key,
                                                debug_raw=cfg.get("debug_raw", False),
                                                get_gemini_key_fn=_get_gemini_key)
            search_sources.extend(urls)
            if sraw:
                all_search_raw.append({"company": c, "queries": sraw})
            if research:
                research_parts.append(f"[Company: {c}]\n{research}")
            if research and len(research.strip()) > 50:
                llm_rev = None
                use_llm = not cfg.get("use_regex_only", False)
                if use_llm:
                    try:
                        pr = f"""Extract revenue for {c} from this research. STRICT: extract ONLY what is explicitly stated.
- Do NOT infer or hallucinate. If no revenue for {c} is in the text, return revenue_usd: null, revenue_values: [].
- Ignore revenue from parent, competitors, or other companies.
- EXCLUDE (NOT operating revenue): acquisition / buyout / merger / takeover price, "bought for", "sold for", "paid $X", funding rounds, valuation, deal size — even if a dollar amount is given.
- EXCLUDE: lower bounds like "revenue over $100M", "above X" — these are thresholds, not actual figures.
- If the text gives a dollar amount but does NOT clearly label it as sales/turnover/annual revenue for {c}, omit it or classify line_type accordingly.
- Only set value_usd when the snippet explicitly states the scale (million/billion/M/B) or unambiguous full amount; if ambiguous, use line_type "unclear" or omit value_usd.
- Every revenue_values row MUST include line_type and entity_match:
  line_type: "operating_revenue" | "m_and_a" | "funding_valuation" | "employee_or_size_band" | "threshold_only" | "other_financial" | "unclear"
  entity_match: "target" (figure is for {c} in the same snippet) | "other_entity" | "unclear"
  entity_name: string or null

Return JSON: {{"revenue_usd": number or null, "revenue_annual_original": "e.g. $X billion 2024", "revenue_entity_ambiguous": false, "revenue_values": [{{"value_usd": n, "year": "YYYY", "raw": "...", "line_type": "operating_revenue", "entity_match": "target", "entity_name": null}}]}}

RESEARCH:
---
{research[:8000]}
---"""
                        raw = call_llm(provider, model, _get_gemini_key() if provider == "gemini" else api_key, pr, max_tokens=512, temperature=0.0)
                        llm_rev = _parse_json_from_llm(raw)
                    except Exception:
                        pass
                if llm_rev and (llm_rev.get("revenue_usd") or llm_rev.get("revenue_values")):
                    vals = llm_rev.get("revenue_values") or (
                        [
                            {
                                "value_usd": llm_rev.get("revenue_usd"),
                                "year": None,
                                "raw": llm_rev.get("revenue_annual_original"),
                                "line_type": "operating_revenue",
                                "entity_match": "target",
                                "entity_name": None,
                            }
                        ]
                        if llm_rev.get("revenue_usd")
                        else []
                    )
                    for v in vals:
                        v["company_search"] = c
                        v["source"] = v.get("source") or "llm"
                        v.setdefault("line_type", "operating_revenue")
                        v.setdefault("entity_match", "target")
                    amb_mc = llm_rev.get("revenue_entity_ambiguous")
                    tmp_c = {
                        "revenue_values": vals,
                        "revenue_entity_ambiguous": amb_mc is True
                        or (isinstance(amb_mc, str) and str(amb_mc).lower() in ("true", "yes", "1")),
                        "revenue_usd": None,
                        "revenue_source": None,
                    }
                    apply_deterministic_primary_revenue(tmp_c, cfg)
                    companies_results.append(
                        {
                            "name": c,
                            "revenue_usd": tmp_c.get("revenue_usd"),
                            "revenue_annual_original": tmp_c.get("revenue_annual_original"),
                            "revenue_values": tmp_c.get("revenue_values") or vals,
                            "revenue_source": "llm",
                            "revenue_selection_reasoning": tmp_c.get("revenue_selection_reasoning"),
                        }
                    )
                    all_revs.extend(
                        [dict(x) for x in (tmp_c.get("revenue_values") or []) if x.get("value_usd")]
                    )
                    print(f"      [LLM] {c}: extracted")
                else:
                    regex_revs = extract_revenue_from_blocks(research, company_search=c)
                    if regex_revs:
                        print(f"      [Regex] fallback {c}: {len(regex_revs)} value(s)")
                    revs_sorted = sorted(regex_revs, key=_revenue_entry_sort_key)
                    vals = []
                    for r in revs_sorted[:15]:
                        item = {
                            "value_usd": r.get("value_usd"),
                            "currency": r.get("currency"),
                            "year": r.get("year"),
                            "raw": r.get("raw"),
                            "source": r.get("source"),
                            "company_search": r.get("company_search"),
                            "line_type": r.get("line_type"),
                            "entity_match": r.get("entity_match"),
                        }
                        if r.get("raw_normalized"):
                            item["raw_normalized"] = r["raw_normalized"]
                        vals.append({k: v for k, v in item.items() if v is not None})
                    tmp_c = {
                        "revenue_values": vals,
                        "revenue_entity_ambiguous": False,
                        "revenue_usd": None,
                        "revenue_source": None,
                    }
                    apply_deterministic_primary_revenue(tmp_c, cfg)
                    companies_results.append(
                        {
                            "name": c,
                            "revenue_usd": tmp_c.get("revenue_usd"),
                            "revenue_annual_original": tmp_c.get("revenue_annual_original"),
                            "revenue_values": tmp_c.get("revenue_values") or vals,
                            "revenue_source": "regex",
                            "revenue_selection_reasoning": tmp_c.get("revenue_selection_reasoning"),
                        }
                    )
                    all_revs.extend([dict(x) for x in (tmp_c.get("revenue_values") or []) if x.get("value_usd")])
            else:
                companies_results.append({"name": c, "revenue_usd": None, "revenue_values": [], "revenue_source": None})

        if not companies_results:
            return {"error": "No search results for any company"}

        all_revs_dedup = []
        seen = set()
        for r in all_revs:
            k = (r.get("value_usd"), r.get("year"), r.get("company_search"), r.get("raw"))
            if k not in seen:
                seen.add(k)
                entry = {
                    "value_usd": r.get("value_usd"),
                    "currency": r.get("currency"),
                    "year": r.get("year"),
                    "raw": r.get("raw"),
                }
                if r.get("source"):
                    entry["source"] = r["source"]
                if r.get("company_search"):
                    entry["company_search"] = r["company_search"]
                if r.get("raw_normalized"):
                    entry["raw_normalized"] = r["raw_normalized"]
                if r.get("line_type"):
                    entry["line_type"] = r["line_type"]
                if r.get("entity_match"):
                    entry["entity_match"] = r["entity_match"]
                if r.get("entity_name") is not None:
                    entry["entity_name"] = r["entity_name"]
                all_revs_dedup.append(entry)

        base_src = "llm" if any(r.get("revenue_source") == "llm" for r in companies_results) else "regex"
        tmp_agg = {
            "revenue_values": all_revs_dedup,
            "revenue_usd": None,
            "revenue_annual_original": None,
            "revenue_source": None,
            "revenue_entity_ambiguous": len(company_names) > 1,
        }
        apply_deterministic_primary_revenue(tmp_agg, cfg)
        rs = tmp_agg.get("revenue_source") or base_src
        result = {
            "llm": None,
            "searxng": None,
            "revenue_usd": tmp_agg.get("revenue_usd"),
            "revenue_annual_original": tmp_agg.get("revenue_annual_original"),
            "revenue_source": f"{rs} (multi-company)",
            "revenue_selection_reasoning": tmp_agg.get("revenue_selection_reasoning"),
            "revenue_entity_ambiguous": tmp_agg.get("revenue_entity_ambiguous"),
            "revenue_values": (tmp_agg.get("revenue_values") or all_revs_dedup)[:15],
            "companies": companies_results,
            "search_sources": list(dict.fromkeys(search_sources))[:20],
            "country": country_h or None,
            "sector": sector_h or None,
        }
        combined_research = "\n\n---\n\n".join(research_parts) if research_parts else ""
        result["employees"] = extract_employees_from_blocks(combined_research) if combined_research else None
        result.setdefault("headquarters", None)
        result = apply_listing_overrides(result, listing_data)
        result = ensure_required_fields(result, country_h, sector_h)
        if all_search_raw:
            result["_search_raw"] = all_search_raw
        return result

    # Single company: 3 sources → LLM extraction
    search_target = name or website or other or "unknown"
    debug_raw = cfg.get("debug_raw", False)

    # Source 1 + Source 2+3 in parallel
    from concurrent.futures import ThreadPoolExecutor
    sectors_list = _load_sectors_list(SCRIPT_DIR / "sectors.json")
    skip_revenue_search = (
        _listing_has_revenue(listing_data)
        if cfg.get("skip_revenue_queries_if_listing_has_revenue", True)
        else False
    )
    gemini_model_text = ""
    research = ""
    search_sources: list[str] = []
    search_raw: list[dict] = []

    def _fetch_gemini_direct():
        if not gemini_key or "gemini" not in backends:
            return ""
        try:
            result = gemini_enrich_direct(search_target, gemini_key, (cfg.get("models") or {}).get("gemini", "gemini-2.5-flash"), sectors_list=sectors_list, debug_raw=debug_raw)
            if result:
                print(f"  [Gemini model] direct response")
            return result
        except Exception as e:
            print(f"  [!] Gemini model direct failed: {e}", file=sys.stderr)
            return ""

    def _fetch_search():
        print(f"  [Search] {search_target}... ({', '.join(backends)})")
        if skip_revenue_search:
            print("  [Search] Skipping revenue-specific queries (listing already has numeric revenue).")
        return _run_search(
            search_target, country_h, sector_h, cfg, model,
            backends, gemini_key, ollama_key,
            debug_raw=debug_raw,
            skip_revenue_queries=skip_revenue_search,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        f_direct = executor.submit(_fetch_gemini_direct)
        f_search = executor.submit(_fetch_search)
        gemini_model_text = f_direct.result()
        research, search_sources, search_raw = f_search.result()

    err_msg = "No search results"
    if "gemini" in backends and not gemini_key:
        err_msg = "Need api_keys.gemini for gemini search"
    elif "ollama" in backends and not ollama_key:
        err_msg = "Need api_keys.ollama or OLLAMA_API_KEY for ollama search"
    elif "searxng" in backends:
        import os
        if not (cfg.get("searxng_base_url", "") or os.getenv("SEARXNG_BASE_URL", "")).strip():
            err_msg = "Need searxng_base_url in config.yaml or SEARXNG_BASE_URL env var"
    if not research or len(research.strip()) < 50:
        return {"error": err_msg}

    # LLM extraction: all 3 sources combined, no truncation
    # Source 1: Gemini model direct
    # Source 2: Gemini web search (via _run_search)
    # Source 3: SearXNG (via _run_search)
    out = None
    use_llm = not cfg.get("use_regex_only", False)
    if use_llm:
        try:
            # Extract SearXNG profile snippets (ZoomInfo, LinkedIn, Crunchbase) and
            # place them FIRST so the LLM sees authoritative structured data before
            # long Gemini narratives that may obscure concrete facts.
            import re as _re_combined
            profile_blocks = []
            other_blocks = []
            if research:
                for block in research.split("\n\n---\n\n"):
                    if _re_combined.search(r"\[SearXNG\|.*(?:site:zoominfo|site:linkedin|site:crunchbase)", block):
                        profile_blocks.append(block)
                    else:
                        other_blocks.append(block)

            combined_parts = []
            if profile_blocks:
                combined_parts.append(
                    f"=== HIGH-PRIORITY: SEARXNG PROFILE SNIPPETS (ZoomInfo/LinkedIn/Crunchbase) ===\n"
                    + "\n\n---\n\n".join(profile_blocks)
                )
            if gemini_model_text:
                combined_parts.append(f"=== SOURCE 1: GEMINI MODEL (direct) ===\n{gemini_model_text}")
            if other_blocks:
                combined_parts.append(
                    f"=== SOURCE 2+3: GEMINI WEB SEARCH + SEARXNG ===\n"
                    + "\n\n---\n\n".join(other_blocks)
                )
            elif research and not profile_blocks:
                combined_parts.append(f"=== SOURCE 2+3: GEMINI WEB SEARCH + SEARXNG ===\n{research}")
            combined = "\n\n".join(combined_parts)

            if sectors_list:
                sectors_str = ", ".join(f'"{s}"' for s in sectors_list[:400])
                sector_json_line = '  "sector": "exactly one string from the allowed list above, or null",'
                sectors_instruction = f"""
SECTOR (controlled vocabulary — same list as Gemini direct source):
- The "sector" field must be EXACTLY ONE string chosen from the list below, OR null.
- Only set sector if the combined data explicitly supports mapping to exactly one list item (e.g. stated industry/sector that clearly matches). If ambiguous, conflicting, or not stated, use null.
- Do NOT output any sector name that is not in the list. No synonyms or paraphrases outside the list.

Allowed sectors (at most one):
{sectors_str}
"""
            else:
                sector_json_line = '  "sector": "sector/industry or null",'
                sectors_instruction = """
SECTOR:
- Extract sector/industry only as explicitly stated in the combined data. Otherwise null. Do not invent.
"""

            revenue_skip_instruction = ""
            if skip_revenue_search:
                revenue_skip_instruction = """
REVENUE EXTRACTION SKIP (STRICT):
- Ignore any revenue/turnover mentions in the text for this extraction run.
- Set revenue_values to [].
"""

            extract_prompt = f"""You are a data extraction assistant. Below is data from 3 sources:
1) Gemini model (direct knowledge)
2) Gemini web search results
3) SearXNG search results

CRITICAL — YOU MUST FOLLOW:
- Base your answer ONLY on the data provided below. Do NOT use your own knowledge, do NOT infer, do NOT invent.
- If a fact is not explicitly stated in the provided data, return null for that field.
- Do NOT invent company names, countries, revenues, or any other information. Only extract what is written.

TARGET ATTRIBUTION (search noise — similarly named companies, parents, subsidiaries, competitors often appear):
- Your job is to decide whether each fact in the combined data actually refers to TARGET COMPANY below (same company name/trade name, or a clearly stated alias in the SAME snippet/sentence as the fact).
- Do NOT assign facts to the target when the text clearly names a different company, a homonym, a parent/sibling entity, or an industry aggregate — unless the passage explicitly states it is the same organization as TARGET COMPANY.
- employees: headcount / staff / workforce for TARGET COMPANY only (not a group total for another firm).

COUNTRY / HEADQUARTERS — DETAILED RULES (follow in order):
- Your goal is to find the SINGLE BEST country and headquarters location for TARGET COMPANY.
- Source priority for country/headquarters (use first match):
  1) ZoomInfo / Crunchbase / Craft.co profile lines: "location in [Country]", "headquartered in [City]"
  2) LinkedIn profile: "Headquarters: [City, Country]"
  3) Official website "About" / "Contact" pages with address
  4) Authoritative text: "founded in [City]", "[Target] is based in [City]", Library of Congress record, etc.
  5) General web mentions tying a place to TARGET COMPANY
- If headquarters is an unambiguous **US city + state** (e.g. "Portland, Oregon" or "Seattle, Washington"), set country to "United States".
- For decentralized / global organizations with no single HQ: use the **founding location** or **primary registered location** as country/headquarters. Example: if the entity "was founded in Seattle" and has no central HQ, set country="United States", headquarters="Seattle, Washington, United States".
- Do NOT set country from: generic market statistics, lists of other companies, unrelated agencies, or web traffic by country.
- If multiple conflicting countries appear for TARGET COMPANY itself (not other entities), prefer the ZoomInfo/Crunchbase profile, then the founding/registration location.
- revenue_usd / revenue_annual_original: the single best annual operating revenue (sales/turnover) explicitly stated FOR TARGET COMPANY. If none is clearly tied to TARGET COMPANY, use null.
- EXCLUDE from revenue entirely: acquisition/buyout/merger/takeover price, "company is bought for $X", "sold for", "deal valued at", funding rounds, valuations — these are NOT revenue even if they mention TARGET COMPANY.
- revenue_values: list EVERY revenue/financial figure you find. For each row write the verbatim text as "raw", numeric USD value (null if only a range like "$5M-$20M"), year if mentioned, and source URL or label. Exclude only clear non-revenue figures: acquisition prices ("acquired for $X", "sold for $X"), funding rounds, valuations. Do NOT leave this empty if any revenue figures appear in the data.
- Do not invent scale: if the text does not state million/billion and the amount is ambiguous, set value_usd to null.

{sectors_instruction}
{revenue_skip_instruction}
TARGET COMPANY: {search_target}

COMBINED DATA:
---
{combined}
---

Return ONLY valid JSON, no markdown:
{{
  "country": "country name or null",
{sector_json_line}
  "revenue_values": [
    {{"value_usd": 8400000, "year": "2024", "raw": "$8.4 million", "source": "owler.com"}}
  ],
  "headquarters": "city, country or null",
  "employees": number or null
}}"""
            raw_resp = call_llm(
                provider, model, _get_gemini_key() if provider == "gemini" else api_key, extract_prompt, max_tokens=8192, temperature=0.0
            )
            if debug_raw:
                print(f"\n--- [DEBUG] LLM extraction RAW RESPONSE ---\n{raw_resp}\n--- [DEBUG] END ---\n", file=sys.stderr)
            out = _parse_json_from_llm(raw_resp)
            if out:
                amb = out.get("revenue_entity_ambiguous")
                out["revenue_entity_ambiguous"] = amb is True or (
                    isinstance(amb, str) and str(amb).lower() in ("true", "yes", "1")
                )
                print(f"  [LLM] extracted: country={out.get('country')}, sector={out.get('sector')}, "
                      f"HQ={out.get('headquarters')}, employees={out.get('employees')}, "
                      f"revenue_usd={out.get('revenue_usd')}, rev_values={len(out.get('revenue_values') or [])}, "
                      f"ambiguous={out.get('revenue_entity_ambiguous')}")
            else:
                print(f"  [!] LLM extraction returned no parseable JSON → falling back to regex", file=sys.stderr)
        except Exception as e:
            print(f"  [!] LLM extract failed: {e}", file=sys.stderr)

    # 2. Build extraction block (LLM output or regex fallback)
    # Optional: merge regex hits into revenue_values (default off — LLM attribution is authoritative)
    merged_regex = bool(cfg.get("revenue_merge_regex", False))
    combined_text_for_regex = (gemini_model_text or "") + "\n" + (research or "")
    regex_revs_extra = (
        extract_revenue_from_blocks(combined_text_for_regex) if merged_regex else []
    )

    searxng_block = None
    if out:
        if out.get("revenue_usd") and not out.get("revenue_values"):
            out["revenue_values"] = [
                {
                    "value_usd": out["revenue_usd"],
                    "year": None,
                    "raw": out.get("revenue_annual_original"),
                    "source": "llm",
                    "line_type": "operating_revenue",
                    "entity_match": "target",
                    "entity_name": None,
                }
            ]
        existing_vals = out.get("revenue_values") or []
        for v in existing_vals:
            _normalize_revenue_row_fields(v)
        if merged_regex:
            existing_usd = {round(v.get("value_usd") or 0, -4) for v in existing_vals}
            for r in regex_revs_extra:
                key = round(r.get("value_usd") or 0, -4)
                if key and key not in existing_usd:
                    entry = {
                        "value_usd": r.get("value_usd"),
                        "year": r.get("year"),
                        "raw": r.get("raw"),
                        "source": r.get("source") or "regex",
                        "line_type": "operating_revenue",
                        "entity_match": "unclear",
                    }
                    if r.get("raw_normalized"):
                        entry["raw_normalized"] = r["raw_normalized"]
                    existing_vals.append({k: v for k, v in entry.items() if v is not None})
                    existing_usd.add(key)
        out["revenue_values"] = existing_vals
        out["revenue_source"] = out.get("revenue_source") or ("llm" if out.get("revenue_usd") or out.get("revenue_values") else None)
        out["search_sources"] = list(dict.fromkeys(search_sources))[:20]
        if out.get("employees") is None:
            out["employees"] = extract_employees_from_blocks(research)
        out = apply_listing_overrides(out, listing_data)
        out = ensure_required_fields(out, country_h, sector_h)
        searxng_block = build_enriched_block(out, listing_data, country_h, sector_h)
        # Entity/scale gates + contradiction check (see apply_deterministic_primary_revenue)
        apply_deterministic_primary_revenue(searxng_block, cfg)
    else:
        # Regex fallback (SearXNG LLM extraction failed)
        regex_revs = extract_revenue_from_blocks(research)
        employees_val = extract_employees_from_blocks(research)
        if regex_revs:
            print(f"  [Regex] last resort: {len(regex_revs)} value(s) (revenue queries only)")
            best = select_primary_revenue_entry(regex_revs) or {}
            vals = []
            for r in sorted(regex_revs, key=_revenue_entry_sort_key)[:15]:
                v = {"value_usd": r.get("value_usd"), "currency": r.get("currency"), "year": r.get("year"), "raw": r.get("raw")}
                if r.get("source"):
                    v["source"] = r.get("source")
                if r.get("line_type"):
                    v["line_type"] = r["line_type"]
                if r.get("entity_match"):
                    v["entity_match"] = r["entity_match"]
                if r.get("raw_normalized"):
                    v["raw_normalized"] = r.get("raw_normalized")
                vals.append({k: x for k, x in v.items() if x is not None})
            regex_out = {
                "revenue_usd": best.get("value_usd"),
                "revenue_annual_original": best.get("raw_normalized") or best.get("raw"),
                "revenue_source": "regex",
                "revenue_entity_ambiguous": False,
                "revenue_selection_reasoning": None,
                "revenue_values": vals,
                "search_sources": list(dict.fromkeys(search_sources))[:20],
                "country": country_h or None,
                "sector": sector_h or None,
                "employees": employees_val,
                "headquarters": None,
            }
            regex_out = apply_listing_overrides(regex_out, listing_data)
            regex_out = ensure_required_fields(regex_out, country_h, sector_h)
            searxng_block = build_enriched_block(regex_out, listing_data, country_h, sector_h)
            apply_deterministic_primary_revenue(searxng_block, cfg)

    # 2. Final block: extraction output (or regex fallback)
    merged = dict(searxng_block or {})
    merged = apply_listing_overrides(merged, listing_data)
    merged = ensure_required_fields(merged, country_h, sector_h)

    result = {**merged}
    if not searxng_block:
        result["error"] = "Extraction failed"
    # Raw responses from all 3 sources — always included
    result["_raw_sources"] = {
        "gemini_model_direct": gemini_model_text or None,
        "gemini_web_search_and_searxng": research or None,
        "searxng_raw_json": search_raw or None,
    }
    return result


def main():
    ap = argparse.ArgumentParser(description="Enrich company info (country, sector, revenue)")
    ap.add_argument("--json",   type=Path, default=None, help="post_parser output JSON")
    ap.add_argument("--text",   type=Path, default=None, help="Raw text file")
    ap.add_argument("--company", type=str, default=None, help="Directly pass company name (bypasses LLM extraction)")
    ap.add_argument("--stdin",  action="store_true",     help="Read from stdin")
    ap.add_argument("--output", type=Path, default=None, help="Output JSON file")
    ap.add_argument("--provider", choices=["anthropic", "openai", "gemini", "ollama", "openrouter", "groq"], default=None, help="Override config.provider")
    ap.add_argument("--search", default=None, dest="search_backend", help="Search: searxng | gemini | ollama | all | searxng,gemini")
    ap.add_argument("--no-fetch", action="store_true", help="Disable fetching result pages")
    ap.add_argument("--no-llm", action="store_true", help="Use regex only for revenue (skip LLM extraction)")
    ap.add_argument("--debug-raw", action="store_true", help="Print raw Gemini/LLM responses to stderr")
    ap.add_argument("--save-raw", action="store_true", help="Include raw search results (SearXNG) in output JSON")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg:
        print("[!] config.yaml not found. All API keys are read from config.yaml.", file=sys.stderr)
        sys.exit(1)

    provider = args.provider or cfg.get("provider", "gemini")
    models = cfg.get("models", {})
    default_models = {"gemini": "gemini-2.5-flash-lite", "openai": "gpt-4o", "anthropic": "claude-opus-4-6", "ollama": "llama3.2", "openrouter": "openai/gpt-oss-12b", "groq": "llama-3.1-8b-instant"}
    model = models.get(provider, default_models.get(provider, "gemini-2.5-flash-lite"))
    api_keys = cfg.get("api_keys", {})
    api_key = (api_keys.get(provider, "") or "").strip()

    if provider != "ollama" and not api_key:
        print(f"[!] Missing api_keys.{provider} in config.yaml", file=sys.stderr)
        sys.exit(1)

    if args.search_backend:
        cfg = {**cfg, "search_backend": args.search_backend}
    if args.no_llm:
        cfg = {**cfg, "use_regex_only": True}
    if args.debug_raw:
        cfg = {**cfg, "debug_raw": True}
    search_backend = str(cfg.get("search_backend", "searxng")).strip().lower()
    if search_backend == "all" and not any(api_keys.get(k, "").strip() for k in ["gemini", "ollama"]):
        print("[>] Tip: api_keys.gemini (or ollama) for search; searxng needs no key (self-hosted)", file=sys.stderr)

    print(f"[>] Provider: {provider} | Model: {model} | Search: {search_backend}")

    parsed = None
    clues = None

    if args.json and args.json.exists():
        with open(args.json, encoding="utf-8") as f:
            parsed = json.load(f)
        clues = clues_from_parsed(parsed, cfg)
        print(f"[>] Using parsed JSON: {args.json.name}")
        cs = clues.get('company_names') or ([clues.get('company_name')] if clues.get('company_name') else [])
        print(f"    company: {', '.join(cs) if cs else clues.get('company_name')}, country: {clues.get('country_hint')}, sector: {clues.get('sector_hint')}")
    elif args.stdin:
        text = sys.stdin.read()
        if not text.strip():
            print("[!] Empty stdin", file=sys.stderr)
            sys.exit(1)
        print("[>] 1. Extracting clues...")
        clues = extract_clues(text, provider, model, api_key)
        print(f"    company: {clues.get('company_name')}, website: {clues.get('website')}")
    elif args.text and args.text.exists():
        with open(args.text, encoding="utf-8") as f:
            text = f.read()
        if not text.strip():
            print("[!] Empty text file", file=sys.stderr)
            sys.exit(1)
        print("[>] 1. Extracting clues...")
        clues = extract_clues(text, provider, model, api_key)
        print(f"    company: {clues.get('company_name')}, website: {clues.get('website')}")
    elif args.company:
        clues = {"company_name": args.company.strip(), "listing_data": {}, "country_hint": None, "sector_hint": None}
        print(f"[>] Using direct company name: {args.company}")
    else:
        print("[!] Provide --company <name>, --json <parsed.json>, --text <file>, or --stdin", file=sys.stderr)
        sys.exit(1)

    if not clues or (not clues.get("company_name") and not clues.get("company_names") and not clues.get("other_clues")):
        print("[!] No company clues to search.", file=sys.stderr)
        sys.exit(1)

    print("[>] 2. Searching the web...")
    enriched = search_and_enrich(clues, cfg, provider, model, api_key)

    search_raw = enriched.pop("_search_raw", None)
    save_raw = getattr(args, "save_raw", False) or cfg.get("save_raw", False)
    ld = clues.get("listing_data") or {}
    ch = (clues.get("country_hint") or "").strip()
    sh = (clues.get("sector_hint") or "").strip()
    if enriched.get("error"):
        enriched_block = {
            "country": ch or None,
            "sector": sh or None,
            "revenue_usd": None,
            "revenue_annual_original": None,
            "revenue_source": None,
            "revenue_selection_reasoning": None,
            "revenue_values": [],
            "employees": None,
            "headquarters": None,
            "search_sources": [],
            "companies": None,
            "_search_error": enriched.get("error"),
        }
    else:
        enriched_block = {k: v for k, v in enriched.items() if k != "error"}
    enriched_block = build_enriched_block(enriched_block, ld, ch, sh)

    result = build_slim_output(parsed, enriched_block)
    if save_raw and search_raw:
        result["_search_raw"] = search_raw

    print("\n[>] Result (slim):")
    for k in ("country", "sector", "revenue_usd", "revenue_raw", "revenue_year", "employees", "headquarters"):
        if k in result and result[k] is not None:
            print(f"    {k}: {result[k]}")

    out_path = args.output or (args.json.with_stem(args.json.stem + "_enriched") if args.json else None)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\n[OK] Saved: {out_path}")
    else:
        print("\n" + json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()