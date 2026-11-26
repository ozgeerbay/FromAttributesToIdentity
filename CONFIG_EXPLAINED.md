# Config and Search Explanation

## enrichment_fields (config.yaml)

**Purpose:** If the listing (parsed JSON) already contains country, sector, revenue, etc., these paths are used to extract them. **NOT web search.**

**Example:** `target.interpreted.country` → country field from listing output.

If present in listing, this data is used; otherwise web search is performed.

---

## Web Search Queries (company_enrich.py)

**Location:** `company_enrich.py` → `_run_search()` function — defined in **one place only**. Not read from config.

**Queries (order):**
1. `{company}` — simplest (as you would type in manual search)
2. `{company} company`
3. `{company} company country headquartered in`
4. `{company} country`
5. `{company} company industry sector`
6. `{company} industry`
7. `{company} sector`
8. `{company} revenue`
9. ... (headquarters, linkedin, zoominfo, etc.)

---

## Why Manual Finds It, Automatic Doesn't?

1. **Result count:** Default was 8, now 15. `config.yaml` → `searxng_max_results: 15` (or more)
2. **First query:** `{company}` added — simple query like manual search
3. **Content field:** SearXNG `content`, `snippet`, `description`, `body` fields are checked

**Debug:** Run with `--save-raw`, inspect `_search_raw` in output for raw SearXNG data.

```bash
python company_enrich.py --json results_with_names/1.json --save-raw --output enriched/1_debug.json
```
