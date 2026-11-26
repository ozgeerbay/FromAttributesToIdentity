#!/usr/bin/env python3
"""
Dark Web Listing Parser
=======================
Parses a single dark web listing (image or text) into a structured JSON file.

Configuration files (must be in the same directory as this script):
  config.yaml                 — provider, model, API keys, output settings
  prompts.yaml                — system prompt rules (edit to update parsing behavior)
  dw_listing_schema_v3.json   — JSON schema, also used as the template sent to LLM

Supported providers: anthropic, openai, gemini

Installation:
  pip install -r requirements.txt

Usage:
  python dw_parser.py --image 6.png
  python post_parser.py --image 6.png --output custom_name.json
  python dw_parser.py --text listing.txt
  cat listing.txt | python dw_parser.py --stdin

  # Override provider or model (config.yaml used otherwise)
  python dw_parser.py --image 6.png --provider openai
  python dw_parser.py --image 6.png --model gemini-2.0-flash

Output filename:
  6.png        ->  6.json
  6.jpg        ->  6.json
  listing.txt  ->  listing.json
  stdin        ->  listing_<timestamp>.json
"""

import base64
import json
import argparse
import sys
import os
import re
import tempfile
from pathlib import Path
from datetime import datetime, timezone


SCRIPT_DIR = Path(__file__).parent


# ── Twitter/X fetch ────────────────────────────────────────────────────────────

def _extract_tweet_id(url: str) -> str | None:
    """Extract tweet ID from a Twitter/X URL."""
    m = re.search(r"/status/(\d+)", url)
    return m.group(1) if m else None


def fetch_tweet(tweet_url: str, cfg: dict = None) -> dict:
    """
    Fetch tweet text and media using Twitter's syndication API.
    No authentication required — Twitter uses this for its own embed feature.
    Returns {text, image_urls, video_urls, tweet_id}.
    """
    import urllib.request
    import urllib.error

    tweet_id = _extract_tweet_id(tweet_url)
    if not tweet_id:
        raise ValueError(f"Could not extract tweet ID from URL: {tweet_url}")

    api_url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&lang=en&token=x"
    req = urllib.request.Request(api_url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; TweetFetcher/1.0)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Twitter syndication API error {e.code}: {e.reason}")

    text = data.get("text") or ""
    image_urls = []
    video_urls = []

    # Extract media
    for photo in data.get("photos", []) or []:
        url = photo.get("url") or photo.get("expandedUrl")
        if url:
            image_urls.append(url)

    video = data.get("video") or {}
    for variant in video.get("variants", []) or []:
        if variant.get("type") == "video/mp4":
            video_urls.append(variant.get("src", ""))

    # Fallback: mediaDetails array
    for md in data.get("mediaDetails", []) or []:
        mtype = md.get("type", "")
        if mtype == "photo":
            u = md.get("media_url_https") or md.get("media_url")
            if u and u not in image_urls:
                image_urls.append(u)
        elif mtype in ("video", "animated_gif"):
            variants = (md.get("video_info") or {}).get("variants", [])
            mp4s = [v for v in variants if v.get("content_type") == "video/mp4"]
            if mp4s:
                best = max(mp4s, key=lambda v: v.get("bitrate", 0))
                video_urls.append(best["url"])

    return {"text": text, "image_urls": image_urls, "video_urls": video_urls, "tweet_id": tweet_id}


