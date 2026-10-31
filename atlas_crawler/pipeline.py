"""Discover -> Classify -> Crawl -> Extract -> Verify -> Score -> Store -> Update."""
import datetime as dt
import json
import logging
import re
from collections import Counter
from urllib.parse import urlsplit

from .classify import classify, AGGREGATOR
from .db import DB
from .extract import (FIELDS, parse_page, extract_rules, extract_structural, ground, closed_signal,
                      looks_like_scholarship, quote_grounded, value_supported, find_dates)
from .fetcher import Fetcher, FetchResult
from .llm import OllamaExtractor
from .search import search
from .util import (NOT_SPECIFIED, canonical_url, host_of, now_iso, registrable_domain, sha, norm_text_key)
from .verify import score_record

log = logging.getLogger("atlas.pipeline")

LONG_FIELDS = {"eligibility_text", "documents_required", "selection_process", "renewal_requirements", "institution_requirements"}
CMP_FIELDS = ["closing_date", "opening_date", "amount", "income_limit", "name", "provider"]
SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".zip", ".rar", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".mp4", ".mp3", ".css", ".js", ".ico")
SKIP_PATH = re.compile(r"login|logout|signin|sign-in|captcha|password|sitemap|javascript|tender|recruitment|vacanc|career|"
                       r"\brti\b|gallery|contact-?us|feedback|screen-?reader|disclaimer|privacy|terms|copyright|hyperlink|"
                       r"photo|video|parliament|annual-?report|whatsnew|what-s-new", re.I)
_GOOD_KW = re.compile(r"scholarship|fellowship|stipend|bursary", re.I)
_DOC_KW = re.compile(r"guideline|notice|notification|apply|application|last-?date|brochure|advertisement|call-?for|eligibility|scheme", re.I)
_YEAR_KW = re.compile(r"2026|2027|26-27|2026-27|2026-2027", re.I)


def candidate_priority(url: str, text: str, base: float = 0.0) -> float:
    """Cheap, transparent ordering so a limited page budget goes to pages likely to be specific scholarship pages."""
    path = urlsplit(url).path
    t = f"{text} {path}"
    return base + 3 * bool(_GOOD_KW.search(text or "")) + 2 * bool(_GOOD_KW.search(path)) \
        + 2 * bool(_DOC_KW.search(t)) + 1.5 * bool(_YEAR_KW.search(t)) + 1 * path.lower().endswith(".pdf")


def compute_status(closing, closed_sig, today: dt.date, soon_days: int):
    if closed_sig:
        return "EXPIRED", f"page states: {closed_sig[:160]}"
    if closing and closing.startswith("Rolling"):
        return "ACTIVE", "rolling scheme, no fixed deadline"
    if closing and closing != NOT_SPECIFIED:
        d = dt.date.fromisoformat(closing)
        if d < today:
            return "EXPIRED", f"closing date {closing} is before {today}"
        if (d - today).days <= soon_days:
            return "EXPIRING_SOON", f"closing date {closing} is within {soon_days} days"
        return "ACTIVE", f"closing date {closing} is in the future"
    return "REVIEW_REQUIRED", "no deadline traceable to the source"


