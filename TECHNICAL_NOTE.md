# Technical Note: Atlas Scholarship Intelligence Crawler

## 1. Architecture
```
 seeds ─┐
 search ─┼─> candidates (persistent frontier) ─> fetch ─> classify source ──┬─ AGGREGATOR ─> mine outbound OFFICIAL links only
 links ──┘                                                                   └─ official/unknown ─> parse ─> extract (2+ methods)
                                                                                    ─> grounding gate ─> verify+score ─> upsert
 SQLite: scholarships · field_evidence · change_log · score_history · snapshots · candidates · crawl_runs
 Every run: discover new pages  +  re-verify every known record  +  time-based lifecycle sweep.  Streamlit dashboard reads the DB.
```
**Stack (all free):** Python, requests, BeautifulSoup/lxml, pypdf, SQLite, Streamlit, PyYAML, pytest; optional Ollama (local LLM).
SQLite was chosen so the reviewer can open the DB file directly; no service to host.

## 2. Discovery (not a fixed URL list)
Three channels feed one persistent frontier: (a) a small seed set of official entry points, (b) free web-search queries
(DuckDuckGo HTML or self-hosted SearXNG) and (c) link-following from fetched pages using scholarship keywords, limited to
the same registrable domain or another official host. Aggregator/news pages found by search are **discovery-only**: the
crawler harvests their outbound links to official hosts and discards everything else they say, so a scholarship found via
an aggregator is only stored if its official page is fetched and verified. Seeds and listing pages are re-crawled each run;
already-seen detail pages are not, so new pages surface as new records. Demonstrated offline: a scholarship reachable only
through the simulated aggregator is discovered in run 2 with its official page as the source.

## 3. Source classification
`classify.py` assigns GOVERNMENT, UNIVERSITY, CORPORATE_CSR, FOUNDATION_TRUST, INTERNATIONAL, AGGREGATOR or UNKNOWN using
hostname-**suffix** matching (so `gov.in.evil.com` is not government) plus curated allow-lists. Strength is 1.0 for
structurally/curated official hosts, 0.6 for unlisted foundation-like hosts, 0.3 unknown, 0 for aggregators. Aggregators can
never become a record's official source, and a source with strength < 0.9 can never be VERIFIED.

## 4. Extraction
Each page becomes ordered text segments. A rule extractor pulls 18 fields (name, provider, amount, eligibility, academic /
education / income / age / gender / category / domicile / institution criteria, opening/closing date, documents, selection,
renewal, application URL). An independent structural extractor reads tables, definition lists and JSON-LD. An optional
Ollama model may *propose* values. All outputs share one contract: `value` + verbatim `quote`.

## 5. Anti-hallucination design
1. **Quote grounding:** the quote must occur verbatim (whitespace-normalised) in the fetched page text, else the value is discarded.
2. **Value-from-quote check:** dates must parse out of the quote to the stored ISO date; amounts/income digits must appear in the quote.
3. **Absent means absent:** a field with no grounded evidence is stored as `Not specified` (application URL: `Not available`).
   The test-suite asserts that a page without an income limit yields no income value and that fabricated LLM quotes are rejected.
4. **Provenance retained:** `field_evidence` stores value, quote, URL, method, grounded flag and capture time; superseded rows
   are kept. Traceability path: *scholarship row → official URL → evidence row (quote) → extracted value*, plus a compressed
   page snapshot so the quote can be re-checked later. Rejected extractions are recorded in the score breakdown for audit.

## 6. Verification and confidence methodology
Confidence is a deterministic weighted sum of nine checks, each scored 0-1 from observable evidence:

| Check | Wt | Evidence used |
|---|---|---|
| official source | 20 | classifier strength |
| live on source | 15 | HTTP 200, page recognised as a scholarship, name in body; ×0.5 for unverifiable TLS or off-domain redirect |
| application URL | 10 | link found and resolves (HEAD/GET); third-party host 0.7 |
| deadline supported | 15 | grounded date (1.0), grounded "rolling" (0.85), none (0) |
| eligibility supported | 12 | grounded eligibility text + count of grounded structured criteria |
| freshness | 5 | deadline in future / years mentioned / Last-Modified |
| no conflicts | 5 | multiple distinct closing dates (0.5 if explained by an extension notice, else 0); extractor disagreements |
| extraction consistency | 8 | agreement between independent extractors; stability vs previous crawl |
| traceability | 10 | share of key fields with a grounded quote (derived provider = ½) |

**VERIFIED** = score ≥ 95 **and** gates pass (official source, confirmed live, no unexplained conflict); otherwise
**REVIEW_REQUIRED**. The per-check table (weight, score, points, detail) is stored with each record and shown under "Why this
score?". Weights live in `config.yaml`. No LLM produces or adjusts a number. Limits: the score measures *evidence quality
on the page*, not whether the underlying scheme is good; weights are a reasoned prior, not calibrated against labelled data.

## 7. Change and stale detection
Each run re-fetches every known record. Per-field comparison (long text compared on a normalised key) writes an append-only
`change_log` row with old value, new value, detection time, source URL and the supporting quote; the old evidence row is
marked superseded, not deleted. Lifecycle statuses: ACTIVE, EXPIRING_SOON (≤14 days), EXPIRED (date passed or "applications
closed" text), REVIEW_REQUIRED (no traceable deadline), NO_LONGER_VERIFIABLE (404/410, unreachable, or page no longer
describes the scheme for N consecutive runs; N is configurable). Failed re-verification removes the live and freshness points
from the stored breakdown, so a vanished page cannot stay VERIFIED. A time-based sweep expires records between fetches.

## 8. What has and has not been demonstrated
* **Demonstrated (offline, automated tests + demo):** end-to-end two-run pipeline on a fictional local site: 2 field changes
  (deadline, amount), 1 removal → NO_LONGER_VERIFIABLE, 2 expired (one by date, one by "applications closed" text), 1
  expiring-soon, 1 aggregator-discovered record whose source is the official page, grounding rejections, gate behaviour.
* **Not done in the build environment:** crawling the live internet (outbound access to government sites was unavailable), so
  real-record counts, real-site extraction accuracy and the share reaching ≥95 are unmeasured. They are produced by running
  `crawl` and are reported by the dashboard. The Ollama path and Playwright rendering for JavaScript-only portals are not
  exercised live.
