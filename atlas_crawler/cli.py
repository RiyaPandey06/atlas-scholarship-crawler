"""CLI:  python -m atlas_crawler.cli <crawl|schedule|stats|export|demo> [--config config.yaml]"""
import argparse
import csv
import json
import logging
import os
import sys
import time

import yaml

from .db import DB
from .pipeline import Crawler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def print_stats(db: DB, include_fixtures=False):
    s = db.stats(include_fixtures)
    print("\n=== Scholarship Intelligence ===")
    for k in ("total_discovered", "verified", "review_required", "active", "expiring_soon", "expired",
              "no_longer_verifiable", "recently_updated_7d", "avg_confidence", "conf_ge_95"):
        print(f"  {k:<24}{s[k]}")
    print("  source_types            ", s["source_types"])


def cmd_crawl(a):
    cfg = load_config(a.config)
    if a.max_pages:
        cfg["crawl"]["max_pages_per_run"] = a.max_pages
    db = DB(cfg["db_path"])
    Crawler(cfg, db).run()
    print_stats(db, include_fixtures="demo" in a.config)


def cmd_schedule(a):
    cfg = load_config(a.config)
    db = DB(cfg["db_path"])
    while True:
        try:
            Crawler(cfg, db).run()
        except Exception:
            logging.exception("run failed")
        print(f"sleeping {a.every_hours}h ...")
        time.sleep(a.every_hours * 3600)


def cmd_stats(a):
    cfg = load_config(a.config)
    print_stats(DB(cfg["db_path"]), include_fixtures=a.fixtures)


def cmd_export(a):
    cfg = load_config(a.config)
    db = DB(cfg["db_path"])
    rows = db.all_scholarships(include_fixtures=a.fixtures)
    if not rows:
        print("no records")
        return
    cols = [c for c in rows[0].keys() if c not in ("score_breakdown",)]
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r[c] for c in cols])
    print(f"wrote {len(rows)} records to {a.out}")


def cmd_inspect(a):
    """Dry-run one URL: fetch, classify, extract, ground, score. Stores nothing. Use it to tune/debug on real pages."""
    from .classify import classify
    from .extract import parse_page
    from .fetcher import Fetcher
    cfg = load_config(a.config)
    cr = Crawler(cfg, DB(":memory:"))
    fr = Fetcher(cfg).fetch(a.url)
    print(f"HTTP {fr.status} {fr.content_type} final={fr.final_url} err={fr.error!r} ssl_unverified={fr.ssl_unverified}")
    if not fr.ok:
        return
    info = classify(fr.final_url, cfg)
    print(f"source: {info.source_type} strength={info.strength} ({info.reason})")
    page = parse_page(fr.text, fr.final_url, fr.is_pdf)
    res = cr._assess(fr, page, info, prev=None)
    if not res["is_scholarship"]:
        print("-> not recognised as a single-scholarship page (would be treated as listing / skipped)")
        return
    print(f"\nCONFIDENCE {res['confidence']}  {res['verification']}  status={res['status']} ({res['status_why']})")
    for c in res["checks"]:
        print(f"  {c['name']:<24} w={c['weight']:<3} score={c['score']:<5} pts={c['points']:<5} {c['detail'][:110]}")
    if res["gates"]:
        print("  gates:", res["gates"])
    print("\nFIELDS (value | method | quote)")
    for f, ev in res["evs"].items():
        print(f"  {f:<24} {ev.value[:60]!r:<64} {ev.method:<10} \"{ev.quote[:90]}\"")
    for f, v, why in res["rejected"]:
        print(f"  REJECTED {f}: {v[:50]!r} ({why})")
    print("\nNot specified:", [f for f, v in res["rec"].items() if v in ("Not specified", "Not available")])