def download_image(url: str) -> tuple[str, str, Path]:
    """Download image from URL to temp file, return (b64, mime, path)."""
    import urllib.request
    import urllib.error
    suffix = ".jpg"
    for ext in [".png", ".webp", ".jpg", ".jpeg", ".gif"]:
        if ext in url.lower():
            suffix = ext
            break
    mime_map = {".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".gif": "image/gif"}
    mime = mime_map.get(suffix, "image/jpeg")
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    # Some media hosts (e.g. pbs.twimg.com) return 403 without browser-like headers.
    candidates = [url]
    if "pbs.twimg.com" in url and "name=" not in url:
        sep = "&" if "?" in url else "?"
        candidates.append(f"{url}{sep}name=orig")

    last_err = None
    for candidate in candidates:
        req = urllib.request.Request(
            candidate,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Referer": "https://x.com/",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                with open(tmp.name, "wb") as f:
                    f.write(resp.read())
            last_err = None
            break
        except urllib.error.HTTPError as e:
            last_err = e
            continue
        except Exception as e:
            last_err = e
            continue

    if last_err is not None:
        raise RuntimeError(f"Image download failed: {url} ({last_err})")

    with open(tmp.name, "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode("utf-8")
    return b64, mime, Path(tmp.name)


# ── File loaders ───────────────────────────────────────────────────────────────

def load_yaml(path: Path, label: str) -> dict:
    if not path.exists():
        print(f"[!] {label} not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        import yaml
    except ImportError:
        print("[!] pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
        sys.exit(1)
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[!] Could not read {label} ({path}): {e}", file=sys.stderr)
        sys.exit(1)


def load_schema_as_template(path: Path) -> str:
    """
    Load the JSON schema file and convert it into a clean JSON template
    to send to the LLM — stripping $schema, descriptions, examples, enums
    so only the structure with null placeholders remains.
    """
    if not path.exists():
        print(f"[!] Schema file not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        with open(path, encoding="utf-8") as f:
            schema = json.load(f)
    except Exception as e:
        print(f"[!] Could not read schema ({path}): {e}", file=sys.stderr)
        sys.exit(1)

    def schema_to_template(node):
        """Recursively convert schema node to a template value."""
        if not isinstance(node, dict):
            return None

        t = node.get("type")

        # Object -> recurse into properties
        if t == "object" or "properties" in node:
            props = node.get("properties", {})
            return {k: schema_to_template(v) for k, v in props.items()}

        # Array -> empty list
        if t == "array" or (isinstance(t, list) and "array" in t):
            return []

        # Nullable scalar -> null
        return None

    props = schema.get("properties", {})
    template = {k: schema_to_template(v) for k, v in props.items()}

    return (
        "Fill the following JSON structure with values extracted from the listing.\n"
        "Set fields to null if unknown or not present in the listing.\n"
        "Output only the completed JSON object — no markdown, no explanation.\n\n"
        + json.dumps(template, indent=2, ensure_ascii=False)
    )


# ── Provider defaults ──────────────────────────────────────────────────────────

PROVIDER_DEFAULTS = {
    "anthropic": {"model": "claude-opus-4-6",   "env_key": "ANTHROPIC_API_KEY"},
    "openai":    {"model": "gpt-4o",            "env_key": "OPENAI_API_KEY"},
    "gemini":    {"model": "gemini-2.0-flash",  "env_key": "GOOGLE_API_KEY"},
    "openrouter": {"model": "google/gemini-2.5-flash-lite", "env_key": "OPENROUTER_API_KEY"},
}

DEFAULT_PROVIDER = "anthropic"


# ── Provider implementations ───────────────────────────────────────────────────

def call_anthropic(images, text, source_hint,
                   model, api_key, system_prompt, schema_template) -> str:
    """images: list of (b64, mime) tuples, or a single (b64, mime) tuple, or None."""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    content = []
    for b64, mime in (_normalise_images(images)):
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64}
        })
    if text:
        content.append({"type": "text", "text": f"Listing source: {source_hint}\n\n{text}"})
    content.append({"type": "text", "text": schema_template})

    response = client.messages.create(
        model=model, max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": content}]
    )
    return response.content[0].text


def call_openai(images, text, source_hint,
                model, api_key, system_prompt, schema_template) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    content = []
    for b64, mime in _normalise_images(images):
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"}
        })
    if text:
        content.append({"type": "text", "text": f"Listing source: {source_hint}\n\n{text}"})
    content.append({"type": "text", "text": schema_template})

    response = client.chat.completions.create(
        model=model, max_tokens=4096,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": content}
        ]
    )
    return response.choices[0].message.content


def call_gemini(images, text, source_hint,
                model, api_key, system_prompt, schema_template) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    contents = []
    for b64, mime in _normalise_images(images):
        contents.append(
            types.Part.from_bytes(
                data=base64.b64decode(b64),
                mime_type=mime,
            )
        )
    if text:
        contents.append(f"Listing source: {source_hint}\n\n{text}")
    contents.append(schema_template)

    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            max_output_tokens=8192,
        ),
    )
    return response.text


