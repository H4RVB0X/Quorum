# Quorum — Claude Code Instructions

## Read First
Before doing anything, read PROJECT_MEMORY.md in full. It contains the complete history of what has been built, all architecture decisions, gotchas, and current system state.

## At the End of Every Session
Append a new session block to PROJECT_MEMORY.md following the exact same format as existing entries:

Be specific. Future Claude sessions rely entirely on this document for context. Do not skip this step.

## Key Facts
- Graph ID: d3a38be8-37d9-4818-be28-5d2d0efa82c0
- Root .env uses localhost hostnames (for host Python scripts)
- backend/.env uses neo4j/ollama hostnames (for Docker containers)
- Dashboard served at localhost:5001/dashboard
- Project is called Quorum in docs, mirofish in internal code
- After any backend change: docker cp the file + docker restart mirofish-offline

## News Feed Gotchas
- **Central bank feeds are never relevance-filtered** — this is intentional. `CENTRAL_BANK_FEEDS` (Fed, ECB, BoE) bypass `filter_articles()` entirely. Every item from a central bank is financially relevant by definition. Do not add them to `RSS_FEEDS` or `NITTER_FEEDS` or they will be filtered.
- **`NITTER_ENABLED` flag** — currently `False` in `news_fetcher.py`. All public nitter.net instances are timing out on every request and producing zero articles. Re-enable only when self-hosted Nitter is running in Docker. The `NITTER_FEEDS` list is preserved in the file — only the flag needs to change.
- **Bloomberg Markets removed from `RSS_FEEDS`** — was returning 403 on all server-side requests. A comment marks the removal location. Replace with an authenticated source (Bloomberg API, RapidAPI proxy) if needed.
- **Unusual Whales removed from `RSS_FEEDS`** — no public RSS feed exists; requires a paid API. A comment marks the removal location.
- **Benzinga URL corrected** — was `benzinga.com/feeds/news` (404); now `benzinga.com/latest?page=1&feed=rss`.
- **Investopedia removed** — returns 402 Payment Required (paywalled). A comment marks the removal location.
- **TheStreet removed** — both `thestreet.com/feeds/rss/index.xml` and `thestreet.com/rss/index.xml` return 403. A comment marks the removal location.
- **AP Business added** — `apnews.com/hub/business/feed`. No paywall, reliable uptime, no timeout needed. Uses feedparser's default fetch (not in `_TIMEOUT_FEEDS`).
- **Benzinga and Seeking Alpha have a 5-second feed timeout** — `parse_feed()` accepts a `timeout` parameter. These two feeds pass `timeout=5` via the `_TIMEOUT_FEEDS` set check in `fetch()`. On timeout, a WARNING is logged (`"{source_name} timed out"`) and the feed returns `[]`. All other feeds (Reuters, Yahoo, CNBC, MarketWatch, FT, AP Business, central banks) have no timeout.
- **Relevance filter is a whitelist, not a blacklist** — if briefings are too short, expand `FINANCIAL_TERMS` in `news_relevance_filter.py`. Common missing terms: company names (Nvidia, Apple, Tesla), commodity-specific terms, emerging-market indices.

## NER / Knowledge Graph Gotchas
- **spaCy replaces LLM-based NER in `incremental_update.py`** — entity extraction uses `en_core_web_sm` loaded once at module level. The old `NERExtractor` (Ollama LLM per chunk) is no longer called. The container uses a `uv` venv at `/app/backend/.venv/` — if spaCy is missing, install via `docker exec mirofish-offline uv pip install spacy "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl" --python /app/backend/.venv/bin/python`. Do NOT use the system `pip` — it installs into the wrong Python.
- **Relation extraction is not performed** — spaCy NER does not extract entity-to-entity relations. The `RELATION` edges from the old LLM pipeline are no longer created by this script.
- **`central_bank_source` property is schema-ready but always `false`** — the briefing `.txt` format does not expose per-chunk source metadata, so no entity is currently tagged `central_bank_source=true` from this script. The property and UNWIND logic are in place for when chunk-level metadata is added.
- **Neo4j writes are batched (BATCH_SIZE=500)** — `UNWIND $entities AS e MERGE ...`. Do not replace with single-entity MERGEs; at 2160+ chunks the round-trip overhead is prohibitive.

## LLM / Prompt Gotchas
- **qwen2.5:14b code-switches to Chinese** on FOMO/emotional/stress language unless `"You must respond entirely in English. Do not use any other language."` is the first line of the system prompt in `build_prompt()`. Do not remove or relocate this line.
- **hedge defaults to catch-all without a naming gate** — qwen2.5:14b will use `hedge` as a generic cautious response unless the prompt explicitly requires the model to name the instrument and the exposure being hedged. The current definition in `build_prompt()` enforces this: "you must be able to state exactly what instrument you are using and what exposure you are hedging… If you cannot name the instrument and the risk, choose hold instead." Do not soften this language.