#!/usr/bin/env python3
"""
Company Shodan Footprint & Cyberattack Intelligence
===================================================
Resolves a company's domain using active web search and LLM extraction,
queries Shodan API with query ssl:"<domain>", recursively filters
fields: product, os, server, and org ($..product, $..os, $..server, $..org).

Additionally, automatically audits the company's breach history via
the local Ransomware Live victim database and live web search synthesis
(using SearXNG or Gemini web search) in a single unified OSINT stage runtime.

All settings (API keys, models, backends) are read from config.yaml.
Supports both the official shodan SDK and pure REST API HTTP fallbacks.
"""

import os
import sys
import json
import argparse
import urllib.request
import urllib.parse
import urllib.error
import time
from pathlib import Path
import re
from collections import Counter

# ANSI escape color codes
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
MAGENTA = "\033[95m"
BLUE = "\033[94m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"

SCRIPT_DIR = Path(__file__).parent
sys.path.append(str(SCRIPT_DIR))

# Force stdout to UTF-8 to prevent UnicodeEncodeError on Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Fallback config loader in case import fails
def load_config() -> dict:
    try:
        from company_enrich import load_config as ce_load_config
        return ce_load_config()
    except ImportError:
        import yaml
        cfg_path = SCRIPT_DIR / "config.yaml"
        if not cfg_path.exists():
            return {}
        with open(cfg_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

# Helper to call LLM with automatic key fallbacks to prevent 429 rate limits
def robust_call_llm(cfg: dict, prompt: str, max_tokens: int = 128) -> str | None:
    api_keys = cfg.get("api_keys", {})
    models = cfg.get("models", {})
    
    # Priority order: 1) Active configured provider, 2) groq, 3) gemini, 4) openrouter
    providers_to_try = [cfg.get("provider", "gemini"), "groq", "gemini", "openrouter"]
    seen = set()
    providers_to_try = [p for p in providers_to_try if not (p in seen or seen.add(p))]
    
    from company_enrich import call_llm
    
    for prov in providers_to_try:
        api_key = (api_keys.get(prov, "") or "").strip()
        if not api_key:
            continue
            
        default_models = {
            "gemini": "gemini-2.5-flash-lite",
            "openai": "gpt-4o",
            "anthropic": "claude-opus-4-6",
            "ollama": "llama3.2",
            "openrouter": "openai/gpt-oss-12b",
            "groq": "llama-3.1-8b-instant"
        }
        model = models.get(prov, default_models.get(prov, "gemini-2.5-flash-lite"))
        
        try:
            print(f"  [*] Attempting LLM domain extraction via provider '{prov}' using model '{model}'...")
            resp = call_llm(prov, model, api_key, prompt, max_tokens=max_tokens, temperature=0.0)
            if resp:
                return resp
        except Exception as e:
            print(f"  [!] Provider '{prov}' call failed: {e}")
            
    return None

def web_search_searxng(query: str, base_url: str, max_results: int = 8) -> tuple[str, list[dict]]:
    """
    SearXNG meta search via JSON API with automatic retry on 429 Rate Limits.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("Missing SearXNG base URL")
    search_url = base if base.endswith("/search") else f"{base}/search"
    params = {"q": query, "format": "json"}
    req_url = f"{search_url}?{urllib.parse.urlencode(params)}"
    
    max_retries = 3
    backoff_seconds = 10
    
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            req_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
        )
        try:
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
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"  [!] SearXNG returned HTTP 429 Rate Limit (Attempt {attempt}/{max_retries}). Sleeping {backoff_seconds}s before retrying...")
                time.sleep(backoff_seconds)
                backoff_seconds *= 2  # Exponential backoff
            else:
                raise e
        except Exception as e:
            if attempt == max_retries:
                raise e
            print(f"  [!] SearXNG query failed: {e}. Retrying in 5s...")
            time.sleep(5)
            
    raise Exception("SearXNG rate limit exceeded or query failed after all retries.")


# Domain extraction helper
def resolve_domain(company_name: str, cfg: dict, country: str | None = None) -> str | None:
    print(f"searxng search for domain..")
    
    # Use the raw company name and country to target the search query
    company_clean = company_name.strip()
    if country:
        search_query = f'"{company_clean}" "{country.strip()}"'
    else:
        search_query = company_clean
        
    search_results = ""
    raw_results = []
    
    search_backend = str(cfg.get("search_backend", "searxng")).strip().lower()
    api_keys = cfg.get("api_keys", {})
    models = cfg.get("models", {})
    
    # List of common directory/encyclopedia domains to skip
    EXCLUDED_DOMAINS = {
        "wikipedia.org", "en.wikipedia.org", "tr.wikipedia.org",
        "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
        "youtube.com", "pinterest.com", "crunchbase.com", "zoominfo.com",
        "bloomberg.com", "yahoo.com", "google.com", "github.com", "reddit.com",
        "mapquest.com", "yelp.com", "glassdoor.com", "tripadvisor.com",
        "yellowpages.com", "dnb.com", "zippia.com", "indeed.com", "salary.com",
        "marketwatch.com", "reuters.com", "sec.gov"
    }

    def is_excluded(url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
            netloc = parsed.netloc or parsed.path
            netloc = netloc.lower().split(":")[0]
            if netloc.startswith("www."):
                netloc = netloc[4:]
            for excl in EXCLUDED_DOMAINS:
                if netloc == excl or netloc.endswith("." + excl):
                    return True
            return False
        except Exception:
            return True


    def get_clean_domain(url: str) -> str | None:
        try:
            parsed = urllib.parse.urlparse(url)
            netloc = parsed.netloc or parsed.path
            netloc = netloc.lower().split(":")[0]
            if netloc.startswith("www."):
                netloc = netloc[4:]
            if "." in netloc and len(netloc) > 3:
                return netloc
        except Exception:
            pass
        return None

    # 1. Try SearXNG search
    base_url = cfg.get("searxng_base_url", "http://localhost:8888")
    try:
        search_results, raw_results = web_search_searxng(search_query, base_url, max_results=10)
        
        # Check results and extract the first non-excluded domain
        for res in raw_results:
            url = res.get("url", "")
            if url and not is_excluded(url):
                domain = get_clean_domain(url)
                if domain:
                    print(f"domain is found: {domain}")
                    return domain
    except Exception:
        pass
            
    # 2. Try Gemini/LLM web search if SearXNG returned no results or could not resolve a domain
    if "gemini" in api_keys:
        gemini_key = (api_keys.get("gemini", "") or "").strip()
        gemini_model = models.get("gemini", "gemini-2.5-flash-lite")
        try:
            from company_enrich import web_search_gemini
            print(f"llm search for domain..")
            search_results, _ = web_search_gemini(search_query, gemini_key, gemini_model)
        except Exception:
            pass

            
    # Fallback prompts for LLM model
    if not search_results or search_results == "No search results available.":
        prompt = f"""You are a cybersecurity intelligence tool. We could not find any web search results for the company "{company_name}".
Based on your pre-trained knowledge, identify the primary, official website domain (e.g., example.com, test.co.uk) of "{company_name}".
Clean it by removing "http://", "https://", "www.", and any trailing slashes or paths.
Return ONLY valid JSON in the following format:
{{"domain": "domain_name_or_null"}}

Examples:
- If company is "A.G. Spanos Companies" -> agspanos.com
- If company is "Mesa Energy Systems" -> mesaenergy.com
- If you are completely unsure or the company is extremely generic, return null."""
    else:
        # Ask the LLM to extract the domain from search results
        prompt = f"""You are a cybersecurity intelligence tool. Your task is to extract the primary, official domain name of the company "{company_name}" based on the search results provided.

Search Results:
---
{search_results}
---

Task:
1. Identify the official website domain (e.g., example.com, test.co.uk) of "{company_name}".
2. Clean it by removing "http://", "https://", "www.", and any trailing slashes or paths.
3. Return ONLY valid JSON in the following format:
{{"domain": "domain_name_or_null"}}

Examples:
- If website is https://www.calidra.cl/contacto -> calidra.cl
- If website is http://xyx.com/abc -> xyx.com
- If cannot be found -> null"""

    try:
        from company_enrich import _parse_json_from_llm
        raw_resp = robust_call_llm(cfg, prompt, max_tokens=128)
        parsed = _parse_json_from_llm(raw_resp)
        if parsed and "domain" in parsed:
            domain = parsed["domain"]
            if domain:
                domain = domain.strip().lower()
                print(f"domain is found: {domain}")
                return domain
    except Exception:
        pass
        
    # 3. Robust clean fallback heuristic if all search and LLM fail
    clean_name = "".join(c for c in company_name if c.isalnum()).lower().strip()
    
    # Strip common trailing corporate words
    corporate_suffixes = {"companies", "company", "solutions", "solution", "systems", "system", "services", "service", "construction", "international", "group", "inc", "co", "ltd", "corp", "corporation", "llc"}
    words = [w.lower().strip() for w in company_name.split() if w.strip()]
    clean_words = ["".join(c for c in w if c.isalnum()) for w in words]
    clean_words = [w for w in clean_words if w]
    
    if clean_words:
        if len(clean_words) > 1 and clean_words[-1] in corporate_suffixes:
            candidate_base = "".join(clean_words[:-1])
        else:
            candidate_base = "".join(clean_words)
        heuristic_domain = candidate_base + ".com"
    else:
        heuristic_domain = clean_name + ".com"
        
    print(f"domain is found: {heuristic_domain}")
    return heuristic_domain


# Shodan Querying
def query_shodan(query: str, shodan_key: str) -> dict:
    # 1. Try using the official shodan library
    try:
        from shodan import Shodan
        api = Shodan(shodan_key)
        results = api.search(query)
        return results
    except ImportError:
        pass
    except Exception:
        pass
        
    # 2. Direct REST API HTTP Fallback
    params = {
        'query': query,
        'key': shodan_key
    }
    url = f"https://api.shodan.io/shodan/host/search?{urllib.parse.urlencode(params)}"
    
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "CompanyShodanEnrich/1.0"}
    )
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
            return data
    except urllib.error.HTTPError as e:
        err_content = e.read().decode('utf-8', errors='ignore')
        try:
            err_json = json.loads(err_content)
            err_msg = err_json.get("error", err_content)
        except Exception:
            err_msg = err_content
        raise RuntimeError(f"Shodan API HTTP Error {e.code}: {err_msg}")
    except Exception as e:
        raise RuntimeError(f"Failed to query Shodan API: {e}")

# Recursive walker ($..product, $..os, $..server, $..org)
def extract_shodan_fields(data: dict) -> dict:
    fields = {
        'product': [],
        'os': [],
        'server': [],
        'org': []
    }
    
    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in fields:
                    if v is not None and isinstance(v, (str, int, float, bool)):
                        fields[k].append(str(v).strip())
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)
                
    walk(data)
    
    # Process results with frequency counts
    results = {}
    for k, v_list in fields.items():
        counter = Counter(v_list)
        sorted_counts = sorted(counter.items(), key=lambda x: x[1], reverse=True)
        results[k] = {
            "unique_values": [item[0] for item in sorted_counts],
            "frequencies": {item[0]: item[1] for item in sorted_counts},
            "total_occurrences": len(v_list)
        }
    return results



# ── crt.sh Certificate Transparency Log Query ──────────────────────────────────

CRTSH_KEYWORDS = [
    "forti", "citrix", "vpn", "gateway", "pulse", "palo", "sophos", 
    "cisco", "asa", "f5", "anyconnect", "globalprotect", "netscaler",
    "pam", "cyberark", "beyondtrust", "thycotic", "delinea", "wallix",
    "rdp", "vnc", "ssh", "awingu", "guacamole"
]

def query_crtsh(domain: str) -> dict:
    """
    Query crt.sh PostgreSQL database for subdomains via Certificate Transparency logs.
    Uses FTS (Full Text Search) with GIN index for speed.
    """
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        return {"all_subdomains": [], "sensitive_matches": [], "total_count": 0, "error": "psycopg not installed"}
    
    result = {
        "all_subdomains": [],
        "sensitive_matches": [],
        "total_count": 0,
        "error": None
    }
    
    db_conn_str = 'postgresql://guest@crt.sh:5432/certwatch'
    
    try:
        print(f"crt.sh search starts...")
        db_connection = psycopg.connect(db_conn_str, autocommit=True, row_factory=dict_row)
        db_cursor = db_connection.cursor()
        
        query = """
            SELECT DISTINCT 
                lower(ns.name_value) AS domain_name
            FROM 
                certificate_and_identities ns
            WHERE 
                ns.certificate_id IN (
                    SELECT c.id 
                    FROM certificate c 
                    WHERE to_tsquery('certwatch', %s) @@ identities(c.certificate)
                )
                AND ns.name_value NOT LIKE '*%%'
                AND ns.name_value LIKE %s;
        """
        
        db_cursor.execute(query, (domain, f"%.{domain}"))
        
        all_domains = []
        sensitive_matches = []
        
        for row in db_cursor:
            subdomain = row['domain_name']
            all_domains.append(subdomain)
            
            # Check for keyword matches with false-positive-safe regex
            matched_kws = []
            for kw in CRTSH_KEYWORDS:
                if kw in ["asa", "pam", "f5"]:
                    pattern = rf"\b{kw}\b"
                    if kw == "asa":
                        pattern += "|ciscoasa"
                    elif kw == "pam":
                        pattern += "|cyberarkpam"
                    if re.search(pattern, subdomain):
                        matched_kws.append(kw)
                elif kw in subdomain:
                    matched_kws.append(kw)
            
            if matched_kws:
                sensitive_matches.append({"domain": subdomain, "keywords": matched_kws})
        
        db_cursor.close()
        db_connection.close()
        
        result["all_subdomains"] = all_domains
        result["sensitive_matches"] = sensitive_matches
        result["total_count"] = len(all_domains)
        
    except Exception as e:
        result["error"] = str(e)
    
    return result

# Query Ransomware.live API endpoint helper
def _query_ransomware_api_endpoint(query_str: str, api_key: str) -> list:
    if not query_str:
        return []
    
    query_str = query_str.strip()
    encoded_q = urllib.parse.quote(query_str)
    url = f"https://api-pro.ransomware.live/victims/search?q={encoded_q}"
    
    headers = {
        "Accept": "application/json",
        "X-API-KEY": api_key,
        "User-Agent": "CompanyShodanEnrich/1.0"
    }
    
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            parsed_data = json.loads(body)
            
            # Handle list vs dict response structures
            if isinstance(parsed_data, list):
                raw_victims = parsed_data
            elif isinstance(parsed_data, dict):
                raw_victims = parsed_data.get("victims", parsed_data.get("results", []))
                if not isinstance(raw_victims, list):
                    if "victim" in parsed_data or "id" in parsed_data:
                        raw_victims = [parsed_data]
                    else:
                        raw_victims = []
            else:
                raw_victims = []
                
            matches = []
            for v in raw_victims:
                matches.append({
                    "id": v.get("id"),
                    "discovered": v.get("discovered"),
                    "website": v.get("website"),
                    "country": v.get("country"),
                    "activity": v.get("activity"),
                    "description": v.get("description"),
                    "permalink": v.get("permalink"),
                    "attack_date": v.get("attackdate") or v.get("attack_date"),
                    "victim": v.get("victim"),
                    "threat_actor": v.get("group") or v.get("threat_actor"),
                    "source": "Ransomware.live API"
                })
            return matches
    except urllib.error.HTTPError as e:
        print(f"  [!] Ransomware.live API HTTP Error {e.code} for '{query_str}': {e.reason}")
    except Exception as e:
        print(f"  [!] Ransomware.live API search failed for '{query_str}': {e}")
    return []

# Live Ransomware.live API checker
def check_ransomware_live_api(company_name: str | None, domain: str | None, api_key: str) -> list:
    print(f"ransomwarelive search for news....")
    matches = []
    
    # 1. Search by company name if available
    if company_name:
        matches = _query_ransomware_api_endpoint(company_name, api_key)
        
    # 2. If no matches and domain is available, search by domain
    if not matches and domain:
        if not company_name or domain.lower() != company_name.lower():
            matches = _query_ransomware_api_endpoint(domain, api_key)
            
    if matches:
        print("match found")
    else:
        print("match not found")
    return matches



# Web breach detection (SearXNG / Gemini search + LLM synthesize)
def check_web_breaches(domain: str, company_name: str, cfg: dict) -> dict:
    print(f"searxng search for breach news...")
    
    provider = cfg.get("provider", "gemini")
    models = cfg.get("models", {})
    default_models = {
        "gemini": "gemini-2.5-flash-lite",
        "openai": "gpt-4o",
        "anthropic": "claude-opus-4-6",
        "ollama": "llama3.2",
        "openrouter": "openai/gpt-oss-12b",
        "groq": "llama-3.1-8b-instant"
    }
    model = models.get(provider, default_models.get(provider, "gemini-2.5-flash-lite"))
    api_keys = cfg.get("api_keys", {})
    api_key = (api_keys.get(provider, "") or "").strip()
    
    target = domain or company_name
    search_query = f'"{target}" "data breach" OR "cyber attack" OR "ransomware" OR "hacked"'
    search_results = ""
    
    # 1. Try SearXNG search
    base_url = cfg.get("searxng_base_url", "http://localhost:8888")
    try:
        search_results, _ = web_search_searxng(search_query, base_url, max_results=8)
    except Exception:
        pass
            
    # 2. Try Gemini web search if SearXNG returned nothing
    if not search_results and "gemini" in api_keys:
        gemini_key = (api_keys.get("gemini", "") or "").strip()
        gemini_model = models.get("gemini", "gemini-2.5-flash-lite")
        try:
            from company_enrich import web_search_gemini
            print(f"llm search for breach news...")
            search_results, _ = web_search_gemini(search_query, gemini_key, gemini_model)
        except Exception:
            pass

            
    if not search_results:
        print("match not found")
        return {
            "has_attack": False,
            "attacks": [],
            "error": "No search results retrieved for cyberattacks."
        }
        
    prompt = f"""You are a professional Cyber Threat Intelligence (CTI) analyst. Your task is to analyze the following search results to determine if the target company/domain "{target}" has historically been the victim of any siber saldırı (cyber attack), ransomware, data leak, or hack.

Search Results:
---
{search_results}
---

Extract all distinct security incidents/cyber attacks relating specifically to "{target}".
Exclude generic security reports, unrelated firms, or general cybersecurity tips.

Return ONLY a valid JSON in this exact structure:
{{
  "has_attack": true or false,
  "attacks": [
    {{
      "attack_type": "e.g. Ransomware / Data Breach / DDoS / Hacking / Phishing / Unclear",
      "threat_actor": "e.g. LockBit / ShinyHunters / Unknown",
      "attack_date": "e.g. May 2026 or 2024 or null",
      "details": "Concise 1-2 sentence summary of what happened, what was leaked/breached, and the impact.",
      "reference_urls": ["list of source URLs from search results"]
    }}
  ]
}}

STRICT RULES:
- If no attacks are found in the text for "{target}", set "has_attack": false and "attacks": [].
- Do not make up facts. Only use information explicitly present in the search results text.
- If multiple attacks happened at different times, list them as separate items in the "attacks" list.
"""

    try:
        from company_enrich import call_llm, _parse_json_from_llm
        raw_resp = call_llm(provider, model, api_key, prompt, max_tokens=1024, temperature=0.0)
        parsed = _parse_json_from_llm(raw_resp)
        if parsed:
            if parsed.get("has_attack"):
                print("match found")
            else:
                print("match not found")
            return parsed
    except Exception:
        pass
        
    print("match not found")
    return {
        "has_attack": False,
        "attacks": [],
        "error": "Failed to synthesize breach data via LLM."
    }


# Beautiful Terminal Dashboard
def print_dashboard(domain, query, total_results, matches, filtered_data):
    # ANSI escape color codes
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    MAGENTA = "\033[95m"
    BLUE = "\033[94m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    RESET = "\033[0m"
    
    print("\n" + "=" * 80)
    print(f" {BOLD}{CYAN}SHODAN CYBER FOOTPRINT INTELLIGENCE{RESET}".center(90))
    print("=" * 80)
    
    print(f"  {BOLD}Target Domain:{RESET}  {GREEN}{domain or 'N/A'}{RESET}")
    print(f"  {BOLD}Shodan Query:{RESET}   {YELLOW}{query}{RESET}")
    print(f"  {BOLD}Total Matches:{RESET}  {MAGENTA}{total_results}{RESET}")
    print("-" * 80)
    
    if not matches:
        print(f"\n  {RED}[!] No matching hosts found in Shodan for this query.{RESET}\n")
        print("=" * 80)
        return
        
    headers = {
        "product": ("Products", BLUE),
        "os": ("Operating Systems", GREEN),
        "server": ("Servers", CYAN),
        "org": ("Organizations", MAGENTA)
    }
    
    for key, (label, color) in headers.items():
        field_info = filtered_data[key]
        unique_vals = field_info["unique_values"]
        freqs = field_info["frequencies"]
        total = field_info["total_occurrences"]
        
        print(f"\n  {BOLD}{color}■ {label}{RESET} ({total} detections):")
        if not unique_vals:
            print(f"    {RESET}No data found for this field.")
            continue
            
        print(f"    ┌──────────────────────────────────────────────┬───────────────┐")
        print(f"    │ Value                                        │ Count         │")
        print(f"    ├──────────────────────────────────────────────┼───────────────┤")
        for val in unique_vals[:5]:
            truncated_val = val[:42].ljust(44)
            count_str = str(freqs[val]).rjust(13)
            print(f"    │ {truncated_val} │ {count_str} │")
        if len(unique_vals) > 5:
            remaining = len(unique_vals) - 5
            print(f"    ├──────────────────────────────────────────────┼───────────────┤")
            print(f"    │ ... and {remaining} more unique value(s)".ljust(44) + " │               │")
        print(f"    └──────────────────────────────────────────────┴───────────────┘")
    print("\n" + "=" * 80 + "\n")


# Beautiful Terminal Breach Dashboard
def print_breach_report(local_breaches, web_breaches):
    RED = "\033[91m"
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    BOLD = "\033[1m"
    RESET = "\033[0m"
    CYAN = "\033[96m"
    
    print("=" * 80)
    print(f" {BOLD}{RED}🚨 CYBER SECURITY BREACH & ATTACK HISTORY 🚨{RESET}".center(90))
    print("=" * 80)
    
    has_any = False

    # 1. Print Ransomware.live API matches
    if local_breaches:
        has_any = True
        print(f"\n  {BOLD}{RED}⚠️ VERIFIED RANSOMWARE.LIVE API MATCHES:{RESET}")
        for idx, match in enumerate(local_breaches, 1):
            print(f"    ┌─ [Match #{idx}] ──────────────────────────────────────────────────────────┐")
            print(f"    │ {BOLD}Victim Name:{RESET}   {match.get('victim')}".ljust(85) + "│")
            print(f"    │ {BOLD}Threat Actor:{RESET}  {RED}{match.get('threat_actor') or 'Unknown'}{RESET}".ljust(94) + "│")
            print(f"    │ {BOLD}Attack Date:{RESET}   {YELLOW}{match.get('attack_date') or 'N/A'}{RESET}".ljust(94) + "│")
            print(f"    │ {BOLD}Activity/Sec:{RESET}  {match.get('activity') or 'N/A'} ({match.get('country') or 'N/A'})".ljust(85) + "│")
            desc = match.get('description') or ''
            truncated_desc = desc[:65].replace("\n", " ")
            if len(desc) > 65:
                truncated_desc += "..."
            print(f"    │ {BOLD}Details:{RESET}       {truncated_desc}".ljust(85) + "│")
            link = match.get('permalink') or ''
            truncated_link = link[:65]
            if len(link) > 65:
                truncated_link += "..."
            print(f"    │ {BOLD}Reference:{RESET}     {CYAN}{truncated_link}{RESET}".ljust(94) + "│")
            print(f"    └───────────────────────────────────────────────────────────────────────────┘")
            
    # 2. Print Web synthesized matches
    web_attacks = web_breaches.get("attacks") or []
    if web_attacks:
        has_any = True
        print(f"\n  {BOLD}{YELLOW}🔍 SYNTHESIZED WEB INTEL INCIDENT REPORTS:{RESET}")
        for idx, attack in enumerate(web_attacks, 1):
            print(f"    ┌─ [Incident #{idx}] ───────────────────────────────────────────────────────┐")
            print(f"    │ {BOLD}Attack Type:{RESET}   {RED}{attack.get('attack_type') or 'Unknown'}{RESET}".ljust(94) + "│")
            print(f"    │ {BOLD}Threat Actor:{RESET}  {attack.get('threat_actor') or 'Unknown'}".ljust(85) + "│")
            print(f"    │ {BOLD}Incident Date:{RESET} {YELLOW}{attack.get('attack_date') or 'N/A'}{RESET}".ljust(94) + "│")
            details = attack.get('details') or ''
            truncated_details = details[:65].replace("\n", " ")
            if len(details) > 65:
                truncated_details += "..."
            print(f"    │ {BOLD}Summary:{RESET}       {truncated_details}".ljust(85) + "│")
            refs = attack.get('reference_urls') or []
            ref_str = refs[0] if refs else 'N/A'
            truncated_ref = ref_str[:65]
            if len(ref_str) > 65:
                truncated_ref += "..."
            print(f"    │ {BOLD}Primary Ref:{RESET}   {CYAN}{truncated_ref}{RESET}".ljust(94) + "│")
            print(f"    └───────────────────────────────────────────────────────────────────────────┘")
            
    if not has_any:
        print(f"\n  {BOLD}{GREEN}✅ No record of historical cyber attacks or ransomware listings found.{RESET}")
        print("  This company currently appears clean across searched threat intelligence databases.")
        
    print("\n" + "=" * 80 + "\n")

# Beautiful Terminal crt.sh Report
def print_crtsh_report(crtsh_results: dict):
    print("=" * 80)
    print(f" {BOLD}{CYAN}🔒 CRT.SH CERTIFICATE TRANSPARENCY LOG REPORT{RESET}".center(90))
    print("=" * 80)
    
    total = crtsh_results.get("total_count", 0)
    sensitive = crtsh_results.get("sensitive_matches", [])
    error = crtsh_results.get("error")
    
    if error:
        print(f"\n  {RED}[!] crt.sh query error: {error}{RESET}")
        print("=" * 80 + "\n")
        return
    
    print(f"\n  {BOLD}Total Unique Subdomains Found:{RESET} {MAGENTA}{total}{RESET}")
    
    if sensitive:
        print(f"\n  {BOLD}{YELLOW}⚠ SENSITIVE SUBDOMAINS WITH SECURITY KEYWORDS:{RESET}")
        for idx, match in enumerate(sensitive, 1):
            domain = match.get("domain", "")
            keywords = ", ".join(match.get("keywords", []))
            print(f"    [{idx}] {RED}{domain}{RESET} → Keywords: {GREEN}{keywords}{RESET}")
    else:
        print(f"\n  {GREEN}✅ No sensitive keyword matches found in subdomains.{RESET}")
    
    print("\n" + "=" * 80 + "\n")

# ── Victim Profile Matching & Technology Verification Helpers ───────────────────

def load_victim_profile(victim_path: Path) -> dict | None:
    try:
        with open(victim_path, encoding="utf-8") as f:
            data = json.load(f)
        
        # Extract security stack details
        tools = []
        sec_stack = data.get("security_stack") or []
        for item in sec_stack:
            vendor = item.get("vendor")
            product = item.get("product")
            raw = item.get("raw")
            if vendor and vendor not in tools: tools.append(vendor)
            if product and product not in tools: tools.append(product)
            if raw and raw not in tools: tools.append(raw)
            
        # Extract technology_text from target.raw
        tech_text = ""
        target_data = data.get("target") or {}
        raw_target = target_data.get("raw") or {}
        tech_text = (raw_target.get("technology_text") or "").strip()

        # Extract additional keywords from technology_text
        tech_keywords = []
        if tech_text:
            for line in tech_text.replace(",", "\n").split("\n"):
                term = line.strip()
                if term and term not in tools:
                    tech_keywords.append(term)

        # Extract country & sector
        payload = data.get("search_payload", {})
        country = payload.get("country")
        sector = payload.get("sector", [])
        if isinstance(sector, list) and sector:
            sector = sector[0]
        elif isinstance(sector, str):
            sector = sector
            
        # Determine if tech indicators exist (gates Steps 4 & 5)
        has_tech = bool(sec_stack) or bool(tech_text)

        return {
            "security_tools": list(set([t.lower().strip() for t in tools if t])),
            "technology_keywords": [t.lower().strip() for t in tech_keywords if t],
            "technology_text": tech_text,
            "has_tech_indicators": has_tech,
            "country": country.lower().strip() if country else None,
            "sector": sector.lower().strip() if isinstance(sector, str) and sector else None,
            "raw_tools": tools # Keep original casing
        }
    except Exception as e:
        print(f"[!] Failed to parse victim profile: {e}")
        return None

def perform_matching_analysis(victim: dict, shodan_summary: dict, local_breaches: list, web_breaches: dict, crtsh_results: dict | None = None) -> dict:
    analysis = {
        "security_tools_matched": [],
        "security_tools_matched_crtsh": [],
        "country_matched": False,
        "sector_matched": False,
        "breaches_matched": False,
        "match_score": 0,
        "max_score": 0
    }
    
    # 1. Match Security Tools
    shodan_vals = []
    for k in ["product", "os", "server", "org"]:
        shodan_vals.extend([v.lower() for v in shodan_summary.get(k, [])])
        
    for tool in victim["security_tools"]:
        analysis["max_score"] += 2
        # Check if the tool is present in Shodan findings (ignoring empty strings)
        matched_in_shodan = any(
            (tool in val or val in tool) 
            for val in shodan_vals 
            if val and val.strip() != ""
        )
        if matched_in_shodan:
            analysis["security_tools_matched"].append(tool)
            analysis["match_score"] += 2

            
    # 2. Match Security Tools against crt.sh CT log subdomains
    if crtsh_results and crtsh_results.get("sensitive_matches"):
        crtsh_keywords_found = set()
        for match in crtsh_results["sensitive_matches"]:
            for kw in match.get("keywords", []):
                crtsh_keywords_found.add(kw.lower())
        
        all_victim_terms = victim["security_tools"] + victim.get("technology_keywords", [])
        for ckw in crtsh_keywords_found:
            for term in all_victim_terms:
                if ckw in term or term in ckw:
                    if ckw not in analysis["security_tools_matched_crtsh"]:
                        analysis["security_tools_matched_crtsh"].append(ckw)
                        analysis["match_score"] += 1
                    break

    # 3. Match Cyber Incidents / Breaches
    analysis["max_score"] += 3
    has_breach = bool(local_breaches) or (web_breaches.get("has_attack", False))
    if has_breach:
        analysis["breaches_matched"] = True
        analysis["match_score"] += 3
        
    return analysis

def print_matching_report(analysis: dict, victim: dict, company_name: str):
    print("\n" + "="*80)
    print(f" {BOLD}{BLUE}🔍 VICTIM PROFILE MATCHING ANALYSIS FOR: {company_name.upper()}{RESET} ")
    print("="*80)
    
    # Tech stack matches
    print(f"\n  {BOLD}🛡️ Security Tech Stack Overlap:{RESET}")
    if analysis["security_tools_matched"]:
        for tool in analysis["security_tools_matched"]:
            print(f"    {GREEN}✓ MATCH FOUND:{RESET} '{tool.title()}' detected in active Shodan footprints (+2 pts)")
    else:
        print(f"    {RED}✗ No matching security tools found in active Shodan footprints.{RESET}")
        
    # crt.sh CT log matches
    print(f"\n  {BOLD}🔒 crt.sh Certificate Transparency Keyword Overlap:{RESET}")
    if analysis.get("security_tools_matched_crtsh"):
        for kw in analysis["security_tools_matched_crtsh"]:
            print(f"    {GREEN}✓ MATCH FOUND:{RESET} Keyword '{kw}' found in crt.sh subdomains AND victim tech stack (+1 pt)")
    else:
        print(f"    {YELLOW}⚠ No crt.sh keyword ↔ victim tech stack overlap found.{RESET}")
    
    # Breach/Ransomware matches
    print(f"\n  {BOLD}💥 Cyber Breach / Ransomware History:{RESET}")
    if analysis["breaches_matched"]:
        print(f"    {GREEN}✓ MATCH FOUND:{RESET} Candidate company has records of cyber attacks / ransomware listings! (+3 pts)")
    else:
        print(f"    {YELLOW}⚠ No breach records or ransomware incidents identified.{RESET}")
        
    # Final Score
    score = analysis["match_score"]
    max_score = analysis["max_score"]
    percent = (score / max_score * 100) if max_score > 0 else 0
    print(f"\n  {BOLD}📈 Overall Candidate Match Score:{RESET} {CYAN}{score} / {max_score} ({percent:.1f}% Match){RESET}")
    print("\n" + "="*80 + "\n")

def print_consolidated_matching_table(results: list, victim: dict):
    print("\n" + "=" * 115)
    print(f" {BOLD}{CYAN}🏆 CONSOLIDATED CANDIDATE MATCHING & TECHNOLOGY VERIFICATION TABLE{RESET} ".center(125))
    print("=" * 115)
    print(f" {BOLD}Target Profile Details:{RESET}")
    print(f"   • Country:      {victim['country'].title() if victim['country'] else 'N/A'}")
    print(f"   • Sector:       {victim['sector'].title() if victim['sector'] else 'N/A'}")
    print(f"   • Tech Stack:   {', '.join([t.title() for t in victim['raw_tools']])}")
    print("-" * 115)
    
    # Headers
    print(f"  {BOLD}{'Candidate Company':<28} │ {'Tools Matched':<26} │ {'Breach':<8} │ {'Score':<10} │ {'Verdict':<15}{RESET}")
    print(f"  {'─'*28}─┼─{'─'*26}─┼─{'─'*8}─┼─{'─'*10}─┼─{'─'*15}")
    
    for r in results:
        comp_name = r["company"][:28].ljust(28)
        shodan_tools = r["analysis"]["security_tools_matched"]
        crtsh_tools = r["analysis"].get("security_tools_matched_crtsh", [])
        tools = shodan_tools + [t for t in crtsh_tools if t not in shodan_tools]
        matched_str = f"{len(tools)}/{len(victim['security_tools'])} ({', '.join([t.title() for t in tools[:2]])})"
        if len(tools) > 2:
            matched_str += "..."
        matched_str = matched_str[:26].ljust(26)
        
        breach_status = f"{GREEN}Yes{RESET}" if r["analysis"]["breaches_matched"] else f"{RED}No{RESET}"
        breach_str = breach_status.ljust(17) # Adjust for ANSI escape chars len
        
        score_val = r["analysis"]["match_score"]
        max_val = r["analysis"]["max_score"]
        score_str = f"{score_val} / {max_val}".ljust(10)
        
        pct = (score_val / max_val * 100) if max_val > 0 else 0
        if pct >= 70:
            verdict = f"{BOLD}{GREEN}★ Strong Match{RESET}"
        elif pct >= 30:
            verdict = f"{BOLD}{YELLOW}◆ Partial Match{RESET}"
        else:
            verdict = f"{BOLD}{RED}✗ No Match{RESET}"
            
        print(f"  {comp_name} │ {matched_str} │ {breach_str} │ {score_str} │ {verdict}")
        
    print("=" * 115 + "\n")

def run_footprint_for_company(company_name: str | None, domain: str | None, query: str | None, shodan_key: str, run_breach: bool, cfg: dict, victim_profile: dict | None = None) -> dict:
    # Resolve domain if not provided
    if not domain and company_name:
        country = victim_profile.get("country") if victim_profile else None
        domain = resolve_domain(company_name.strip(), cfg, country=country)

        
    if not query:
        if domain:
            # Robust SLD extraction
            parts = domain.lower().strip().split(".")
            cc_tlds = {"co", "com", "org", "net", "gov", "edu", "ac", "sch", "res"}
            if len(parts) >= 3 and parts[-2] in cc_tlds:
                sld = parts[-3]
            elif len(parts) >= 2:
                sld = parts[-2]
            else:
                sld = domain
            query = f'ssl:"{sld}"'
        elif company_name:
            fallback_domain = "".join(c for c in company_name if c.isalnum()).lower().strip()
            query = f'ssl:"{fallback_domain}"'
        else:
            raise ValueError("Must specify company name, domain, or custom query.")

            
    # Determine if tech/security indicators exist (gates Shodan & crt.sh)
    run_tech_recon = True
    if victim_profile is not None:
        run_tech_recon = victim_profile.get("has_tech_indicators", False)
    
    # Execute Shodan search (only if tech indicators present)
    if run_tech_recon:
        print("tech products exist...")
        print("shodan search starts...")
        try:
            raw_results = query_shodan(query, shodan_key)
        except Exception:
            raw_results = {"total": 0, "matches": []}
    else:
        print("tech products do not exist...")
        raw_results = {"total": 0, "matches": []}
        
    total_results = raw_results.get("total", 0)
    matches = raw_results.get("matches", [])
    
    # Filter key fields recursively
    filtered_data = extract_shodan_fields(raw_results)
    
    # Extract unique values for clean, simple shodan summary
    shodan_summary = {
        "product": filtered_data.get("product", {}).get("unique_values", []),
        "os": filtered_data.get("os", {}).get("unique_values", []),
        "server": filtered_data.get("server", {}).get("unique_values", []),
        "org": filtered_data.get("org", {}).get("unique_values", []),
    }
    
    if run_tech_recon:
        if shodan_summary["product"]:
            print(f"match found: {', '.join(shodan_summary['product'])}")
        else:
            print("match not found")
            
    local_breaches = []
    web_breaches = {"has_attack": False, "attacks": []}
    
    # Execute crt.sh CT log search (only if tech indicators present and domain resolved)
    crtsh_results = {"all_subdomains": [], "sensitive_matches": [], "total_count": 0}
    if run_tech_recon and domain:
        crtsh_results = query_crtsh(domain)
        # Process and print matching keywords from crt.sh
        if crtsh_results.get("sensitive_matches"):
            matched_kws = sorted(list(set([kw for m in crtsh_results["sensitive_matches"] for kw in m["keywords"]])))
            print(f"match found: {', '.join(matched_kws)}")
        else:
            print("match not found...")
    elif not run_tech_recon:
        pass
    
    if run_breach:
        # Search Ransomware.live API using the configured key
        ransomware_key = (cfg.get("api_keys", {}).get("ransomware_live", "") or "").strip()
        if ransomware_key:
            local_breaches = check_ransomware_live_api(company_name, domain, ransomware_key)
        else:
            print("  [!] Missing Ransomware.live API Key in config.yaml. Skipping API lookup.")
            
        # Search web for breaches
        web_breaches = check_web_breaches(domain, company_name or domain, cfg)
        
    return {
        "domain": domain,
        "query": query,
        "total_results": total_results,
        "matches": matches,
        "filtered_data": filtered_data,
        "shodan_summary": shodan_summary,
        "local_breaches": local_breaches,
        "web_breaches": web_breaches,
        "crtsh_results": crtsh_results,
        "raw_results": raw_results
    }


def main():
    ap = argparse.ArgumentParser(description="Query Shodan with domain SSL certificate and recursively filter key attributes. Also audits breach history.")
    ap.add_argument("--company", type=str, default=None, help="Name of company to resolve and search.")
    ap.add_argument("--domain", type=str, default=None, help="Direct domain name (skips resolution phase).")
    ap.add_argument("--query", type=str, default=None, help="Direct custom Shodan search query.")
    ap.add_argument("--key", type=str, default=None, help="Shodan API key override.")
    ap.add_argument("--no-breach", action="store_true", help="Disable cyber breach and ransomware history checks (enabled by default).")
    ap.add_argument("--output", type=Path, default=None, help="File path to save the detailed JSON output.")
    ap.add_argument("--candidate-list", type=Path, default=None, help="Path to a JSON list of candidate companies to evaluate in batch.")
    ap.add_argument("--victim-json", type=Path, default=None, help="Path to the victim's parsed JSON file for matching/overlap analysis.")
    args = ap.parse_args()

    # Load configuration
    cfg = load_config()
    shodan_key = args.key or (cfg.get("api_keys", {}).get("shodan", "") or "").strip() or os.getenv("SHODAN_API_KEY")
    
    if not shodan_key:
        print("[!] Error: Missing Shodan API Key. Set it in config.yaml (api_keys.shodan) or environment variable SHODAN_API_KEY.", file=sys.stderr)
        sys.exit(1)

    run_breach = not args.no_breach

    # Load victim profile if provided
    victim = None
    if args.victim_json:
        if not args.victim_json.exists():
            print(f"[!] Error: Victim JSON file not found: {args.victim_json}", file=sys.stderr)
            sys.exit(1)
        victim = load_victim_profile(args.victim_json)
        if not victim:
            print(f"[!] Error: Could not load victim profile from {args.victim_json}", file=sys.stderr)
            sys.exit(1)

    # ────────────────────────────────────────────────────────────────────────────
    # Case A: Batch Candidate Evaluation Mode
    # ────────────────────────────────────────────────────────────────────────────
    if args.candidate_list:
        if not args.candidate_list.exists():
            print(f"[!] Error: Candidate list file not found: {args.candidate_list}", file=sys.stderr)
            sys.exit(1)
        if not victim:
            print("[!] Error: You must specify --victim-json when running in batch mode.", file=sys.stderr)
            sys.exit(1)
            
        with open(args.candidate_list, encoding="utf-8") as f:
            try:
                candidates = json.load(f)
            except json.JSONDecodeError:
                print(f"[!] Error: Candidate list '{args.candidate_list}' is not valid JSON.", file=sys.stderr)
                sys.exit(1)
                
        if not isinstance(candidates, list):
            print("[!] Error: Candidate list must be a JSON array of strings.", file=sys.stderr)
            sys.exit(1)
            
        print(f"\n[>] Starting Batch OSINT Stage & Matching for {len(candidates)} candidates...")
        results = []
        
        for idx, candidate in enumerate(candidates, 1):
            print(f"\n{'='*70}")
            print(f"  [{idx}/{len(candidates)}] Processing Candidate: {BOLD}{candidate}{RESET}")
            print(f"{'='*70}")
            
            run_res = run_footprint_for_company(candidate, None, None, shodan_key, run_breach, cfg, victim_profile=victim)
            
            # Print dashboards for this candidate
            print_dashboard(run_res["domain"], run_res["query"], run_res["total_results"], run_res["matches"], run_res["filtered_data"])
            if run_breach:
                print_breach_report(run_res["local_breaches"], run_res["web_breaches"])
            
            # Print crt.sh report
            if run_res.get("crtsh_results", {}).get("total_count", 0) > 0:
                print_crtsh_report(run_res["crtsh_results"])
                
            # Perform matching analysis
            analysis = perform_matching_analysis(victim, run_res["shodan_summary"], run_res["local_breaches"], run_res["web_breaches"], run_res.get("crtsh_results"))
            print_matching_report(analysis, victim, candidate)
            
            results.append({
                "company": candidate,
                "domain": run_res["domain"],
                "query": run_res["query"],
                "total_results": run_res["total_results"],
                "shodan_summary": run_res["shodan_summary"],
                "local_breaches": run_res["local_breaches"],
                "web_breaches": run_res["web_breaches"],
                "analysis": analysis
            })
            
            # Save candidate individual JSON report
            safe_name = "".join(c for c in candidate if c.isalnum() or c in (' ', '_', '-')).rstrip()
            safe_name = safe_name.replace(" ", "_")
            cand_out_path = SCRIPT_DIR / "outputs" / f"{safe_name}_shodan.json"
            cand_out_path.parent.mkdir(parents=True, exist_ok=True)
            
            cand_payload = {
                "company_name": candidate,
                "resolved_domain": run_res["domain"],
                "shodan_query": run_res["query"],
                "total_matches": run_res["total_results"],
                "shodan_summary": run_res["shodan_summary"],
                "victim_matching_report": analysis,
                "crtsh_results": run_res.get("crtsh_results")
            }
            if run_breach:
                cand_payload["breach_report"] = {
                    "local_ransomware_matches": run_res["local_breaches"],
                    "web_synthesized_breaches": run_res["web_breaches"],
                }
            with open(cand_out_path, "w", encoding="utf-8") as f:
                json.dump(cand_payload, f, indent=2, ensure_ascii=False)
            print(f"[OK] Saved detailed JSON report to: {cand_out_path}")
            
            # Cooldown sleep to respect search engines / Gemini quota
            if idx < len(candidates):
                print(f"\n[~] Cooling down for 20 seconds before next candidate...")
                time.sleep(20)
                
        # 1. Print Consolidated Table
        print_consolidated_matching_table(results, victim)
        
        # 2. Save master summary report
        master_out_path = SCRIPT_DIR / "outputs" / "candidate_matching_summary.json"
        master_payload = {
            "evaluation_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "victim_profile": {
                "source_file": str(args.victim_json),
                "country": victim["country"],
                "sector": victim["sector"],
                "security_tools": victim["raw_tools"]
            },
            "candidates_evaluated": [
                {
                    "company_name": r["company"],
                    "domain": r["domain"],
                    "shodan_query": r["query"],
                    "total_matches": r["total_results"],
                    "match_score": r["analysis"]["match_score"],
                    "max_score": r["analysis"]["max_score"],
                    "tools_matched": r["analysis"]["security_tools_matched"],
                    "breaches_matched": r["analysis"]["breaches_matched"]
                } for r in results
            ]
        }
        with open(master_out_path, "w", encoding="utf-8") as f:
            json.dump(master_payload, f, indent=2, ensure_ascii=False)
        print(f"[OK] Master match summary report saved to: {master_out_path}")
        
    # ────────────────────────────────────────────────────────────────────────────
    # Case B: Single Target Mode
    # ────────────────────────────────────────────────────────────────────────────
    elif args.company or args.domain or args.query:
        comp_name = args.company
        # Resolve domain and query first to show in terminal
        domain = args.domain
        query = args.query
        
        print(f"\n[>] Running Footprint & Cyberattack Intelligence for single target...")
        run_res = run_footprint_for_company(comp_name, domain, query, shodan_key, run_breach, cfg, victim_profile=victim)
        
        # Print dashboards
        print_dashboard(run_res["domain"], run_res["query"], run_res["total_results"], run_res["matches"], run_res["filtered_data"])
        if run_breach:
            print_breach_report(run_res["local_breaches"], run_res["web_breaches"])
        
        # Print crt.sh report
        if run_res.get("crtsh_results", {}).get("total_count", 0) > 0:
            print_crtsh_report(run_res["crtsh_results"])

        analysis = None
        if victim:
            analysis = perform_matching_analysis(victim, run_res["shodan_summary"], run_res["local_breaches"], run_res["web_breaches"], run_res.get("crtsh_results"))
            print_matching_report(analysis, victim, comp_name or run_res["domain"] or "Custom Query")
            
        # Save output JSON
        out_path = args.output
        if not out_path and comp_name:
            safe_name = "".join(c for c in comp_name if c.isalnum() or c in (' ', '_', '-')).rstrip()
            safe_name = safe_name.replace(" ", "_")
            out_path = SCRIPT_DIR / "outputs" / f"{safe_name}_shodan.json"
            
        if out_path:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            output_payload = {
                "company_name": comp_name,
                "resolved_domain": run_res["domain"],
                "shodan_query": run_res["query"],
                "total_matches": run_res["total_results"],
                "shodan_summary": run_res["shodan_summary"],
                "crtsh_results": run_res.get("crtsh_results")
            }
            if victim and analysis:
                output_payload["victim_matching_report"] = analysis
            if run_breach:
                output_payload["breach_report"] = {
                    "local_ransomware_matches": run_res["local_breaches"],
                    "web_synthesized_breaches": run_res["web_breaches"],
                }
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(output_payload, f, indent=2, ensure_ascii=False)
            print(f"[OK] Saved detailed JSON report to: {out_path}")
            
    else:
        print("[!] Error: You must specify --company <name>, --domain <domain>, --query <query>, or --candidate-list <path>.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
