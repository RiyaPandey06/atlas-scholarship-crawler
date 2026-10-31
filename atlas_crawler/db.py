import json
import os
import sqlite3
import zlib
import datetime as dt

from .util import now_iso

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        with open(SCHEMA, encoding="utf-8") as f:
            self.con.executescript(f.read())
        have = {r[1] for r in self.con.execute("PRAGMA table_info(candidates)")}
        for col, ddl in (("priority", "REAL DEFAULT 0"), ("attempts", "INTEGER DEFAULT 0"), ("next_retry", "TEXT")):
            if col not in have:                       # migrate databases created by v1.0
                self.con.execute(f"ALTER TABLE candidates ADD COLUMN {col} {ddl}")
        self.con.commit()

    # ------------------------------------------------------------ runs
    def start_run(self) -> int:
        cur = self.con.execute("INSERT INTO crawl_runs(started_at) VALUES (?)", (now_iso(),))
        self.con.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, stats: dict):
        self.con.execute("UPDATE crawl_runs SET finished_at=?, stats=? WHERE id=?", (now_iso(), json.dumps(stats), run_id))
        self.con.commit()

    # ------------------------------------------------------------ candidates
    def add_candidate(self, url, depth, via, source_type, strength, recrawl=0, priority=0.0) -> bool:
        cur = self.con.execute(
            "INSERT OR IGNORE INTO candidates(url,depth,discovered_via,source_type,official_strength,state,recrawl,first_seen,priority) "
            "VALUES (?,?,?,?,?,'new',?,?,?)", (url, depth, via, source_type, strength, recrawl, now_iso(), priority))
        self.con.commit()
        return cur.rowcount > 0

    def reset_recrawl(self):
        self.con.execute("UPDATE candidates SET state='new' WHERE recrawl=1")
        self.con.commit()

    def next_candidate(self, max_depth):
        return self.con.execute(
            "SELECT * FROM candidates WHERE state='new' AND depth<=? AND (next_retry IS NULL OR next_retry<=?) "
            "ORDER BY official_strength DESC, priority DESC, depth ASC, first_seen ASC LIMIT 1", (max_depth, now_iso())).fetchone()

    def defer_candidate(self, url, hours, note, max_attempts=3):
        """Transient failure (5xx, timeout, 429): retry later; give up after max_attempts."""
        row = self.con.execute("SELECT attempts FROM candidates WHERE url=?", (url,)).fetchone()
        n = (row["attempts"] or 0) + 1 if row else 1
        if n >= max_attempts:
            self.mark_candidate(url, "failed", f"gave up after {n} attempts: {note}")
            return
        nxt = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.con.execute("UPDATE candidates SET attempts=?, next_retry=?, note=?, last_checked=? WHERE url=?", (n, nxt, note, now_iso(), url))
        self.con.commit()

    def mark_candidate(self, url, state, note="", recrawl=None):
        if recrawl is None:
            self.con.execute("UPDATE candidates SET state=?, note=?, last_checked=? WHERE url=?", (state, note, now_iso(), url))
        else:
            self.con.execute("UPDATE candidates SET state=?, note=?, last_checked=?, recrawl=? WHERE url=?",
                             (state, note, now_iso(), recrawl, url))
        self.con.commit()

    # ------------------------------------------------------------ scholarships
    def find_by_fp(self, fp):
        return self.con.execute("SELECT * FROM scholarships WHERE fingerprint=?", (fp,)).fetchone()

    def insert_scholarship(self, rec: dict) -> int:
        cols = ",".join(rec)
        cur = self.con.execute(f"INSERT INTO scholarships({cols}) VALUES ({','.join('?' * len(rec))})", list(rec.values()))
        self.con.commit()
        return cur.lastrowid

    def update_scholarship(self, sid: int, upd: dict):
        sets = ",".join(f"{k}=?" for k in upd)
        self.con.execute(f"UPDATE scholarships SET {sets} WHERE id=?", list(upd.values()) + [sid])
        self.con.commit()

    def all_scholarships(self, include_fixtures=True):
        q = "SELECT * FROM scholarships" + ("" if include_fixtures else " WHERE is_fixture=0") + " ORDER BY id"
        return self.con.execute(q).fetchall()

    # ------------------------------------------------------------ evidence / history
    def add_evidence(self, sid, evs: dict, url, run_id):
        ts = now_iso()
        for f, ev in evs.items():
            self.con.execute(
                "INSERT INTO field_evidence(scholarship_id,field,value,quote,source_url,method,grounded,captured_at,run_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (sid, f, ev.value, ev.quote, url, ev.method, int(ev.grounded), ts, run_id))
        self.con.commit()

    def supersede_evidence(self, sid, fields):
        for f in fields:
            self.con.execute("UPDATE field_evidence SET superseded_at=? WHERE scholarship_id=? AND field=? AND superseded_at IS NULL",
                             (now_iso(), sid, f))
        self.con.commit()

    def log_change(self, sid, ctype, field, old, new, url, evidence, run_id):
        self.con.execute(
            "INSERT INTO change_log(scholarship_id,change_type,field,old_value,new_value,detected_at,source_url,evidence,run_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (sid, ctype, field, old, new, now_iso(), url, (evidence or "")[:1200], run_id))
        self.con.commit()

    def add_score_history(self, sid, run_id, conf, verification, status):
        self.con.execute("INSERT INTO score_history(scholarship_id,run_id,at,confidence,verification,status) VALUES (?,?,?,?,?,?)",
                         (sid, run_id, now_iso(), conf, verification, status))
        self.con.commit()

    def add_snapshot(self, sid, run_id, fr, content_hash, text):
        last = self.con.execute("SELECT content_hash FROM snapshots WHERE scholarship_id=? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        if last and last["content_hash"] == content_hash:
            return
        blob = zlib.compress(text[:400_000].encode("utf-8", "ignore"))
        self.con.execute("INSERT INTO snapshots(scholarship_id,run_id,fetched_at,http_status,content_hash,text_blob) VALUES (?,?,?,?,?,?)",
                         (sid, run_id, fr.fetched_at, fr.status, content_hash, blob))
        self.con.commit()

    # ------------------------------------------------------------ dashboard queries
    def stats(self, include_fixtures=False) -> dict:
        w = "" if include_fixtures else "WHERE is_fixture=0"
        q = lambda sql, *a: self.con.execute(sql, a).fetchone()[0]
        total = q(f"SELECT COUNT(*) FROM scholarships {w}")
        and_ = "WHERE" if not w else "WHERE"
        base = "" if include_fixtures else "is_fixture=0 AND "
        cnt = lambda cond: q(f"SELECT COUNT(*) FROM scholarships WHERE {base}{cond}")
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fx = "" if include_fixtures else "AND s.is_fixture=0"
        recent = q(f"SELECT COUNT(DISTINCT c.scholarship_id) FROM change_log c JOIN scholarships s ON s.id=c.scholarship_id "
                   f"WHERE c.change_type IN ('CHANGED','STATUS','UNAVAILABLE') AND c.detected_at>=? {fx}", since)
        avg = q(f"SELECT COALESCE(ROUND(AVG(confidence),1),0) FROM scholarships {w}")
        return {
            "total_discovered": total,
            "verified": cnt("verification='VERIFIED'"),
            "review_required": cnt("verification='REVIEW_REQUIRED'"),
            "active": cnt("status IN ('ACTIVE','EXPIRING_SOON')"),
            "expiring_soon": cnt("status='EXPIRING_SOON'"),
            "expired": cnt("status='EXPIRED'"),
            "no_longer_verifiable": cnt("status='NO_LONGER_VERIFIABLE'"),
            "recently_updated_7d": recent,
            "avg_confidence": avg,
            "conf_ge_95": cnt("confidence>=95"),
            "source_types": {r[0]: r[1] for r in self.con.execute(
                f"SELECT source_type, COUNT(*) FROM scholarships {w} GROUP BY source_type")},
        }

    def evidence_for(self, sid):
        return self.con.execute("SELECT * FROM field_evidence WHERE scholarship_id=? ORDER BY id", (sid,)).fetchall()

    def changes_for(self, sid):
        return self.con.execute("SELECT * FROM change_log WHERE scholarship_id=? ORDER BY id DESC", (sid,)).fetchall()
