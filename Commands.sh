# ══════════════════════════════════════════════════════════════════════════════
# DARK WEB RANSOMWARE VICTIM IDENTIFICATION PIPELINE — COMMAND REFERENCE
# ══════════════════════════════════════════════════════════════════════════════
#
# This project is a modular threat intelligence pipeline that:
#   1. Parses a ransomware dark web listing (image, text, or Twitter/X URL)
#   2. Extracts structured attributes (country, sector, revenue, employees, HQ)
#   3. Queries an LLM multiple times and aggregates results with Borda count
#   4. Runs OSINT enrichment (Shodan, web search) on the top candidate companies
#
# ── REQUIREMENTS ─────────────────────────────────────────────────────────────
#   - Python 3.11+
#   - SearXNG running locally via Docker (for web search enrichment)
#   - API keys configured in config.yaml (OpenRouter, Shodan, etc.)
#
# ── SETUP ────────────────────────────────────────────────────────────────────

# Navigate to the project directory
cd path\to\thesis_code

# Activate the virtual environment (Windows)
venv\Scripts\activate

# Install required dependencies
pip install -r requirements.txt

# Start SearXNG (required for web search enrichment in OSINT stage)
cd searxng-docker
docker compose up -d
cd ..


# ══════════════════════════════════════════════════════════════════════════════
# OPTION A: END-TO-END PIPELINE (Recommended)
# Runs all 4 stages in sequence: Parse → Extract Attributes → LLM Borda → OSINT
# Output is saved to: outputs/e2e/<stem>/
#   ├── parsed.json          — structured parser output
#   ├── attributes.json      — extracted 5 core attributes
#   ├── borda_results.json   — all LLM iterations + Borda ranking
#   └── candidate_list.json  — top 5 identified companies
# ══════════════════════════════════════════════════════════════════════════════

# --- Input: Local image file (screenshot of dark web listing) ---
python e2e_pipeline.py --image examples/6.png
python e2e_pipeline.py --image examples/2.png

# --- Input: Twitter/X post URL (the listing is fetched from the tweet) ---
python e2e_pipeline.py --url "https://x.com/DarkWebInformer/status/2032502147267363322"

# --- Input: Plain text file (paste the listing text into a .txt file) ---
python e2e_pipeline.py --text listing.txt

# --- Input: Pre-parsed JSON (skip Stage 1, use an existing parsed.json) ---
python e2e_pipeline.py --parsed outputs/e2e/6/parsed.json

# --- Options ---
# Specify a different LLM model for Borda identification (Stage 3):
python e2e_pipeline.py --image examples/6.png --model "anthropic/claude-opus-4"

# Skip the OSINT stage (only parse + identify, faster):
python e2e_pipeline.py --image examples/6.png --no-osint

# Save output to a custom directory:
python e2e_pipeline.py --image examples/6.png --output my_results/run1/

# NOTE: Telegram channel URL support is not yet implemented.
#       Currently supported input types: --image, --url (Twitter/X), --text, --parsed


# ══════════════════════════════════════════════════════════════════════════════
# OPTION B: MODULAR USAGE (Run each stage independently)
# Use this when you want fine-grained control over individual stages.
# ══════════════════════════════════════════════════════════════════════════════

# ── STAGE 1: Parse a single listing ──────────────────────────────────────────
# Converts a dark web listing (image or URL) into a structured JSON file.
# The provider and model are read from config.yaml by default.

# From a local image:
python post_parser.py --image examples/6.png
python post_parser.py --image examples/2.png --output my_parsed/2.json

# From a Twitter/X URL:
python post_parser.py --url "https://x.com/DarkWebInformer/status/2032502147267363322"

# From a plain text file:
python post_parser.py --text listing.txt

# Override the provider or model:
python post_parser.py --image examples/6.png --provider openai
python post_parser.py --image examples/6.png --model gemini-2.5-flash


# ── BATCH PROCESSING (Parse & Enrich entire folder) ──────────────────────────
# Processes all images in a folder, parsing and enriching each one.
# Output is saved under output_dir/parsing_results/ (parsed) and output_dir/ (enriched).

python pipeline.py --batch examples/                             # parse & enrich all, skip if exists
python pipeline.py --batch examples/ --force                     # re-run everything from scratch
python pipeline.py --batch examples/ --output results/           # save results to custom folder


# ── STAGE 2+3 (Modular): Enrich a single parsed JSON (web search + LLM) ──────
# Takes a parsed JSON and enriches it with web search results and revenue data.

python company_enrich.py --json results/6.json --output enriched/6_enriched.json


# ── STAGE 1+2+3: Parse + Enrich pipeline (no OSINT) ──────────────────────────
# Combines parsing and web enrichment into one command.

python pipeline.py --image examples/6.png                          # single image
python pipeline.py --image examples/6.png --output enriched_v3/   # custom output
python pipeline.py --url "https://x.com/DarkWebInformer/status/2032502147267363322"
python pipeline.py --batch examples/ --output results/             # entire folder


# ── STAGE 4: OSINT Assessment (Shodan + web) ─────────────────────────────────
# Searches for a company or domain using Shodan and SearXNG.
# Can be run standalone or fed the output of the Borda stage.

# Search by company name:
python osint_stage.py --company "Calidra"

# Search by domain:
python osint_stage.py --domain "calidra.cl"

# Custom Shodan query:
python osint_stage.py --query "ssl:\"calidra.cl\""

# Feed candidate list from Borda stage + victim parsed JSON:
python osint_stage.py --candidate-list outputs/e2e/6/candidate_list.json \
                      --victim-json outputs/e2e/6/parsed.json
