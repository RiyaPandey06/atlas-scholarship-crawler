"""Scholarship Intelligence dashboard.   streamlit run app.py
Use ATLAS_CONFIG=config.demo.yaml to inspect the offline demo database."""
import json
import os
import zlib

import pandas as pd
import streamlit as st
import yaml

from atlas_crawler.db import DB

st.set_page_config(page_title="Atlas Scholarship Intelligence", layout="wide")
CFG_PATH = os.environ.get("ATLAS_CONFIG", "config.yaml")
cfg = yaml.safe_load(open(CFG_PATH, encoding="utf-8"))
db = DB(cfg["db_path"])
is_demo = "demo" in CFG_PATH

st.title("Scholarship Intelligence")
st.caption(f"Database: `{cfg['db_path']}` · config: `{CFG_PATH}`")
if is_demo:
    st.warning("DEMO DATABASE: fictional test-fixture scholarships on a local site. Not real scholarship data.")
include_fx = is_demo or st.sidebar.checkbox("Include test-fixture records", value=False)

s = db.stats(include_fx)
cols = st.columns(7)
for c, (label, key) in zip(cols, [("Total discovered", "total_discovered"), ("Verified", "verified"), ("Review required", "review_required"),
                                  ("Active", "active"), ("Expired", "expired"), ("Updated (7d)", "recently_updated_7d"),
                                  ("Avg confidence", "avg_confidence")]):
    c.metric(label, f"{s[key]}%" if key == "avg_confidence" else s[key])
c2 = st.columns(3)
c2[0].metric("Confidence ≥ 95%", s["conf_ge_95"])
c2[1].metric("No longer verifiable", s["no_longer_verifiable"])
c2[2].metric("Expiring soon", s["expiring_soon"])
st.caption("Source types: " + ", ".join(f"{k}: {v}" for k, v in s["source_types"].items()))

rows = db.all_scholarships(include_fixtures=include_fx)
df = pd.DataFrame([dict(r) for r in rows])
if df.empty:
    st.info("No records yet. Run `python -m atlas_crawler.cli crawl` (or `... demo`).")
    st.stop()

f1, f2, f3, f4 = st.columns([3, 1.4, 1.4, 1.4])
q = f1.text_input("Search name / provider / eligibility", "")
sv = f2.multiselect("Verification", sorted(df.verification.unique()))
ss = f3.multiselect("Status", sorted(df.status.unique()))
st_ = f4.multiselect("Source type", sorted(df.source_type.unique()))
view = df.copy()
if q:
    m = view[["name", "provider", "eligibility_text", "amount", "category_criteria", "education_level"]].fillna("").agg(" ".join, axis=1)
    view = view[m.str.contains(q, case=False, regex=False)]
if sv:
    view = view[view.verification.isin(sv)]
if ss:
    view = view[view.status.isin(ss)]
if st_:
    view = view[view.source_type.isin(st_)]

st.subheader(f"Scholarships ({len(view)})")
show = view[["id", "name", "provider", "amount", "closing_date", "status", "verification", "confidence", "source_type", "last_verified"]]
st.dataframe(show, use_container_width=True, hide_index=True)

st.divider()
st.subheader("Scholarship details")
choice = st.selectbox("Open a scholarship", view.id.tolist(), format_func=lambda i: f"#{i} · {view[view.id == i].name.iloc[0]}") if len(view) else None
if choice:
    r = df[df.id == choice].iloc[0]
    left, right = st.columns([2, 1])
    left.markdown(f"### {r['name']}")
    left.markdown(f"**Provider:** {r.provider}  \n**Amount / benefit:** {r.amount}  \n**Deadline:** {r.closing_date} (opens: {r.opening_date})")
    left.markdown(f"**Official source:** [{r.official_url}]({r.official_url})  \n**Application link:** {r.application_url}"
                  f" (HTTP {r.application_url_status})")
    right.metric("Confidence", f"{r.confidence}%")
    right.markdown(f"**{r.verification}** · status **{r.status}**  \nlast verified: {r.last_verified}  \nfirst seen: {r.first_seen}")

    t1, t2, t3, t4, t5 = st.tabs(["Eligibility", "Why this score?", "Change history", "Source evidence", "Snapshot"])
    with t1:
        for lbl, k in [("Eligibility (verbatim)", "eligibility_text"), ("Education level", "education_level"),
                       ("Academic requirements", "academic_requirements"), ("Income criteria", "income_limit"),
                       ("Age criteria", "age_criteria"), ("Gender criteria", "gender_criteria"),
                       ("Category criteria", "category_criteria"), ("Domicile", "domicile"),
                       ("Institution requirements", "institution_requirements"), ("Documents required", "documents_required"),
                       ("Selection process", "selection_process"), ("Renewal", "renewal_requirements")]:
            st.markdown(f"**{lbl}:** {r[k]}")
    with t2:
        bd = json.loads(r.score_breakdown or "{}")
        chk = pd.DataFrame(bd.get("checks", []))
        if not chk.empty:
            st.dataframe(chk[["name", "weight", "score", "points", "detail"]], hide_index=True, use_container_width=True)
            st.markdown(f"**Total = {chk.points.sum():.1f} / 100.** VERIFIED requires ≥ {cfg['scoring']['verified_threshold']} and all gates passing.")
        if bd.get("gates"):
            st.error("Gates failed: " + "; ".join(bd["gates"]))
        st.caption("Status reason: " + bd.get("status_reason", ""))
        if bd.get("rejected_unsupported"):
            st.warning("Extractions rejected by the anti-hallucination gate: " + json.dumps(bd["rejected_unsupported"]))
        hist = pd.DataFrame([dict(x) for x in db.con.execute("SELECT run_id, at, confidence, verification, status FROM score_history WHERE scholarship_id=?", (int(choice),))])
        if not hist.empty:
            st.markdown("**Score history**")
            st.dataframe(hist, hide_index=True, use_container_width=True)
    with t3:
        ch = pd.DataFrame([dict(x) for x in db.changes_for(int(choice))])
        if ch.empty:
            st.info("No changes recorded.")
        else:
            st.dataframe(ch[["detected_at", "change_type", "field", "old_value", "new_value", "source_url", "evidence"]], hide_index=True, use_container_width=True)
    with t4:
        ev = pd.DataFrame([dict(x) for x in db.evidence_for(int(choice))])
        if not ev.empty:
            ev["current"] = ev.superseded_at.isna()
            st.caption("Database → Official source → Evidence → Extracted value. Superseded rows are kept for audit.")
            st.dataframe(ev[["field", "value", "quote", "source_url", "method", "grounded", "captured_at", "current"]], hide_index=True, use_container_width=True)
    with t5:
        snap = db.con.execute("SELECT fetched_at, http_status, text_blob FROM snapshots WHERE scholarship_id=? ORDER BY id DESC LIMIT 1", (int(choice),)).fetchone()
        if snap:
            st.caption(f"Fetched {snap['fetched_at']} (HTTP {snap['http_status']}) - page text the evidence quotes were checked against")
            st.text(zlib.decompress(snap["text_blob"]).decode("utf-8", "ignore")[:6000])

st.divider()
with st.expander("Crawl runs"):
    runs = pd.DataFrame([dict(x) for x in db.con.execute("SELECT id, started_at, finished_at, stats FROM crawl_runs ORDER BY id DESC LIMIT 20")])
    st.dataframe(runs, hide_index=True, use_container_width=True)
