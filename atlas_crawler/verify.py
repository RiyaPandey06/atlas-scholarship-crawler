"""Confidence methodology.

confidence = sum(weight_i * score_i) over nine independently computed, evidence-based checks.
No LLM output is ever used as (or to produce) a confidence number. Every check records its inputs, so the
dashboard can show *why* a score is what it is.

VERIFIED requires: confidence >= threshold (default 95) AND hard gates:
  - the source is classified as official (strength >= 0.9)
  - the scholarship is live on that source (HTTP 200 + scholarship page + name present)
  - no unexplained conflicting values
Anything else is REVIEW_REQUIRED.
"""
import datetime as dt
import re
from urllib.parse import urlsplit

from .extract import find_dates
from .util import registrable_domain

KEY_FIELDS = ["name", "provider", "closing_date", "amount", "eligibility_text", "application_url"]
STRUCTURED_ELIG = ["education_level", "income_limit", "age_criteria", "gender_criteria", "category_criteria",
                   "domicile", "academic_requirements", "institution_requirements"]

DEFAULT_WEIGHTS = {"official_source": 20, "live_on_source": 15, "application_url": 10, "deadline_supported": 15,
                   "eligibility_supported": 12, "freshness": 5, "no_conflicts": 5,
                   "extraction_consistency": 8, "traceability": 10}


def _g(evs, f):
    ev = evs.get(f)
    return ev if ev is not None and ev.grounded else None


def score_record(ctx: dict, cfg: dict):
    weights = {**DEFAULT_WEIGHTS, **cfg.get("scoring", {}).get("weights", {})}
    threshold = float(cfg.get("scoring", {}).get("verified_threshold", 95))
    evs, src, fr, today = ctx["evs"], ctx["source"], ctx["fetch"], ctx["today"]
    checks = []

    def add(name, s, detail):
        s = max(0.0, min(1.0, s))
        w = weights[name]
        checks.append({"name": name, "weight": w, "score": round(s, 3), "points": round(w * s, 2), "detail": detail})

    # 1 official source
    add("official_source", src.strength, f"{src.source_type}: {src.reason}")

    # 2 live on source
    live = 1.0 if (fr.status == 200 and ctx.get("is_scholarship_page")) else 0.0
    notes = [f"HTTP {fr.status}", "scholarship page detected" if ctx.get("is_scholarship_page") else "not recognised as scholarship page"]
    if live and not ctx.get("name_in_body"):
        live, _ = 0.8, notes.append("name only in <title>/metadata, not in body")
    if fr.ssl_unverified:
        f = float(cfg.get("scoring", {}).get("ssl_unverified_factor", 0.5))
        live *= f
        notes.append(f"TLS certificate could not be verified (x{f})")
    if ctx.get("offdomain_redirect"):
        live *= 0.5
        notes.append("redirected to a different domain (x0.5)")
    add("live_on_source", live, "; ".join(notes))

    # 3 application URL
    app = _g(evs, "application_url")
    if not app:
        add("application_url", 0.0, "no application link found on the official page")
    else:
        st = ctx.get("app_status", 0)
        same = registrable_domain(urlsplit(app.value).hostname or "") == registrable_domain(urlsplit(ctx["url"]).hostname or "")
        if 200 <= st < 400:
            add("application_url", 1.0 if (same or ctx.get("app_official")) else 0.7,
                f"{app.value} -> HTTP {st}" + ("" if (same or ctx.get("app_official")) else " (third-party host, not classified official)"))
        else:
            add("application_url", 0.3, f"{app.value} did not resolve (HTTP {st})")

    # 4 deadline supported
    cd = _g(evs, "closing_date")
    if cd and cd.value.startswith("Rolling"):
        add("deadline_supported", 0.85, "rolling/no fixed deadline, supported by quote")
    elif cd:
        add("deadline_supported", 1.0, f"{cd.value} supported by quote: \"{cd.quote[:90]}\"")
    else:
        add("deadline_supported", 0.0, "no deadline could be traced to source text")

    # 5 eligibility supported
    el = _g(evs, "eligibility_text")
    n_struct = sum(1 for f in STRUCTURED_ELIG if _g(evs, f))
    base = (0.5 if el else 0.0) + 0.5 * min(1.0, n_struct / 3)
    if not el:
        base = 0.5 * min(1.0, n_struct / 3)
    add("eligibility_supported", base, f"eligibility text {'grounded' if el else 'missing'}; {n_struct} structured criteria grounded")

    # 6 freshness
    closing = cd.value if cd else None
    if closing and closing.startswith("Rolling"):
        fresh, d = 0.9, "rolling scheme"
    elif closing:
        if closing >= today.isoformat():
            fresh, d = 1.0, f"deadline {closing} is in the future"
        else:
            fresh, d = 0.2, f"deadline {closing} has passed"
    else:
        years = [int(y) for y in re.findall(r"\b(20\d{2})\b", ctx.get("full", ""))]
        top = max(years) if years else 0
        fresh = 0.7 if top >= today.year else 0.4 if top == today.year - 1 else 0.1
        d = f"latest year mentioned on page: {top or 'none'}"
    if fr.last_modified:
        try:
            lm = dt.datetime.strptime(fr.last_modified[:25], "%a, %d %b %Y %H:%M:%S").date()
            if (today - lm).days <= 180:
                fresh, d = max(fresh, 0.8), d + f"; Last-Modified {lm}"
        except ValueError:
            pass
    add("freshness", fresh, d)

    # 7 conflicts
    cl = ctx.get("conflict_level", 1.0)
    dis = ctx.get("disagreements", [])
    s = cl - 0.5 * len(dis)
    det = {1.0: "no conflicting closing dates on page", 0.5: "multiple dates, explained by an extension notice", 0.0: "multiple unexplained closing dates"}[cl]
    if dis:
        det += "; extractor disagreement on: " + ", ".join(dis)
    add("no_conflicts", s, det)

    # 8 extraction consistency
    comp, agree = ctx.get("comparable", 0), ctx.get("agree", 0)
    if comp:
        cons, det = agree / comp, f"{agree}/{comp} fields agree between independent extractors"
    elif ctx.get("stable") is True:
        cons, det = 1.0, "key fields identical to the previous crawl"
    else:
        cons, det = 0.7, "no second extractor overlap and no prior crawl to compare (neutral 0.7)"
    if comp and ctx.get("stable") is False:
        cons *= 0.8
        det += "; key fields changed since last crawl (x0.8)"
    add("extraction_consistency", cons, det)

    # 9 traceability of key fields
    pts = 0.0
    missing = []
    for f in KEY_FIELDS:
        ev = evs.get(f)
        if ev is not None and ev.grounded:
            pts += 1
        elif ev is not None and ev.method == "derived":
            pts += 0.5
        else:
            missing.append(f)
    add("traceability", pts / len(KEY_FIELDS), f"{pts:g}/{len(KEY_FIELDS)} key fields traced to a quote" + (f"; missing: {', '.join(missing)}" if missing else ""))

    conf = round(sum(c["points"] for c in checks), 1)
    gates = []
    if not src.official:
        gates.append("source not classified official")
    if live < 0.8:
        gates.append("not confirmed live on source")
    if cl == 0.0:
        gates.append("unexplained conflicting values")
    verified = conf >= threshold and not gates
    return conf, ("VERIFIED" if verified else "REVIEW_REQUIRED"), checks, gates
