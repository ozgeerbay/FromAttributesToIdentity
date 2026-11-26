# Slim Enriched Output

Output of `company_enrich.py` is a minimal JSON for the next stage (company name search). Fields:

| Field | Type | Description |
|-------|------|-------------|
| `country` | string \| null | Country (ISO name) |
| `sector` | string \| null | Sector / industry |
| `revenue_usd` | number \| null | Annual revenue (USD) |
| `revenue_annual_original` | string \| null | Raw revenue text |
| `revenue_source` | string \| null | Source (llm, regex, listing, zoominfo, …) |
| `revenue_selection_reasoning` | string \| null | Why `revenue_usd` was chosen, or why it was left null (ambiguous entity, contradictory magnitudes, no eligible row). |
| `revenue_entity_ambiguous` | bool \| null | True when sources mix several namesake companies; scalar `revenue_usd` is then suppressed. |
| `revenue_values` | array | Rows with `value_usd`, `raw`, `source`, and (from LLM) `line_type`, `entity_match`, `entity_name`. Primary scalar only uses `operating_revenue` + `target` + explicit scale in `raw`. See `revenue_contradiction_ratio` and `revenue_merge_regex` in `config.yaml`. |
| `employees` | number \| null | Employee count (from enrichment or listing) |
| `headquarters` | string \| null | HQ location |
| `security_stack` | array \| null | From parsed listing if present |
| `technology_text` | string \| null | From parsed listing `target.raw.technology_text` if present |
| `enriched_at` | string | ISO timestamp |

## Example

```json
{
  "country": "France",
  "sector": "Technology",
  "revenue_usd": 15600000,
  "revenue_annual_original": "$15.60M",
  "revenue_source": "regex",
  "revenue_values": [],
  "employees": 120,
  "headquarters": "Paris, France",
  "security_stack": ["AWS", "Okta"],
  "technology_text": "Cloud infrastructure, SIEM",
  "enriched_at": "2025-03-03T12:00:00+00:00"
}
```