class Crawler:
    def __init__(self, cfg, db: DB, fetcher=None, llm=None, today=None, out=print):
        self.cfg, self.db = cfg, db
        self.fetcher = fetcher or Fetcher(cfg)
        self.llm = llm or OllamaExtractor(cfg)
        self.today = today or dt.date.today()
        self.out = out
        self.c = cfg.get("crawl", {})
        self.stats = Counter()
        self.touched = set()
        self.run_id = None

    # ================================================================= public
    def run(self) -> dict:
        self.run_id = self.db.start_run()
        self.out(f"== crawl run {self.run_id} started ({self.today}) ==")
        self.db.reset_recrawl()
        self._seed()
        self._discover_search()
        self._crawl_frontier()
        self._refresh_known()
        self._sweep_lifecycle()
        s = {**dict(self.stats), **self.db.stats(include_fixtures=True)}
        self.db.finish_run(self.run_id, s)
        self.out(f"== run {self.run_id} done: " + ", ".join(f"{k}={v}" for k, v in sorted(self.stats.items())) + " ==")
        return s

    # ================================================================= discovery
    def _seed(self):
        for u in self.cfg.get("seeds", []):
            u = canonical_url(u)
            info = classify(u, self.cfg)
            if self.db.add_candidate(u, 0, "seed", info.source_type, info.strength, recrawl=1, priority=100):
                self.stats["candidates_new"] += 1

    def _discover_search(self):
        d = self.cfg.get("discovery", {})
        if not d.get("enable_search", False):
            return
        for q in d.get("search_queries", []):
            for u in search(q, self.cfg, d.get("results_per_query", 10)):
                u = canonical_url(u)
                info = classify(u, self.cfg)
                if self.db.add_candidate(u, 0, f"search:{q[:60]}", info.source_type, info.strength, priority=candidate_priority(u, "", 2)):
                    self.stats["candidates_new"] += 1
                    self.stats["discovered_via_search"] += 1

    def _candidate_links(self, page, depth, page_info):
        kws = re.compile("|".join(self.cfg.get("discovery", {}).get("scholarship_link_keywords", ["scholarship", "fellowship"])), re.I)
        out, seen = [], set()
        for href, text in page.links:
            if not href.startswith(("http://", "https://")):
                continue
            path = urlsplit(href).path.lower()
            if path.endswith(SKIP_EXT) or SKIP_PATH.search(href):
                continue
            if not (kws.search(text or "") or kws.search(path)):
                continue
            c = canonical_url(href)
            if c in seen:
                continue
            seen.add(c)
            info = classify(c, self.cfg)
            same = registrable_domain(host_of(c)) == registrable_domain(host_of(page.url))
            if info.source_type == AGGREGATOR and not page_info.source_type == AGGREGATOR:
                continue                                  # official pages never lead us into aggregators
            if same or info.official:
                out.append((c, info, candidate_priority(c, text)))
        return out[:80]

    def _mine_aggregator(self, page, depth):
        """Aggregator pages are discovery-only: harvest outbound links to *official* hosts, store nothing from them."""
        n = 0
        for href, text in page.links:
            if not href.startswith(("http://", "https://")):
                continue
            c = canonical_url(href)
            info = classify(c, self.cfg)
            if info.official and not SKIP_PATH.search(c) and not urlsplit(c).path.lower().endswith(SKIP_EXT):
                if self.db.add_candidate(c, depth + 1, "aggregator-mined", info.source_type, info.strength, priority=candidate_priority(c, text, 4)):
                    n += 1
                    self.stats["candidates_new"] += 1
                if n >= 25:
                    break
        self.stats["official_links_mined_from_aggregators"] += n

    # ================================================================= crawl loop
    def _crawl_frontier(self):
        maxp, maxd = int(self.c.get("max_pages_per_run", 100)), int(self.c.get("max_depth", 2))
        fetched = 0
        while fetched < maxp:
            cand = self.db.next_candidate(maxd)
            if not cand:
                break
            url = cand["url"]
            fr = self.fetcher.fetch(url)
            fetched += 1
            self.stats["pages_fetched"] += 1
            if not fr.ok:
                note = f"[{fr.kind}] {fr.error or 'HTTP ' + str(fr.status)}"
                if fr.kind == "transient":      # 5xx / timeout / 429: retry on a later run instead of discarding
                    self.db.defer_candidate(url, float(self.c.get("retry_after_hours", 6)), note)
                    self.stats["fetch_deferred"] += 1
                else:
                    self.db.mark_candidate(url, "failed", note)
                    self.stats["fetch_failed"] += 1
                self.stats[f"fail_{fr.kind}"] += 1
                self.out(f"  [fail] {url} ({fr.error or fr.status})")
                continue
            try:
                self._process_candidate(cand, fr)
            except Exception as e:                       # one bad page must never kill a run
                log.exception("processing failed: %s", url)
                self.db.mark_candidate(url, "failed", f"processing error: {e}"[:200])
                self.stats["processing_errors"] += 1

    def _process_candidate(self, cand, fr: FetchResult):
        url, depth = cand["url"], cand["depth"]
        info = classify(fr.final_url or url, self.cfg)
        page = parse_page(fr.text, fr.final_url or url, fr.is_pdf)
        if info.source_type == AGGREGATOR:
            self._mine_aggregator(page, depth)
            self.db.mark_candidate(url, "done", "aggregator: mined for official links only")
            self.out(f"  [aggr] {url} -> mined official links")
            return
        links = self._candidate_links(page, depth, info)
        a = self._assess(fr, page, info, prev=None)
        if not a["is_scholarship"] or (len(links) >= 15 and a["rec"].get("closing_date", NOT_SPECIFIED) == NOT_SPECIFIED):
            new = 0
            for c, ci, pr in links:
                if self.db.add_candidate(c, depth + 1, f"link:{url[:80]}", ci.source_type, ci.strength, priority=pr):
                    new += 1
            self.stats["candidates_new"] += new
            self.stats["links_discovered"] += new
            self.db.mark_candidate(url, "done", f"listing/non-scholarship page, {new} new links", recrawl=1 if links else 0)
            self.out(f"  [list] {url} -> {new} new candidate links")
            return
        for c, ci, pr in links:                          # scholarship pages may also link to siblings
            if self.db.add_candidate(c, depth + 1, f"link:{url[:80]}", ci.source_type, ci.strength, priority=pr):
                self.stats["candidates_new"] += 1
                self.stats["links_discovered"] += 1
        sid, kind = self._store(url, fr, a, via=cand["discovered_via"])
        self.touched.add(sid)
        self.db.mark_candidate(url, "done", f"record {sid} ({kind})")
        self.out(f"  [{kind:>7}] {a['rec']['name'][:60]!r} conf={a['confidence']} {a['verification']} status={a['status']}")

    # ================================================================= assessment (extract + verify)
    def _assess(self, fr: FetchResult, page, info, prev):
        host = host_of(page.url)
        hints = self.cfg.get("provider_hints", {})
        hint = next((v for k, v in hints.items() if host == k or host.endswith("." + k)), "")
        rules, close_dates, extended = extract_rules(page, hint, host)
        name = rules["name"].value if "name" in rules else None
        if not looks_like_scholarship(page, name):
            return {"is_scholarship": False, "rec": {}}
        struct = extract_structural(page)
        llm = self.llm.extract(page) if self.llm.available() else {}
        merged = dict(rules)
        for src_ in (struct, llm):                       # fill gaps only; rules stay primary
            for f, ev in src_.items():
                merged.setdefault(f, ev)
        accepted, rejected = ground(merged, page.corpus)

        # --- agreement between independent extractors (rules vs structural/LLM)
        other = {**llm, **struct}
        comp = agree = 0
        dis = []
        for f in CMP_FIELDS:
            a_, b_ = rules.get(f), other.get(f)
            if not a_ or not b_:
                continue
            if not (quote_grounded(a_.quote, page.corpus) and quote_grounded(b_.quote, page.corpus)
                    and value_supported(f, a_.value, a_.quote) and value_supported(f, b_.value, b_.quote)):
                continue
            comp += 1
            same = (a_.value == b_.value) if f in ("closing_date", "opening_date") else \
                (re.sub(r"[^\d.]", "", a_.value) == re.sub(r"[^\d.]", "", b_.value) if f in ("amount", "income_limit")
                 else norm_text_key(a_.value) == norm_text_key(b_.value))
            agree += same
            if not same:
                dis.append(f)

        distinct = set(close_dates)
        conflict = 1.0 if len(distinct) <= 1 else (0.5 if extended else 0.0)

        rec = {f: NOT_SPECIFIED for f in FIELDS}
        rec["application_url"] = "Not available"
        for f, ev in accepted.items():
            if ev.grounded or ev.method == "derived":
                rec[f] = ev.value

        app_status, app_official = 0, False
        if "application_url" in accepted and accepted["application_url"].grounded:
            app = accepted["application_url"].value
            app_status = self.fetcher.check_url(app)
            app_official = classify(app, self.cfg).official

        closing = rec["closing_date"] if rec["closing_date"] != NOT_SPECIFIED else None
        csig = closed_signal(page)
        status, status_why = compute_status(closing, csig, self.today, int(self.c.get("expiring_soon_days", 14)))

        key_now = {f: rec[f] for f in ("closing_date", "amount", "eligibility_text")}
        stable = None
        if prev is not None:
            stable = all(norm_text_key(prev[f] or "") == norm_text_key(key_now[f]) for f in key_now)

        name_in_body = bool(name) and norm_text_key(name) in norm_text_key(page.full)
        offdomain = registrable_domain(host_of(fr.final_url or fr.url)) != registrable_domain(host_of(fr.url))
        ctx = dict(evs=accepted, source=info, fetch=fr, today=self.today, is_scholarship_page=True, name_in_body=name_in_body,
                   offdomain_redirect=offdomain, app_status=app_status, app_official=app_official, url=fr.url,
                   conflict_level=conflict, disagreements=dis, comparable=comp, agree=agree, stable=stable, full=page.full)
        conf, verification, checks, gates = score_record(ctx, self.cfg)
        return dict(is_scholarship=True, rec=rec, evs=accepted, rejected=rejected, confidence=conf, verification=verification,
                    checks=checks, gates=gates, status=status, status_why=status_why, closed_signal=csig or "",
                    app_status=app_status, info=info, page=page, content_hash=sha(page.full))

    # ================================================================= store + change detection
    def _store(self, url, fr, a, via=""):
        fp = sha(canonical_url(url))
        row = self.db.find_by_fp(fp)
        now = now_iso()
        rec, info = a["rec"], a["info"]
        breakdown = json.dumps({"checks": a["checks"], "gates": a["gates"], "status_reason": a["status_why"],
                                "rejected_unsupported": [list(r) for r in a["rejected"]], "discovered_via": via})
        base = dict(name=rec["name"], source_type=info.source_type, official_strength=info.strength, official_url=url,
                    application_url_status=a["app_status"], status=a["status"], verification=a["verification"],
                    confidence=a["confidence"], score_breakdown=breakdown, closed_signal=a["closed_signal"],
                    last_seen=now, last_verified=now, consecutive_failures=0, content_hash=a["content_hash"],
                    is_fixture=int(info.is_fixture), **{f: rec[f] for f in FIELDS if f != "name"})
        if row is None:
            sid = self.db.insert_scholarship({**base, "fingerprint": fp, "first_seen": now, "last_changed": now})
            self.db.add_evidence(sid, a["evs"], url, self.run_id)
            self.db.log_change(sid, "DISCOVERED", "record", "", rec["name"], url, f"first found via {via}", self.run_id)
            self.db.add_score_history(sid, self.run_id, a["confidence"], a["verification"], a["status"])
            self.db.add_snapshot(sid, self.run_id, fr, a["content_hash"], a["page"].full)
            self.stats["new_records"] += 1
            return sid, "NEW"

        sid, changed = row["id"], []
        for f in FIELDS:
            old, new = row[f] or NOT_SPECIFIED, rec[f]
            diff = (norm_text_key(old) != norm_text_key(new)) if f in LONG_FIELDS else (old != new)
            if diff:
                ev = a["evs"].get(f)
                if ev is not None:
                    evtxt = f"\"{ev.quote[:600]}\""
                else:
                    prev_ev = self.db.con.execute("SELECT quote FROM field_evidence WHERE scholarship_id=? AND field=? AND superseded_at IS NULL "
                                                  "ORDER BY id DESC LIMIT 1", (sid, f)).fetchone()
                    evtxt = f"field no longer found on source; previously supported by: \"{(prev_ev['quote'] if prev_ev else '')[:400]}\""
                self.db.log_change(sid, "CHANGED", f, old, new, url, evtxt, self.run_id)
                changed.append(f)
                self.stats["changes_detected"] += 1
                self.out(f"      CHANGE DETECTED  {rec['name'][:40]!r}.{f}: {old[:40]!r} -> {new[:40]!r}")
        if row["status"] != a["status"]:
            self.db.log_change(sid, "STATUS", "status", row["status"], a["status"], url, a["status_why"], self.run_id)
            if a["status"] == "EXPIRED":
                self.stats["expired_detected"] += 1
        if changed:
            self.db.supersede_evidence(sid, changed)
            self.db.add_evidence(sid, {f: a["evs"][f] for f in changed if f in a["evs"]}, url, self.run_id)
            base["last_changed"] = now
        have = {r["field"] for r in self.db.con.execute("SELECT field FROM field_evidence WHERE scholarship_id=? AND superseded_at IS NULL", (sid,))}
        self.db.add_evidence(sid, {f: ev for f, ev in a["evs"].items() if f not in have and f not in changed}, url, self.run_id)
        self.db.update_scholarship(sid, base)
        self.db.add_score_history(sid, self.run_id, a["confidence"], a["verification"], a["status"])
        self.db.add_snapshot(sid, self.run_id, fr, a["content_hash"], a["page"].full)
        self.stats["records_changed" if changed else "records_unchanged"] += 1
        return sid, ("CHANGED" if changed else "SAME")

    # ================================================================= re-verification of known records
    def _refresh_known(self):
        limit = int(self.c.get("recrawl_limit", 300))
        n = 0
        for row in self.db.all_scholarships():
            if row["id"] in self.touched or n >= limit:
                continue
            n += 1
            url = row["official_url"]
            fr = self.fetcher.fetch(url)
            self.stats["pages_refetched"] += 1
            if not fr.ok:
                reason = "removed from official source (HTTP %s)" % fr.status if fr.kind == "not_found" else \
                    f"unreachable [{fr.kind}]: {fr.error or 'HTTP ' + str(fr.status)}"
                self._register_failure(row, fr, reason)
                continue
            info = classify(fr.final_url or url, self.cfg)
            page = parse_page(fr.text, fr.final_url or url, fr.is_pdf)
            a = self._assess(fr, page, info, prev=row)
            if not a["is_scholarship"]:
                self._register_failure(row, fr, "page no longer describes this scholarship")
                continue
            sid, kind = self._store(url, fr, a, via="re-verification")
            self.touched.add(sid)
            self.out(f"  [{kind:>7}] re-verified {rec_name(a)!r} conf={a['confidence']} status={a['status']}")

    def _register_failure(self, row, fr, reason):
        fails = (row["consecutive_failures"] or 0) + 1
        need = int(self.c.get("unverifiable_after_failures", 2))
        new_status = "NO_LONGER_VERIFIABLE" if fails >= need else "REVIEW_REQUIRED"
        bd = json.loads(row["score_breakdown"] or "{}")
        conf = row["confidence"] or 0
        for ch in bd.get("checks", []):
            if ch["name"] in ("live_on_source", "freshness") and ch["points"]:
                conf -= ch["points"]
                ch.update(score=0.0, points=0.0, detail=f"re-verification failed: {reason}")
        bd["gates"] = list(bd.get("gates", [])) + [f"re-verification failed: {reason}"]
        self.db.update_scholarship(row["id"], dict(consecutive_failures=fails, status=new_status, verification="REVIEW_REQUIRED",
                                                   confidence=round(max(conf, 0), 1), score_breakdown=json.dumps(bd), last_seen=now_iso()))
        if row["status"] != new_status:
            self.db.log_change(row["id"], "UNAVAILABLE", "status", row["status"], new_status, row["official_url"],
                               f"{reason}; failures={fails}", self.run_id)
            self.stats["unavailable_detected"] += 1
            self.out(f"      STALE DETECTED  {row['name'][:50]!r}: {row['status']} -> {new_status} ({reason})")
        self.touched.add(row["id"])
        self.db.add_score_history(row["id"], self.run_id, round(max(conf, 0), 1), "REVIEW_REQUIRED", new_status)

    # ================================================================= time-based lifecycle sweep
    def _sweep_lifecycle(self):
        soon = int(self.c.get("expiring_soon_days", 14))
        for row in self.db.all_scholarships():
            if row["status"] == "NO_LONGER_VERIFIABLE":
                continue
            closing = row["closing_date"] if row["closing_date"] != NOT_SPECIFIED else None
            st, why = compute_status(closing, row["closed_signal"], self.today, soon)
            if st != row["status"]:
                self.db.update_scholarship(row["id"], {"status": st})
                self.db.log_change(row["id"], "STATUS", "status", row["status"], st, row["official_url"], why, self.run_id)
                if st == "EXPIRED":
                    self.stats["expired_detected"] += 1


def rec_name(a):
    return a["rec"]["name"][:60]
