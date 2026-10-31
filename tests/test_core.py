import datetime as dt
import json
import os
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from atlas_crawler.classify import classify
from atlas_crawler.db import DB
from atlas_crawler.extract import (find_dates, parse_page, extract_rules, ground, quote_grounded, value_supported)
from atlas_crawler.llm import parse_llm_json
from atlas_crawler.pipeline import Crawler, compute_status
from atlas_crawler.util import canonical_url, NOT_SPECIFIED
from atlas_crawler.verify import score_record

CFG = yaml.safe_load(open(os.path.join(ROOT, "config.yaml")))


# ------------------------------------------------------------------ dates
@pytest.mark.parametrize("text,iso", [
    ("Last date: 31 August 2026", "2026-08-31"), ("closing 31st Aug, 2026", "2026-08-31"),
    ("by August 31, 2026", "2026-08-31"), ("deadline 31/08/2026", "2026-08-31"),
    ("2026-08-31", "2026-08-31"), ("15-Sep-2026", "2026-09-15")])
def test_find_dates(text, iso):
    assert find_dates(text)[0][0] == iso


def test_invalid_date_ignored():
    assert find_dates("31 February 2026") == []


# ------------------------------------------------------------------ classification
def test_classification_is_suffix_based():
    assert classify("https://scholarships.gov.in/x", CFG).source_type == "GOVERNMENT"
    assert classify("https://gov.in.evil.com/x", CFG).strength < 0.9
    assert classify("https://www.buddy4study.com/page/x", CFG).source_type == "AGGREGATOR"
    assert classify("https://iitb.ac.in/sch", CFG).source_type == "UNIVERSITY"
    assert classify("https://www.tatatrusts.org/x", CFG).official
    assert classify("https://randomscholarshiphub.com", CFG).source_type == "AGGREGATOR"


def test_canonical_url_strips_tracking():
    assert canonical_url("HTTPS://Example.GOV.in/a/?utm_source=x&id=2#frag") == "https://example.gov.in/a?id=2"


# ------------------------------------------------------------------ anti-hallucination
HTML = """<html><head><title>Test Merit Scholarship | Foo</title></head><body><h1>Test Merit Scholarship</h1>
<p>This scholarship is offered by The Foo Trust. The scholarship gives financial assistance for study.</p>
<p>Scholarship amount: ₹10,000 per annum</p><p>Last date to apply: 31 August 2026</p>
<h2>Eligibility</h2><p>Students pursuing an undergraduate course are eligible to apply for this scholarship.</p>
<a href="/apply">Apply Now</a></body></html>"""


def test_missing_fields_are_not_invented():
    page = parse_page(HTML, "https://foo.example/s")
    evs, _, _ = extract_rules(page)
    assert "income_limit" not in evs          # page never mentions income
    assert "age_criteria" not in evs
    assert evs["closing_date"].value == "2026-08-31"
    assert evs["amount"].value.startswith("₹10,000")


def test_ground_rejects_fabricated_quote_and_unsupported_value():
    page = parse_page(HTML, "https://foo.example/s")
    from atlas_crawler.extract import Ev
    evs = {"income_limit": Ev("₹5 lakh", "Family income below ₹5 lakh", "llm"),                    # quote not on page
           "closing_date": Ev("2026-09-15", "Last date to apply: 31 August 2026", "llm"),          # value not in quote
           "amount": Ev("₹10,000 per annum", "Scholarship amount: ₹10,000 per annum", "llm")}      # legit
    ok, rejected = ground(evs, page.corpus)
    assert set(ok) == {"amount"}
    assert {r[0] for r in rejected} == {"income_limit", "closing_date"}


def test_llm_json_requires_quote_and_date_in_quote():
    raw = json.dumps({"closing_date": {"value": "2026-08-31", "quote": "no date here"},
                      "amount": {"value": "₹1", "quote": "₹1 only"}, "domicile": {"value": "India"}})
    out = parse_llm_json(raw)
    assert "closing_date" not in out and "domicile" not in out and "amount" in out


