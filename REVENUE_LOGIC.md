# Revenue Extraction Logic — How It Works

## 1. Source

Revenue values come from **web search** results (Gemini, Ollama, SearXNG):
- When country is known: `"X France revenue"` (better results for smaller companies)
- When country is unknown: `"X revenue"`
- Gemini: google_search tool + grounding metadata
- Ollama: cloud web search
- SearXNG: meta search (self-hosted)

**Regex** runs over these texts (`extract_revenue_patterns`). All patterns like `$X billion`, `revenue: X`, `€X million` are captured.

**Excluded:** Acquisition price, deal value, purchase price (not revenue). Lower bounds like "revenue over $100M" (threshold, not actual figure).

**Listing priority:** Config-driven (`config.yaml` → `enrichment_fields`). When the listing has data for a field (country, sector, revenue), it wins. Research fills gaps only. No hardcoded field names.

## 2. Why Multiple Values?

- Search results contain text from **multiple sources** (different sites, years, companies)
- Regex has **no context** — it cannot tell which revenue belongs to which company
- E.g. "Mint Mobile revenue" search may include T-Mobile ($66B), Verizon ($135B) etc. in snippets
- So `revenue_values` can be **mixed**

## 3. How Is revenue_annual_original Selected?

A single "best" value is chosen by this ordering:

```python
sorted(regex_revs, key=lambda x: (x.get("year") or "0", x.get("value_usd") or 0), reverse=True)[0]
```

- **First:** `year` descending (2025 > 2024 > 2023 > null)
- **Then:** `value_usd` descending

So: **most recent year + highest amount** is selected.

**Issue:** This value may come from the wrong company (e.g. T-Mobile $250B, not Mint Mobile).

## 4. Multiple Companies (Mint Mobile & Ultra Mobile)

- Each company is searched **separately**
- But `revenue_usd` / `revenue_annual_original` picks a single "best" from all found values
- So parent (T-Mobile) or another company's revenue may be selected

**Correct source:** `enriched.companies` — separate `revenue_usd` and `revenue_values` per company.

## 5. Per-Value Metadata (source, company_search)

Each `revenue_values[]` entry now includes:
- **source**: Backend and query (e.g. `Gemini|Acme Corp revenue`, `SearXNG|Acme revenue`)
- **company_search**: Which company we were searching when this value was found (multi-company only)
- **raw_normalized**: Human-readable format for huge values (e.g. `$112.1B` instead of `$112097.4 Million`)

## 6. Summary

| Field | Description |
|-------|-------------|
| `revenue_values` | All values with source, company_search, raw_normalized |
| `revenue_usd` | Single value chosen by sort (year↑, value↓) |
| `revenue_annual_original` | Normalized display (e.g. "$112.1B") when raw is confusing |
| `companies` | Per-company revenue with source attribution |

**Recommendation:** For multiple companies, use `companies[].revenue_usd`; check `source` and `company_search` to verify attribution.
