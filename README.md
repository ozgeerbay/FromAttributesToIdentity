# Dark Web Anonymous Victim Identification Pipeline

This repository contains a Python pipeline for converting unstructured ransomware listing evidence into structured attributes, ranking likely victim companies with repeated LLM judgments, and optionally collecting passive OSINT signals.

The project was developed for thesis research. It is designed to process:

- local screenshots or images;
- text files;
- Twitter/X status URLs; and
- previously parsed JSON.

## Project overview

The pipeline has four stages:

1. **Parse** — [`post_parser.py`](post_parser.py) extracts schema-constrained JSON from an image, text, or tweet.
2. **Extract/enrich** — [`company_enrich.py`](company_enrich.py) and [`e2e_pipeline.py`](e2e_pipeline.py) extract company clues and can enrich them using SearXNG or supported LLM search providers.
3. **Identify** — [`e2e_pipeline.py`](e2e_pipeline.py) requests ranked candidate companies multiple times and aggregates the results with Borda count.
4. **Assess** — [`osint_stage.py`](osint_stage.py) optionally resolves domains and checks Shodan, crt.sh, Ransomware.live, and breach/news search sources.

The modular scripts can be run separately. [`pipeline.py`](pipeline.py) provides parse-and-enrich workflows, including batch processing.

## Repository contents

- [`e2e_pipeline.py`](e2e_pipeline.py) — recommended end-to-end orchestrator.
- [`post_parser.py`](post_parser.py) — listing parser and Twitter/X fetcher.
- [`company_enrich.py`](company_enrich.py) — company profile extraction and web enrichment.
- [`pipeline.py`](pipeline.py) — single-item and batch parse/enrichment entry point.
- [`osint_stage.py`](osint_stage.py) — domain, Shodan, certificate, and breach assessment.
- [`crtsh.py`](crtsh.py) — crt.sh certificate database helper.
- [`config.yaml`](config.yaml) — providers, models, search, output, and API-key settings.
- [`structure.json`](structure.json) — JSON schema used during listing parsing.
- [`prompts.yaml`](prompts.yaml) and [`sectors.json`](sectors.json) — prompts and sector definitions.
- [`examples/`](examples/) — 20 example screenshots.
- [`searxng-docker/`](searxng-docker/) — local SearXNG deployment files.
- [`Commands.sh`](Commands.sh) — additional command examples.

## Requirements

- Python 3.11 or newer
- Docker and Docker Compose when using local SearXNG search
- API credentials for the providers and optional OSINT services you enable

## Installation

```bash
git clone <repository-url>
cd <repository-directory>

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
# source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## API credentials

Do not commit credentials. The safest option is to export environment variables in each session.

[`config.yaml`](config.yaml) also accepts credentials under `api_keys`. Keep values empty in published copies and set the matching environment variable instead:

| Service | Environment variable | Config key |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | `anthropic` |
| OpenAI | `OPENAI_API_KEY` | `openai` |
| Gemini | `GOOGLE_API_KEY` | `gemini` |
| OpenRouter | `OPENROUTER_API_KEY` | `openrouter` |
| Ollama | `OLLAMA_API_KEY` | `ollama` |
| Groq | `GROQ_API_KEY` | `groq` |
| Shodan | `SHODAN_API_KEY` | `shodan` |
| Ransomware.live | `RANSOMWARE_LIVE_API_KEY` | `ransomware_live` |


```bash
# Windows PowerShell
$env:OPENROUTER_API_KEY="..."
$env:SHODAN_API_KEY="..."

# Linux/macOS
export OPENROUTER_API_KEY="..."
export SHODAN_API_KEY="..."
```

## SearXNG setup

SearXNG is optional for standalone parsing but is the default self-hosted search backend:

```bash
cd searxng-docker
docker compose up -d
cd ..
```

Verify `http://localhost:8888` is reachable. The URL can be changed in `config.yaml` or with `SEARXNG_BASE_URL`.

## Configuration

Edit [`config.yaml`](config.yaml) to choose the parser provider and model, search backend, SearXNG URL, and result options. The default provider is OpenRouter and the default search backend is SearXNG.

For the thesis-evaluation setup, set `provider: openrouter` and select an OpenRouter model in `models.openrouter`. A model supplied with `--model` in the end-to-end pipeline overrides this value.

## Usage

### End-to-end

```bash
# Image
python e2e_pipeline.py --image examples/6.png

# Twitter/X status
python e2e_pipeline.py --url "https://x.com/DarkWebInformer/status/..."

# Plain text
python e2e_pipeline.py --text listing.txt

# Already parsed JSON
python e2e_pipeline.py --parsed parsed_output.json

# Choose a Borda identification model
python e2e_pipeline.py --image examples/6.png --model "google/gemini-2.5-flash"

# Skip the OSINT stage
python e2e_pipeline.py --image examples/6.png --no-osint
```

End-to-end outputs are written to `outputs/e2e/<input-stem>/`:

- `parsed.json` — parser output
- `attributes.json` — country, sector, revenue, employee, headquarters, and technology fields
- `borda_results.json` — individual votes and ranking details
- `candidate_list.json` — top candidate names
- OSINT report and JSON files when that stage is enabled

### Parse and enrich

```bash
python post_parser.py --image examples/6.png --output parsed.json
python company_enrich.py --json parsed.json --output enriched.json

# Batch parsing and enrichment
python pipeline.py --batch examples/ --output results/
python pipeline.py --batch examples/ --force --output results/
```

Use `python <script>.py --help` for provider, model, output, and search options.

### Standalone OSINT

```bash
python osint_stage.py --company "Example Company"
python osint_stage.py --domain "example.com"
python osint_stage.py --query 'ssl:"example.com"'
python osint_stage.py --candidate-list outputs/e2e/6/candidate_list.json --victim-json outputs/e2e/6/parsed.json
```

## Borda ranking

The identification stage sends the extracted company profile to the selected model repeatedly (eight iterations by default). Each response is interpreted as a ranked list. Borda points are assigned as 5, 4, 3, 2, and 1 for ranks one through five, and totals are used to produce the final ranking.

## Ethical and safety considerations

Use this pipeline only with evidence you are authorized to analyze. OSINT requests and breach lookups should comply with service terms and applicable privacy and cybersecurity laws. The project reports indicators and candidate matches; it does not establish attribution or victim identity with certainty.

Example images may contain sensitive or publicly accessible information. Review the `examples/` directory before making the repository public.

## Limitations

- Candidate ranking depends on model access, prompt behavior, and source quality.
- Twitter/X fetching and third-party APIs can change or require authentication.
- SearXNG and OSINT services may enforce rate limits or availability restrictions.
- Results should be reviewed and independently verified.


## License

This project is licensed under the [MIT License](LICENSE). When redistributing the code or substantial portions of it, retain the copyright and license notice.
