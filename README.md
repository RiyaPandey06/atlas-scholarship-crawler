# Atlas Scholarship Intelligence Crawler

Automated pipeline that **discovers → classifies → crawls → extracts → verifies → scores → stores → updates**
scholarship opportunities for Indian students, keeping an evidence trail from every database value back to the
official source text.

> **Status, stated plainly.** The system, schema, dashboard, tests and an offline two-run demo are complete and pass
> (`pytest`: 24 tests). The *real-internet* crawl has to be run from your machine / CI (see "Produce the real dataset").
> `data/demo.db` and `sample_data/` contain **fictional test-fixture scholarships only** (flagged `is_fixture=1`,
> hidden from the dashboard by default). No real scholarship data is bundled, and none was hand-written.

## Quick start
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q tests                      # 24 tests incl. an end-to-end two-run crawl
python -m atlas_crawler.cli demo               # offline demo: run 1 -> run 2 shows change / removal / expiry detection
ATLAS_CONFIG=config.demo.yaml streamlit run app.py     # inspect the demo DB
```

## Produce the real dataset
```bash
# edit config.yaml: set user_agent contact email; add seeds / queries if you like
python -m atlas_crawler.cli inspect https://scholarships.gov.in/All-Scholarships   # dry-run ONE url: fields, quotes, score
python -m atlas_crawler.cli crawl              # run 1: discovers + stores (data/atlas.db)
python -m atlas_crawler.cli crawl              # run again later: re-verifies, detects changes / expiry / removal
python -m atlas_crawler.cli stats
streamlit run app.py
python -m atlas_crawler.cli export --out scholarships.csv
python -m atlas_crawler.cli schedule --every-hours 24     # long-running scheduler (or use .github/workflows/crawl.yml)
```
Assignment minimums (20+ records, 15+ verified, 10+ at >=95, 3+ source types) are *outputs of a run*, not settings:
if a run falls short, add seeds/queries or raise `max_pages_per_run` and run again. Do **not** loosen the
scoring or grounding rules to hit a number. `inspect` shows exactly why a page scored what it did.

## After a real crawl: diagnose, then fix the right thing
```bash
python -m atlas_crawler.cli diagnose            # points lost per check, worst records, failures by host/kind
python -m atlas_crawler.cli inspect <url>       # one page: every field, its quote, rejected values, score
```
Score ceilings (default weights): no stated deadline -> max ~82; no apply link -> max 90; unverifiable TLS -> max 92.5.
So VERIFIED needs pages that state a deadline (or "rolling"), link to an application route and load over valid TLS:
specific scheme/notice/guideline pages, not generic department pages. For TLS chain errors try
`pip install truststore` and `crawl.use_os_trust_store: true` before touching `scoring.ssl_unverified_factor`.
Fetch failures are classified: `not_found` / `blocked` are final; `transient` (5xx, timeouts, 429) is retried on later runs.

## How it works
| Stage | Module | What it does |
|---|---|---|
| Discover | `pipeline.py`, `search.py` | seeds + free web search (DuckDuckGo HTML / your SearXNG) + link-following; persistent frontier (`candidates`) so each run finds *new* pages; aggregator pages are mined only for outbound **official** links |
| Classify | `classify.py` | hostname-**suffix** rules + curated allow-lists -> GOVERNMENT / UNIVERSITY / CORPORATE_CSR / FOUNDATION_TRUST / INTERNATIONAL / AGGREGATOR / UNKNOWN; aggregators can never be a record's source |
| Crawl | `fetcher.py` | robots.txt, per-domain delay, retries, PDF text, TLS-fallback flagged and penalised |
| Extract | `extract.py`, `llm.py` | rule extractor + independent structural extractor (tables/JSON-LD) + optional Ollama proposer. Each value carries a verbatim quote |
| Ground | `extract.ground` | quote must appear verbatim in the page and the value must follow from the quote, else the value is dropped -> `Not specified` |
| Verify/score | `verify.py` | 9 weighted, evidence-based checks (see below) |
| Store/update | `pipeline.py`, `db.py`, `schema.sql` | upsert, append-only `change_log`, superseded (never deleted) evidence, snapshots, score history |
| Stale detection | `pipeline.py` | deadline passed / "applications closed" text -> EXPIRED; <=14 days -> EXPIRING_SOON; removed or unreachable on consecutive re-verifications -> NO_LONGER_VERIFIABLE |

### Confidence = sum of nine checks (weights in `config.yaml`, sum 100)
official source (20) · live on source (15) · application URL resolves (10) · deadline supported by quote (15) ·
eligibility supported by quotes (12) · freshness (5) · no conflicting values (5) · agreement between independent
extractors (8) · key-field traceability (10). VERIFIED requires >=95 **and** gates: official source, confirmed live,
no unexplained conflict. No LLM ever produces or adjusts the number.

## Layout
```
atlas_crawler/   classify, fetcher, extract, llm, verify, db, search, pipeline, cli
app.py           Streamlit dashboard        schema.sql   database schema
config.yaml      production config          config.demo.yaml   offline demo config
demo/            fictional local site       tests/       pytest suite
TECHNICAL_NOTE.md   3-page note             sample_data/ demo (fixture) export
```

## Known limitations (read before judging results)
* Extraction is pattern-based on English text; JavaScript-only portals (several state portals, parts of NSP) may yield
  little text. A Playwright rendering fallback is the natural next step and is not implemented.
* Real-site accuracy has not been measured by me; use `inspect` and manual sampling and tune the patterns.
* One page = one scholarship. Multi-scheme pages are treated as listings and expanded, not split.
* Free web search (DuckDuckGo HTML) can rate-limit; set `discovery.searxng_url` for a self-hosted option.
* The LLM path (Ollama) is optional and covered only by unit tests of its grounding gate, not by a live model run.
