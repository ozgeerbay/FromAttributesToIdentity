# Company Enrich — Current Logic

This document reflects the current behavior of `company_enrich.py`.

---

## 1) Purpose

Enrich company intelligence from parsed listing data + web search, producing fields such as:
- `country`, `sector`, `headquarters`, `employees`
- `revenue_usd`, `revenue_values`, `revenue_selection_reasoning`
- `_raw_sources` (debug/audit trail)

---

## 2) Inputs

- `--json`: post-parser output (preferred)
- `--text` / `--stdin`: raw text mode

For JSON mode, clues are built from:
- `search_payload`
- `target.interpreted`
- `target.raw`
- `config.yaml -> enrichment_fields` (this becomes `listing_data`)

---

## 3) High-Level Flow

1. Build clues (`clues_from_parsed` or `extract_clues`)
2. Fetch 3 sources in parallel where possible:
   - Gemini model direct (`gemini_enrich_direct`, no web tool)
   - Web search (`_run_search`) via configured backends
3. LLM extraction over combined data
4. Revenue post-processing (`apply_deterministic_primary_revenue`)
5. Listing overrides + required field normalization
6. Final slim output + raw sources

---

## 4) Query Strategy (Important)

`_run_search()` uses **different query families**:

### A) Gemini/Ollama queries (`search_queries`)
Natural language only (no `site:` operators), e.g.:
- `{company}`
- `{company} company`
- `{company} country`, `{company} industry`
- `{company} revenue` (only when revenue queries not skipped)
- `{company} headquarters location`
- `{company} number of employees`

### B) SearXNG queries (`sx_queries`)
Quoted + operator-based exact queries, e.g.:
- `"{company}" company`
- `"{company}" site:zoominfo.com revenue`
- `"{company}" site:linkedin.com industry`
- `"{company}" site:zoominfo.com company size`

---

## 5) Revenue Query Skip Behavior

Config key:
- `skip_revenue_queries_if_listing_has_revenue` (in `config.yaml`)

When `true`:
- If `listing_data.revenue_usd` exists (numeric), revenue-specific web queries are skipped.
- This affects both Gemini web search and SearXNG revenue-focused query generation.

Additionally, extraction prompt enforces:
- **REVENUE EXTRACTION SKIP (STRICT)** in that run:
  - ignore revenue mentions from combined text
  - output `revenue_values: []`
  - final scalar revenue can still come from listing override

---

## 6) Source Combination for Extraction

Before LLM extraction:
- SearXNG profile snippets (`site:zoominfo`, `site:linkedin`, `site:crunchbase`) are lifted to a high-priority section first.
- Then Gemini direct text.
- Then remaining Gemini/SearXNG blocks.

This is designed to reduce noise from long narrative blocks and prioritize profile-like facts.

---

## 7) Extraction Rules

Single-company extraction prompt asks for:
- `country`, `sector`, `revenue_values`, `headquarters`, `employees`

Country/HQ logic prioritizes:
1. ZoomInfo/Crunchbase/Craft profile lines
2. LinkedIn HQ lines
3. Official site address pages
4. Founded/based authoritative lines
5. General web mentions tied to target

For decentralized entities:
- prefers founding/registered location when no single HQ.

---

## 8) Revenue Selection Logic (Post-Extraction)

`apply_deterministic_primary_revenue()`:
- normalizes rows
- filters primary-eligible candidates by line/entity rules
- blocks scalar output when:
  - entity ambiguity is true, or
  - eligible values are contradictory beyond configured ratio (`revenue_contradiction_ratio`)
- selects one primary candidate via source/year/value tie-breaks

If no valid primary:
- `revenue_usd` remains `null`
- reasoning explains why
- `revenue_values` retained for audit

---

## 9) Listing Overrides

`apply_listing_overrides()` can overwrite enrichment output with `listing_data` for:
- `country`, `sector`, `employees`, `headquarters`, `revenue_usd`, `revenue_raw`

For revenue override:
- sets `revenue_source = "listing"`
- injects a listing row into `revenue_values`

This is why listing-derived revenue may dominate final scalar output.

---

## 10) Multi-Company Mode

If multiple names exist in `company_names`:
- each company is searched separately
- per-company revenues extracted
- aggregate revenue decision made from merged candidates
- `companies[]` preserves per-company details

---

## 11) Output Shape (Current)

Top-level fields (not nested under `enriched`), e.g.:
- `country`
- `sector`
- `revenue_usd`
- `revenue_annual_original`
- `revenue_source`
- `revenue_selection_reasoning`
- `revenue_entity_ambiguous`
- `revenue_values`
- `employees`
- `headquarters`
- `_raw_sources` (contains model/search/raw audit text)

---

## 12) Debugging Flags

- `company_enrich.py --debug-raw` prints raw Gemini and extraction responses.
- `pipeline.py --debug-raw` now forwards the same flag to `company_enrich.py`.
