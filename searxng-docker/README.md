# SearXNG for Company Enrich

SearXNG meta search engine — used when `search_backend: searxng` in config.

## Quick start

```bash
cd searxng-docker
docker compose up -d
```

Then set in `config.yaml` (or env `SEARXNG_BASE_URL`):

```yaml
search_backend: searxng
searxng_base_url: "http://localhost:8888"
```

## Docs

- [SearXNG Docker install](https://docs.searxng.org/admin/installation-docker.html)