# ------------------------------------------------------------------ lifecycle + scoring
def test_status_rules():
    t = dt.date(2026, 10, 30)
    assert compute_status("2026-10-29", None, t, 14)[0] == "EXPIRED"
    assert compute_status("2026-11-05", None, t, 14)[0] == "EXPIRING_SOON"
    assert compute_status("2027-01-01", None, t, 14)[0] == "ACTIVE"
    assert compute_status("2027-01-01", "Applications are now closed", t, 14)[0] == "EXPIRED"
    assert compute_status(None, None, t, 14)[0] == "REVIEW_REQUIRED"
    assert compute_status("Rolling (no fixed deadline)", None, t, 14)[0] == "ACTIVE"


class _FR:
    status, ssl_unverified, last_modified = 200, False, ""


def _score(source_url, **over):
    page = parse_page(HTML, source_url)
    evs, _, _ = extract_rules(page, "", "foo.example")
    evs, _ = ground(evs, page.corpus)
    ctx = dict(evs=evs, source=classify(source_url, CFG), fetch=_FR(), today=dt.date(2026, 6, 1), is_scholarship_page=True,
               name_in_body=True, offdomain_redirect=False, app_status=200, app_official=True, url=source_url,
               conflict_level=1.0, disagreements=[], comparable=3, agree=3, stable=None, full=page.full)
    ctx.update(over)
    return score_record(ctx, CFG)


def test_weights_sum_to_100():
    assert sum(CFG["scoring"]["weights"].values()) == 100


def test_aggregator_source_can_never_be_verified():
    conf, verdict, checks, gates = _score("https://www.buddy4study.com/page/test")
    assert verdict == "REVIEW_REQUIRED" and "source not classified official" in gates


def test_unexplained_conflict_blocks_verification():
    conf, verdict, _, gates = _score("https://scholarships.gov.in/x", conflict_level=0.0)
    assert verdict == "REVIEW_REQUIRED" and any("conflict" in g for g in gates)


def test_score_is_sum_of_check_points():
    conf, _, checks, _ = _score("https://scholarships.gov.in/x")
    assert conf == round(sum(c["points"] for c in checks), 1)


# ------------------------------------------------------------------ end to end (local fictional site, 2 runs)
def test_end_to_end_two_runs(tmp_path):
    from demo import mock_site
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config.demo.yaml")))
    cfg["db_path"] = str(tmp_path / "t.db")
    srv = mock_site.start(0, version=1)
    port = srv.server_address[1]
    cfg["seeds"] = [f"http://127.0.0.1:{port}/", f"http://localhost:{port}/aggregator"]
    db = DB(cfg["db_path"])
    try:
        out = lambda *_: None
        Crawler(cfg, db, out=out).run()
        assert len(db.all_scholarships()) == 7
        mock_site.STATE["version"] = 2
        Crawler(cfg, db, out=out).run()
    finally:
        srv.shutdown()

    rows = {r["name"]: r for r in db.all_scholarships()}
    assert len(rows) == 8                                                   # +1 discovered via aggregator, official source kept
    delta = rows["DEMO Green Energy Research Scholarship"]
    assert delta["official_url"].startswith("http://127.0.0.1") and delta["source_type"] == "TEST_FIXTURE"
    assert not any("localhost" in r["official_url"] for r in rows.values())  # aggregator never becomes a source

    ch = db.con.execute("SELECT * FROM change_log WHERE change_type='CHANGED'").fetchall()
    assert {(c["field"]) for c in ch} == {"closing_date", "amount"}
    c = [c for c in ch if c["field"] == "closing_date"][0]
    assert c["old_value"] and c["new_value"] and c["old_value"] != c["new_value"] and c["detected_at"] and c["evidence"] and c["source_url"]
    assert db.con.execute("SELECT COUNT(*) FROM field_evidence WHERE superseded_at IS NOT NULL").fetchone()[0] >= 2   # history kept

    assert rows["DEMO Women in Engineering Scholarship"]["status"] == "NO_LONGER_VERIFIABLE"
    assert rows["DEMO Legacy Merit Scholarship 2025"]["status"] == "EXPIRED"
    assert rows["DEMO Science Excellence Scholarship"]["status"] == "EXPIRED"
    assert rows["DEMO Final Year Project Scholarship"]["status"] == "EXPIRING_SOON"
    assert rows["DEMO Merit Scholarship for Undergraduate Students"]["verification"] == "VERIFIED"
    assert rows["DEMO Women in Engineering Scholarship"]["verification"] == "REVIEW_REQUIRED"
    # a field absent from the page is never invented
    assert rows["DEMO Merit Scholarship for Undergraduate Students"]["gender_criteria"] == NOT_SPECIFIED