def call_openrouter(images, text, source_hint,
                    model, api_key, system_prompt, schema_template) -> str:
    import requests
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    content = []
    for b64, mime in _normalise_images(images):
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"}
        })
    if text:
        content.append({"type": "text", "text": f"Listing source: {source_hint}\n\n{text}"})
    content.append({"type": "text", "text": schema_template})

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": content}
        ]
    }

    resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"].get("content", "")


def _normalise_images(images) -> list[tuple[str, str]]:
    """Accept None, a single (b64, mime) tuple, or a list of (b64, mime) tuples."""
    if not images:
        return []
    if isinstance(images, tuple):
        return [images]
    return images


PROVIDER_CALLERS = {
    "anthropic": call_anthropic,
    "openai":    call_openai,
    "gemini":    call_gemini,
    "openrouter": call_openrouter,
}


# ── Core helpers ───────────────────────────────────────────────────────────────

def load_image(image_path: Path) -> tuple[str, str]:
    mime_map = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif",
    }
    # If path has no extension or file doesn't exist, search for it
    if not image_path.exists():
        for ext in mime_map:
            candidate = image_path.with_suffix(ext)
            if candidate.exists():
                image_path = candidate
                break
        else:
            raise FileNotFoundError(f"No image file found for: {image_path.stem!r} (tried {list(mime_map.keys())})")
    mime = mime_map.get(image_path.suffix.lower(), "image/png")
    with open(image_path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8"), mime, image_path


def clean_and_parse(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        try:
            import json_repair
            return json_repair.loads(raw)
        except ImportError:
            raise e
        except Exception:
            raise e


def enrich_meta(result: dict, filename: str | None) -> dict:
    if "meta" not in result:
        result["meta"] = {}
    if filename:
        result["meta"]["screenshot_file"] = filename
    if not result["meta"].get("collected_at"):
        result["meta"]["collected_at"] = datetime.now(timezone.utc).isoformat()
    return result


def resolve_output_path(output_arg, source_name: str) -> Path:
    if output_arg:
        p = Path(output_arg)
        if p.suffix != ".json" or (p.exists() and p.is_dir()):
            stem = Path(source_name).stem if source_name != "stdin" else f"listing_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            p = p / (stem + ".json")
        return p
    if source_name != "stdin":
        return Path(Path(source_name).stem + ".json")
    return Path(f"listing_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    # Load all config files
    cfg     = load_yaml(SCRIPT_DIR / "config.yaml",  "config.yaml")
    prompts = load_yaml(SCRIPT_DIR / "prompts.yaml", "prompts.yaml")

    system_prompt = prompts.get("system_prompt", "")
    if not system_prompt:
        print("[!] prompts.yaml must contain 'system_prompt'", file=sys.stderr)
        sys.exit(1)

    schema_path = SCRIPT_DIR / cfg.get("schema_file", "dw_listing_schema_v3.json")
    schema_template = load_schema_as_template(schema_path)

    cfg_provider = cfg.get("provider", DEFAULT_PROVIDER)
    cfg_models   = cfg.get("models", {})
    cfg_api_keys = cfg.get("api_keys", {})
    cfg_quiet    = cfg.get("output", {}).get("quiet", False)

    # Argument parser
    ap = argparse.ArgumentParser(
        description="Dark web listing parser -> structured JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ig = ap.add_mutually_exclusive_group(required=True)
    ig.add_argument("--image", type=Path)
    ig.add_argument("--text",  type=Path)
    ig.add_argument("--stdin", action="store_true")
    ig.add_argument("--url",   type=str, help="Twitter/X tweet URL")

    ap.add_argument("--output",   type=Path, default=None)
    ap.add_argument("--provider", choices=list(PROVIDER_CALLERS.keys()), default=None)
    ap.add_argument("--model",    type=str,  default=None)
    ap.add_argument("--api-key",  type=str,  default=None)
    ap.add_argument("--quiet",    action="store_true")

    args = ap.parse_args()

    # Resolve settings: CLI > config.yaml > hardcoded defaults
    provider = args.provider or cfg_provider
    model    = args.model    or cfg_models.get(provider) or PROVIDER_DEFAULTS[provider]["model"]
    quiet    = args.quiet    or cfg_quiet

    api_key = args.api_key or cfg_api_keys.get(provider) or ""
    if not api_key:
        api_key = os.environ.get(PROVIDER_DEFAULTS[provider]["env_key"], "")
    if not api_key:
        env_var = PROVIDER_DEFAULTS[provider]["env_key"]
        print(
            f"[!] No API key found.\n"
            f"    Option 1 — config.yaml:  api_keys.{provider}: \"YOUR_KEY\"\n"
            f"    Option 2 — env variable: export {env_var}=YOUR_KEY\n"
            f"    Option 3 — CLI flag:     --api-key YOUR_KEY",
            file=sys.stderr
        )
        sys.exit(1)

    caller = PROVIDER_CALLERS[provider]
    print(f"[>] Provider: {provider}  |  Model: {model}  |  Schema: {schema_path.name}")

    # Parse
    try:
        if args.image:
            print(f"[>] Parsing image: {args.image.name if args.image.suffix else args.image.name + '.*'}")
            image_b64, image_mime, args.image = load_image(args.image)
            raw = caller((image_b64, image_mime), None, args.image.name,
                         model, api_key, system_prompt, schema_template)
            result = clean_and_parse(raw)
            result = enrich_meta(result, args.image.name)
            output_path = resolve_output_path(args.output, args.image.name)

        elif args.text:
            if not args.text.is_file():
                print(f"[x] File not found: {args.text}", file=sys.stderr); sys.exit(1)
            print(f"[>] Parsing text: {args.text.name}")
            text = args.text.read_text(encoding="utf-8")
            raw = caller(None, text, args.text.name,
                         model, api_key, system_prompt, schema_template)
            result = clean_and_parse(raw)
            result = enrich_meta(result, args.text.name)
            output_path = resolve_output_path(args.output, args.text.name)

        elif args.stdin:
            print("[>] Reading from stdin (Ctrl+D to finish)...")
            text = sys.stdin.read()
            raw = caller(None, text, "stdin",
                         model, api_key, system_prompt, schema_template)
            result = clean_and_parse(raw)
            result = enrich_meta(result, None)
            output_path = resolve_output_path(args.output, "stdin")

        elif args.url:
            print(f"[>] Fetching tweet: {args.url}")
            tweet = fetch_tweet(args.url, cfg)
            tweet_id = tweet["tweet_id"]
            tweet_text = tweet["text"]
            image_urls = tweet["image_urls"]
            print(f"    text: {tweet_text[:120]}{'...' if len(tweet_text) > 120 else ''}")
            print(f"    images: {len(image_urls)}, videos: {len(tweet['video_urls'])}")

            source_hint = f"tweet:{tweet_id}"
            tmp_paths = []

            if image_urls:
                print(f"[>] Downloading {len(image_urls)} image(s)...")
                images = []
                for url in image_urls:
                    b64, mime, tmp_path = download_image(url)
                    images.append((b64, mime))
                    tmp_paths.append(tmp_path)
                raw = caller(images, tweet_text or None, source_hint,
                             model, api_key, system_prompt, schema_template)
            elif tweet_text:
                raw = caller(None, tweet_text, source_hint,
                             model, api_key, system_prompt, schema_template)
            else:
                raise ValueError("Tweet has no text or images to parse.")

            for p in tmp_paths:
                try:
                    os.unlink(p)
                except Exception:
                    pass

            result = clean_and_parse(raw)
            result = enrich_meta(result, None)
            result["meta"]["source_url"] = args.url
            result["meta"]["source_platform"] = "twitter"
            result["meta"]["platform_type"] = "twitter"
            output_path = resolve_output_path(args.output, f"tweet_{tweet_id}")

    except json.JSONDecodeError as e:
        print(f"[x] JSON parse error: {e}", file=sys.stderr); sys.exit(1)
    except Exception as e:
        print(f"[x] Error: {e}", file=sys.stderr); sys.exit(1)

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    if not quiet:
        print(json.dumps(result, indent=2, ensure_ascii=False))

    print(f"\n[OK] Saved -> {output_path}")


if __name__ == "__main__":
    main()