def cmd_diagnose(a):
    """Where are points being lost? Aggregates the stored score breakdowns and summarises fetch failures."""
    import collections
    from urllib.parse import urlsplit
    cfg = load_config(a.config)
    db = DB(cfg["db_path"])
    rows = db.all_scholarships(include_fixtures=a.fixtures)
    print(f"\n=== {len(rows)} records ===")
    lost, nlow = collections.Counter(), collections.Counter()
    table = []
    for r in rows:
        bd = json.loads(r["score_breakdown"] or "{}")
        per = []
        for c in bd.get("checks", []):
            d = c["weight"] - c["points"]
            if d > 0.05:
                lost[c["name"]] += d
                nlow[c["name"]] += 1
                per.append((d, c["name"], c["detail"]))
        per.sort(reverse=True)
        table.append((r["confidence"] or 0, r, per, bd.get("gates", [])))
    print("\nPoints lost per check across all records (this is what to fix first):")
    for k, v in lost.most_common():
        print(f"  {k:<24} total_lost={v:6.1f}  records_affected={nlow[k]}/{len(rows)}  avg_lost={v / max(len(rows), 1):.1f}")
    print("\nRecords (lowest confidence first):")
    for conf, r, per, gates in sorted(table, key=lambda t: t[0])[: a.worst]:
        print(f"\n#{r['id']} conf={conf} {r['verification']} status={r['status']} {r['source_type']}\n  {r['name'][:80]}\n  {r['official_url']}")
        for d, name, detail in per[:4]:
            print(f"    -{d:4.1f} {name}: {detail[:100]}")
        if gates:
            print("    gates:", gates)
    print("\nCandidate frontier by state:")
    for st_ in db.con.execute("SELECT state, COUNT(*) n FROM candidates GROUP BY state"):
        print(f"  {st_['state']:<8}{st_['n']}")
    print("\nFailures by host and kind (top 15):")
    fails = collections.Counter()
    for c in db.con.execute("SELECT url, note FROM candidates WHERE state='failed'"):
        kind = (c["note"] or "").split("]")[0].lstrip("[") if (c["note"] or "").startswith("[") else "unknown"
        fails[(urlsplit(c["url"]).hostname, kind)] += 1
    for (h, k), n in fails.most_common(15):
        print(f"  {n:4} {h}  [{k}]")
    print("\nSample failure notes:")
    for c in db.con.execute("SELECT url, note FROM candidates WHERE state='failed' LIMIT 8"):
        print(f"  {c['url'][:90]}  ->  {(c['note'] or '')[:90]}")


def cmd_demo(a):
    """Two crawl runs against the local fictional site: run 1 discovers, run 2 detects change/removal/new."""
    sys.path.insert(0, ROOT)
    from demo import mock_site
    cfg = load_config(os.path.join(ROOT, "config.demo.yaml"))
    if os.path.exists(cfg["db_path"]):
        os.remove(cfg["db_path"])
    srv = mock_site.start(8765, version=1)
    db = DB(cfg["db_path"])
    print("\n############ RUN 1 (site version 1) ############")
    Crawler(cfg, db).run()
    mock_site.STATE["version"] = 2
    print("\n############ RUN 2 (site version 2: deadline changed, amount changed, one removed, one new) ############")
    Crawler(cfg, db).run()
    srv.shutdown()
    print_stats(db, include_fixtures=True)
    print("\nChange log (excluding DISCOVERED):")
    for r in db.con.execute("SELECT s.name, c.change_type, c.field, c.old_value, c.new_value FROM change_log c "
                            "JOIN scholarships s ON s.id=c.scholarship_id WHERE c.change_type!='DISCOVERED' ORDER BY c.id"):
        print(f"  {r['change_type']:<11} {r['name'][:46]:<46} {r['field']}: {str(r['old_value'])[:30]!r} -> {str(r['new_value'])[:30]!r}")
    print(f"\nInspect: ATLAS_CONFIG=config.demo.yaml streamlit run app.py   (db: {cfg['db_path']})")


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="atlas_crawler")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("crawl", cmd_crawl), ("schedule", cmd_schedule), ("stats", cmd_stats), ("export", cmd_export), ("demo", cmd_demo), ("inspect", cmd_inspect), ("diagnose", cmd_diagnose)):
        sp = sub.add_parser(name)
        sp.add_argument("--config", default=os.path.join(ROOT, "config.yaml"))
        sp.set_defaults(fn=fn)
        if name == "crawl":
            sp.add_argument("--max-pages", type=int)
        if name == "inspect":
            sp.add_argument("url")
        if name == "diagnose":
            sp.add_argument("--worst", type=int, default=6)
            sp.add_argument("--fixtures", action="store_true")
        if name == "schedule":
            sp.add_argument("--every-hours", type=float, default=24)
        if name in ("stats", "export"):
            sp.add_argument("--fixtures", action="store_true", help="include demo/test-fixture records")
        if name == "export":
            sp.add_argument("--out", default="scholarships_export.csv")
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