# ------------------------------------------------------------------ v1.1: fetch robustness, retry, priority, migration
def _fetch_with(monkeypatch, body: bytes, ctype: str, status=200, url="https://x.gov.in/a.pdf"):
    from atlas_crawler.fetcher import Fetcher

    class R:
        def __init__(s):
            s.status_code, s.url, s.headers, s.encoding, s.apparent_encoding = status, url, {"Content-Type": ctype}, "utf-8", "utf-8"
        def iter_content(s, n): yield body
        def close(s): pass
    f = Fetcher({"crawl": {"per_domain_delay_sec": 0, "respect_robots": False}})
    monkeypatch.setattr(f, "_get", lambda *a, **k: R())
    return f.fetch(url)


def test_fake_pdf_html_page_is_parsed_as_html(monkeypatch):
    r = _fetch_with(monkeypatch, b"<html><body>Scholarship notice</body></html>", "application/pdf")
    assert r.ok and not r.is_pdf and "Scholarship notice" in r.text


def test_invalid_pdf_bytes_are_flagged_not_crashed(monkeypatch):
    r = _fetch_with(monkeypatch, b"\x00\x01garbage", "application/pdf")
    assert not r.ok and r.kind == "invalid_content" and "not a valid PDF" in r.error


def test_error_kinds(monkeypatch):
    assert _fetch_with(monkeypatch, b"x", "text/html", 404).kind == "not_found"
    assert _fetch_with(monkeypatch, b"x", "text/html", 500).kind == "transient"
    assert _fetch_with(monkeypatch, b"x", "text/html", 403).kind == "blocked"


def test_transient_failures_are_deferred_then_abandoned(tmp_path):
    db = DB(str(tmp_path / "d.db"))
    db.add_candidate("https://a.gov.in/x", 0, "seed", "GOVERNMENT", 1.0)
    for i in range(2):
        db.defer_candidate("https://a.gov.in/x", 6, "[transient] HTTP 500")
        row = db.con.execute("SELECT * FROM candidates").fetchone()
        assert row["state"] == "new" and row["next_retry"]
        assert db.next_candidate(2) is None                      # not picked again until next_retry
    db.defer_candidate("https://a.gov.in/x", 6, "[transient] HTTP 500")
    assert db.con.execute("SELECT state FROM candidates").fetchone()["state"] == "failed"


def test_priority_prefers_specific_scholarship_pages():
    from atlas_crawler.pipeline import candidate_priority, SKIP_PATH
    good = candidate_priority("https://x.gov.in/guidelines-scholarship-2026-27.pdf", "Scholarship guidelines 2026-27")
    bland = candidate_priority("https://x.gov.in/about-us", "About us")
    assert good > bland + 5
    assert SKIP_PATH.search("https://x.gov.in/tenders/list") and not SKIP_PATH.search("https://x.gov.in/schemes/post-matric-scholarship")


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE candidates (url TEXT PRIMARY KEY, depth INTEGER, discovered_via TEXT, source_type TEXT, "
                "official_strength REAL, state TEXT DEFAULT 'new', recrawl INTEGER DEFAULT 0, first_seen TEXT, last_checked TEXT, note TEXT)")
    con.commit(); con.close()
    db = DB(path)
    cols = {r[1] for r in db.con.execute("PRAGMA table_info(candidates)")}
    assert {"priority", "attempts", "next_retry"} <= cols
