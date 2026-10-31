"""Evidence-first extraction.

Every extracted value is paired with a verbatim *quote* from the source page. A value that cannot be
traced to a quote found in the page text is rejected and stored as "Not specified" (never guessed).
Two independent extractors (rules + structural/JSON-LD/table) are run so their agreement can be scored.
"""
import json
import re
import datetime as dt
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .util import norm_ws, NOT_SPECIFIED

I = re.I

FIELDS = ["name", "provider", "amount", "eligibility_text", "academic_requirements", "education_level",
          "income_limit", "age_criteria", "gender_criteria", "category_criteria", "domicile",
          "institution_requirements", "opening_date", "closing_date", "documents_required",
          "selection_process", "renewal_requirements", "application_url"]

# ----------------------------------------------------------------------------- dates
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
DATE_RES = [
    ("dmy", re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-/.,]*(?:of\s+)?" + _MON + r"\.?[\s\-/.,]*(\d{4})\b", I)),
    ("mdy", re.compile(r"\b" + _MON + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", I)),
    ("num", re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b")),      # Indian convention DD/MM/YYYY
    ("iso", re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")),
]


def find_dates(text: str):
    """Return [(iso, start, end)] sorted by position, overlaps removed."""
    out = []
    for kind, rx in DATE_RES:
        for m in rx.finditer(text):
            g = m.groups()
            try:
                if kind == "dmy":
                    d, mo, y = int(g[0]), _MONTHS[g[1][:3].lower()], int(g[2])
                elif kind == "mdy":
                    mo, d, y = _MONTHS[g[0][:3].lower()], int(g[1]), int(g[2])
                elif kind == "num":
                    d, mo, y = int(g[0]), int(g[1]), int(g[2])
                else:
                    y, mo, d = int(g[0]), int(g[1]), int(g[2])
                iso = dt.date(y, mo, d).isoformat()
            except (ValueError, KeyError):
                continue
            out.append((iso, m.start(), m.end()))
    out.sort(key=lambda t: (t[1], -(t[2] - t[1])))
    res, last_end = [], -1
    for t in out:
        if t[1] >= last_end:
            res.append(t)
            last_end = t[2]
    return res


# ----------------------------------------------------------------------------- page model
@dataclass
class Ev:
    value: str
    quote: str
    method: str = "rule"
    grounded: bool = False
    note: str = ""


@dataclass
class Page:
    url: str
    title: str = ""
    h1: str = ""
    segments: list = field(default_factory=list)
    full: str = ""
    links: list = field(default_factory=list)       # [(abs_url, anchor_text)]
    jsonld: list = field(default_factory=list)
    kv: list = field(default_factory=list)          # [(key, value)] from tables / definition lists
    corpus: str = ""                                # everything a quote may legitimately come from


def _segments(lines):
    segs = []
    for line in lines:
        line = norm_ws(line)
        if not line:
            continue
        if len(line) > 280:
            segs.extend(p for p in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line) if p)
        else:
            segs.append(line)
    return segs


def _flatten_json(o, acc):
    if isinstance(o, dict):
        for v in o.values():
            _flatten_json(v, acc)
    elif isinstance(o, list):
        for v in o:
            _flatten_json(v, acc)
    elif isinstance(o, (str, int, float)):
        acc.append(str(o))


def parse_page(raw: str, url: str, is_pdf: bool = False) -> Page:
    p = Page(url=url)
    if is_pdf:
        p.segments = _segments(raw.splitlines())
        p.title = p.segments[0] if p.segments else ""
        p.h1 = p.title
        p.full = " ".join(p.segments)
        p.corpus = norm_ws(p.title + " " + p.full)
        return p
    try:
        soup = BeautifulSoup(raw, "lxml")
    except Exception:
        soup = BeautifulSoup(raw, "html.parser")
    p.title = norm_ws(soup.title.get_text(" ")) if soup.title else ""
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or s.get_text() or "")
            p.jsonld.extend(data if isinstance(data, list) else [data])
        except Exception:
            pass
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        p.links.append((urljoin(url, href), norm_ws(a.get_text(" "))))
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        if len(cells) == 2:
            k, v = norm_ws(cells[0].get_text(" ")), norm_ws(cells[1].get_text(" "))
            if k and v and len(k) < 80:
                p.kv.append((k, v))
    for dl in soup.find_all("dl"):
        for dt_, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
            k, v = norm_ws(dt_.get_text(" ")), norm_ws(dd.get_text(" "))
            if k and v:
                p.kv.append((k, v))
    h1 = soup.find("h1")
    p.h1 = norm_ws(h1.get_text(" ")) if h1 else ""
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        t.decompose()
    p.segments = _segments(soup.get_text("\n").splitlines())
    p.full = " ".join(p.segments)
    js = []
    _flatten_json(p.jsonld, js)
    p.corpus = norm_ws(" ".join([p.title, p.full] + js))
    return p


# ----------------------------------------------------------------------------- generic scan helpers
def scan(segs, kw_re, fn, spans=(1, 2)):
    for i, s in enumerate(segs):
        if not kw_re.search(s):
            continue
        for sp in spans:
            w = " ".join(segs[i:i + sp])
            v = fn(w)
            if v:
                return v, w
    return None


AMT_RE = re.compile(r"(?:₹|Rs\.?|INR|USD|US\$|\$|£|€)\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:lakhs?|lacs?|crore|thousand|million|k)\b)?"
                    r"(?:\s?(?:per|/|a)\s?(?:month|annum|year|semester|yr|mo|course)\b)?", I)
LAKH_RE = re.compile(r"\d+(?:\.\d+)?\s?(?:lakhs?|lacs?|crore)\b", I)
AMT_CTX = re.compile(r"scholarship|stipend|amount|fellowship|award|grant|assistance|tuition|benefit|worth|up to|financial|per month|per annum|per year", I)
BENEFIT_RE = re.compile(r"(?:full|100\s?%)\s+tuition(?:\s+fee)?(?:\s+waiver)?|tuition\s+fee\s+(?:waiver|reimbursement)|fully[\s-]funded|fee\s+waiver", I)


def _amount(w):
    if re.search(r"income|salary|turnover", w, I) and not re.search(r"scholarship amount|stipend|award", w, I):
        return None
    ms = []
    for m in AMT_RE.finditer(w):
        t = norm_ws(m.group(0))
        if t not in ms:
            ms.append(t)
    return "; ".join(ms[:3]) if ms else None


def _income(w):
    qual = r"(?:(?:not\s+exceed(?:ing)?|less\s+than|below|upto|up\s+to|maximum|max\.?|under|within|exceeding)\s+)?"
    m = re.search(qual + r"(?:" + AMT_RE.pattern + r"|" + LAKH_RE.pattern + r")", w, I)
    if m:
        return norm_ws(m.group(0))
    m = re.search(r"no\s+income\s+(?:limit|ceiling|criteri\w+)|income\s+(?:limit|ceiling)\s+does\s+not\s+apply", w, I)
    return norm_ws(m.group(0)) if m else None


def _age(w):
    pats = [r"between\s+\d{1,2}\s+(?:and|to|-)\s+\d{1,2}\s+years",
            r"(?:below|under|not\s+(?:exceed(?:ing)?|more\s+than)|up\s*to|maximum|max\.?|less\s+than|above|at\s+least|minimum|not\s+less\s+than)\s+(?:the\s+age\s+of\s+)?\d{1,2}\s+years",
            r"aged?\s+(?:\w+\s+){0,3}\d{1,2}\s*(?:years|yrs)", r"\d{1,2}\s*(?:to|-)\s*\d{1,2}\s+years\s+of\s+age"]
    for p in pats:
        m = re.search(p, w, I)
        if m:
            return norm_ws(m.group(0))
    return None


def _gender(w):
    m = re.search(r"only\s+(?:for\s+)?(?:girls?|female|women)|(?:girl|female|women)s?\s+(?:students?|candidates?|applicants?)\s+(?:only|are\s+eligible)|exclusively\s+for\s+(?:girls?|women)|for\s+(?:girl|female|women)\s+students?", w, I)
    return norm_ws(m.group(0)) if m else None


CAT_TOKENS = [("SC", r"\bSC\b|(?i:scheduled\s+castes?)"), ("ST", r"\bST\b|(?i:scheduled\s+tribes?)"),
              ("OBC", r"\bOBC\b|(?i:other\s+backward)"), ("EWS", r"\bEWS\b|(?i:economically\s+weaker)"),
              ("Minority", r"(?i:minorit(?:y|ies))"),
              ("PwD/Divyang", r"\bPwD\b|(?i:divyang|persons?\s+with\s+disabilit\w+|differently[\s-]abled)"),
              ("DNT/PVTG", r"\bDNT\b|\bPVTG\b|(?i:denotified)")]


def _category(w):
    found = [n for n, rx in CAT_TOKENS if re.search(rx, w)]
    return ", ".join(found) if found else None


def _domicile(w):
    m = re.search(r"(?:permanent|bonafide|bona\s+fide)\s+(?:resident|domicile)[^.;,]{0,40}|domicile\s+of\s+[A-Z][\w ]{2,30}|resident\s+of\s+[A-Z][\w ]{2,30}|citizens?\s+of\s+India|Indian\s+(?:national|citizen)s?|domicile\s+certificate", w)
    if not m:
        m = re.search(r"citizens?\s+of\s+India|Indian\s+(?:national|citizen)s?|domicile", w, I)
    return norm_ws(m.group(0)) if m else None


LEVELS = [("Class 6-8", r"(?i:class(?:es)?\s*(?:vi|vii|viii|6|7|8)\b)"),
          ("Class 9-10", r"(?i:class(?:es)?\s*(?:ix|x|9|10)\b|matric(?:ulation)?\b)"),
          ("Class 11-12", r"(?i:class(?:es)?\s*(?:xi|xii|11|12)\b|higher\s+secondary|intermediate|10\s?\+\s?2)"),
          ("Diploma/ITI", r"(?i:diploma|polytechnic)|\bITI\b"),
          ("Undergraduate", r"(?i:undergraduate|bachelor|b\.?\s?tech|b\.?\s?sc\b)|\bUG\b|\bB\.?E\.?\b|\bMBBS\b|\bB\.?Com\b"),
          ("Postgraduate", r"(?i:postgraduate|post-graduate|master'?s?\b|m\.?\s?tech|m\.?\s?sc\b)|\bPG\b|\bMBA\b"),
          ("PhD/Research", r"(?i:ph\.?\s?d|doctoral|doctorate|research\s+scholar|post[\s-]?doc)|\bJRF\b")]
ELIG_KW = re.compile(r"eligib|pursuing|studying|enrolled|admitted|open\s+to|available\s+(?:to|for)|applicable|for\s+students|candidate|who\s+can\s+apply", I)


def _levels(w):
    return [n for n, rx in LEVELS if re.search(rx, w)]


def _academic(w):
    m = re.search(r"(?:minimum|at\s+least|not\s+less\s+than|secured?|obtained?|scored?)?\s*\d{2}(?:\.\d+)?\s?%\s*(?:marks|or\s+above|and\s+above|aggregate)?|\bCGPA\s*(?:of)?\s*\d(?:\.\d+)?|first\s+(?:class|division)", w, I)
    return norm_ws(m.group(0)) if m else None


INST_RE = re.compile(r"recogni[sz]ed\s+(?:university|institution|college|board)|AICTE|UGC|NAAC|NIRF|government\s+(?:college|school|institution)|affiliated\s+(?:to|with)|regular\s+(?:full[\s-]time\s+)?students?", I)


def _inst(w):
    if not INST_RE.search(w):
        return None
    if len(w) <= 200:
        return norm_ws(w)
    m = INST_RE.search(w)
    st = w.rfind(" ", 0, max(0, m.start() - 40)) + 1
    en = w.find(" ", min(len(w), m.end() + 80))
    return norm_ws(w[st: en if en > 0 else len(w)])


def _is_heading(seg: str) -> bool:
    return len(seg) <= 45 and not seg.endswith((".", ";", ":", ",")) and len(seg.split()) <= 6 and seg[:1].isupper()


def section_window(segs, i, max_chars, kind):
    """Heading at segs[i] plus the segments that belong to that section, stopping at the next heading
    (eligibility: bullet sentences) or at the first full sentence (documents: short list items)."""
    out, n = [segs[i]], len(segs[i])
    for seg in segs[i + 1:i + 14]:
        if kind == "eligibility" and _is_heading(seg):
            break
        if kind == "documents" and (len(seg) > 45 or seg.endswith(".") or _is_heading(seg) and re.search(r"apply|selection|renew|eligib|how\s+to", seg, I)):
            break
        if n + len(seg) + 1 > max_chars:
            break
        out.append(seg)
        n += len(seg) + 1
    return " ".join(out)

# ----------------------------------------------------------------------------- rule extractor
CLOSE_KW = re.compile(r"last\s+(?:date|day)|closing\s+date|clos(?:e|es|ed|ing)\s+(?:on|by)|deadline|due\s+date|apply\s+(?:on\s+or\s+)?(?:before|by|till|until)|(?:till|until)\s|applications?\s+(?:will\s+)?(?:close|end)|end\s+date", I)
OPEN_KW = re.compile(r"opening\s+date|start(?:ing)?\s+date|open(?:s|ed)?\s+(?:on|from)|commence(?:s|ment)?|applications?\s+(?:will\s+)?(?:open|start|begin)|from\s+date", I)
WINDOW_KW = re.compile(r"window|period|duration|apply\s+(?:online\s+)?(?:from|between)|applications?\s+(?:are\s+)?(?:invited|open)", I)
ROLLING = re.compile(r"open\s+throughout\s+the\s+year|rolling\s+basis|no\s+(?:fixed\s+)?(?:last|closing)\s+date|round\s+the\s+year|accepted\s+throughout|year[\s-]round", I)
EXT_RE = re.compile(r"extended|extension|revised|re-?opened", I)
CLOSED_RE = re.compile(r"(?:applications?|registrations?|window|portal)\s+(?:are\s+|is\s+|has\s+|have\s+)?(?:now\s+)?(?:been\s+)?closed|last\s+date\s+(?:has\s+)?(?:been\s+)?(?:over|passed|expired)|(?:scholarship|scheme)\s+(?:has\s+been\s+)?(?:discontinued|withdrawn)|no\s+longer\s+(?:accepting|available)", I)
RANGE_SEP = re.compile(r"^\s*(?:to|till|until|through|–|—|-|and)\s*$", I)


def closing_candidates(segs):
    """All (iso, window, extended) closing-date candidates found, plus a 'rolling' quote if present."""
    cands, rolling = [], None
    for i, s in enumerate(segs):
        if ROLLING.search(s) and not rolling:
            rolling = s
        has_close = CLOSE_KW.search(s)
        has_win = WINDOW_KW.search(s) and re.search(r"apply|application", s, I)
        if not (has_close or has_win):
            continue
        for sp in (1, 2):
            w = " ".join(segs[i:i + sp])
            ds = find_dates(w)
            if not ds:
                continue
            ext = bool(EXT_RE.search(w))
            if has_close:
                m = CLOSE_KW.search(w)
                after = [d for d in ds if d[1] >= m.start()] or ds
                is_range = len(after) >= 2 and RANGE_SEP.match(w[after[-2][2]:after[-1][1]])
                cands.append(((after[-1] if is_range else after[0])[0], w, ext))
            elif len(ds) >= 2 and RANGE_SEP.match(w[ds[0][2]:ds[1][1]]):
                cands.append((ds[1][0], w, ext))
            else:
                continue
            break
    seen, uniq = set(), []
    for c in cands:
        if (c[0], c[1]) not in seen:
            seen.add((c[0], c[1]))
            uniq.append(c)
    return uniq, rolling


def extract_name(p: Page):
    for o in p.jsonld:
        if isinstance(o, dict) and o.get("name") and re.search(r"grant|scholar|fellow|award|incentive", json.dumps(o.get("@type", "")), I):
            return Ev(norm_ws(str(o["name"])), norm_ws(str(o["name"])), "jsonld")
    if p.h1 and len(p.h1) > 6:
        return Ev(p.h1, p.h1, "rule")
    if p.title:
        parts = [x.strip() for x in re.split(r"\s[|\-–—:]\s", p.title) if x.strip()]
        best = max((x for x in parts if re.search(r"scholar|fellow|award|scheme|grant|stipend", x, I)), key=len, default=None)
        if best:
            return Ev(best, best, "rule")
    return None


PROV_RE = re.compile(r"((?:offered|awarded|provided|launched|sponsored|implemented|funded|administered)\s+by\s+(?:the\s+)?"
                     r"([A-Z][\w&.,' ()-]{3,90}?))(?:[.;]|\s+(?:for|to|under|in)\b|$)")


def extract_provider(p: Page, hint: str = ""):
    for o in p.jsonld:
        if isinstance(o, dict):
            for k in ("provider", "funder", "sponsor", "organizer"):
                v = o.get(k)
                n = v.get("name") if isinstance(v, dict) else v if isinstance(v, str) else None
                if n:
                    return Ev(norm_ws(n), norm_ws(n), "jsonld")
    for s in p.segments:
        m = PROV_RE.search(s)
        if m:
            return Ev(norm_ws(m.group(2)), norm_ws(m.group(1)), "rule")
    if hint:
        return Ev(hint, "", "derived", note="derived from domain mapping; no textual quote")
    return None


def extract_application_url(p: Page, source_host: str):
    best = None
    for href, text in p.links:
        if not text or len(text) > 80:
            continue
        if re.search(r"\bapply\b|apply\s+now|online\s+application|application\s+form|register|registration|click\s+here\s+to\s+apply", text, I) \
                and not re.search(r"how\s+to|guideline|instruction|manual|brochure|login\s+help", text, I):
            if urlsplit(href).path.lower().endswith((".pdf", ".doc", ".docx", ".zip")):
                continue
            score = (urlsplit(href).hostname == source_host) + bool(re.search(r"apply", text, I))
            if best is None or score > best[0]:
                best = (score, href, text)
    return Ev(best[1], best[2], "rule", note="anchor text; href is the value") if best else None


def extract_rules(p: Page, provider_hint: str = "", source_host: str = ""):
    segs, out = p.segments, {}

    def put(field_, ev):
        if ev and ev.value:
            out[field_] = ev

    put("name", extract_name(p))
    put("provider", extract_provider(p, provider_hint))
    put("application_url", extract_application_url(p, source_host))

    cands, rolling = closing_candidates(segs)
    if cands:
        ext = [c for c in cands if c[2]]
        pick = max(ext, key=lambda c: c[0]) if ext else cands[0]
        put("closing_date", Ev(pick[0], pick[1], "rule", note="extended" if ext else ""))
    elif rolling:
        put("closing_date", Ev("Rolling (no fixed deadline)", rolling, "rule"))
    r = scan(segs, OPEN_KW, lambda w: (find_dates(w)[0][0] if find_dates(w) else None))
    if r:
        put("opening_date", Ev(r[0], r[1], "rule"))
    elif cands:
        for i, s in enumerate(segs):
            ds = find_dates(s)
            if len(ds) >= 2 and WINDOW_KW.search(s):
                put("opening_date", Ev(ds[0][0], s, "rule"))
                break

    r = scan(segs, AMT_CTX, _amount) or scan(segs, BENEFIT_RE, lambda w: (norm_ws(BENEFIT_RE.search(w).group(0)) if BENEFIT_RE.search(w) else None), spans=(1,))
    if r:
        put("amount", Ev(r[0], r[1], "rule"))
    r = scan(segs, re.compile(r"income", I), _income)
    if r:
        put("income_limit", Ev(r[0], r[1], "rule"))
    r = scan(segs, re.compile(r"\bage\b|aged|years", I), _age)
    if r:
        put("age_criteria", Ev(r[0], r[1], "rule"))
    r = scan(segs, re.compile(r"girl|female|women", I), _gender)
    if r:
        put("gender_criteria", Ev(r[0], r[1], "rule"))
    r = scan(segs, re.compile(r"categor|caste|\bSC\b|\bST\b|OBC|EWS|minorit|disabilit|divyang|PwD|tribe", I), _category, spans=(1,))
    if r:
        put("category_criteria", Ev(r[0], r[1], "rule"))
    r = scan(segs, re.compile(r"domicile|resident|citizen|national", I), _domicile, spans=(1,))
    if r:
        put("domicile", Ev(r[0], r[1], "rule"))
    r = scan(segs, re.compile(r"\d\s?%|CGPA|first\s+(?:class|division)|marks|percentage", I), _academic, spans=(1,))
    if r:
        put("academic_requirements", Ev(r[0], r[1], "rule"))
    r = scan(segs, re.compile(r"institution|college|university|school|enrolled|recogni", I), _inst, spans=(1,))
    if r:
        put("institution_requirements", Ev(r[0], r[1], "rule"))

    best_lv, best_seg = [], None
    for s in segs:
        if ELIG_KW.search(s):
            lv = _levels(s)
            if len(lv) > len(best_lv):
                best_lv, best_seg = lv, s
    if best_lv:
        put("education_level", Ev(", ".join(best_lv), best_seg, "rule"))

    for i, s in enumerate(segs):
        if re.match(r"^\W*(?:eligibility(?:\s+criteria)?|who\s+can\s+apply)\W*$", s, I) and i + 1 < len(segs):
            q = section_window(segs, i, 900, "eligibility")
            put("eligibility_text", Ev(q, q, "rule"))
            break
    else:
        r = scan(segs, re.compile(r"\beligible\b|eligibility\s+criteria", I), lambda w: w if len(w) > 40 else None)
        if r:
            put("eligibility_text", Ev(r[1], r[1], "rule"))

    for i, s in enumerate(segs):
        if re.match(r"^\W*(?:documents?\s+(?:required|needed)|required\s+documents|list\s+of\s+documents)\W*$", s, I) and i + 1 < len(segs):
            q = section_window(segs, i, 600, "documents")
            put("documents_required", Ev(q, q, "rule"))
            break
    else:
        r = scan(segs, re.compile(r"aadhaar|marksheet|income\s+certificate|caste\s+certificate|bank\s+(?:passbook|account)", I),
                 lambda w: w if len(re.findall(r"aadhaar|marksheet|certificate|passbook|photograph|bonafide|id\s+proof", w, I)) >= 2 else None, spans=(1,))
        if r:
            put("documents_required", Ev(r[1], r[1], "rule"))

    r = scan(segs, re.compile(r"selection\s+(?:will\s+be|is|process|criteria)|shortlist|merit\s+list|selected\s+(?:on|based|through)", I), lambda w: w if len(w) > 25 else None, spans=(1,))
    if r:
        put("selection_process", Ev(r[1], r[1], "rule"))
    r = scan(segs, re.compile(r"renew", I), lambda w: w if len(w) > 20 else None, spans=(1,))
    if r:
        put("renewal_requirements", Ev(r[1], r[1], "rule"))
    return out, [c[0] for c in cands], bool(any(c[2] for c in cands))


def closed_signal(p: Page):
    for s in p.segments:
        m = CLOSED_RE.search(s)
        if m:
            return s
    return None


# ----------------------------------------------------------------------------- structural extractor (independent method)
_KV_MAP = [("closing_date", r"last\s+date|closing\s+date|deadline|end\s+date|apply\s+before"),
           ("opening_date", r"opening\s+date|start\s+date|applications?\s+open"),
           ("amount", r"^(?:scholarship\s+)?(?:amount|benefit|award|stipend|value)|scholarship\s+(?:amount|value)"),
           ("provider", r"^(?:provider|offered\s+by|organi[sz]ation|sponsor|funded\s+by|awarding\s+body|implemented\s+by)"),
           ("income_limit", r"income")]


def extract_structural(p: Page):
    out = {}
    for k, v in p.kv:
        for field_, rx in _KV_MAP:
            if field_ in out or not re.search(rx, k, I):
                continue
            quote = f"{k} {v}"
            if field_ in ("closing_date", "opening_date"):
                ds = find_dates(v)
                if ds:
                    out[field_] = Ev(ds[-1][0] if field_ == "closing_date" else ds[0][0], quote, "structural")
            elif field_ == "amount":
                a = _amount(v)
                if a:
                    out[field_] = Ev(a, quote, "structural")
            elif field_ == "income_limit":
                a = _income(v)
                if a:
                    out[field_] = Ev(a, quote, "structural")
            else:
                out[field_] = Ev(v[:120], quote, "structural")
    for o in p.jsonld:
        if not isinstance(o, dict):
            continue
        for key, f in (("validThrough", "closing_date"), ("endDate", "closing_date"), ("validFrom", "opening_date"), ("startDate", "opening_date")):
            if o.get(key) and f not in out:
                ds = find_dates(str(o[key]))
                if ds:
                    out[f] = Ev(ds[0][0], str(o[key]), "structural")
    return out


# ----------------------------------------------------------------------------- grounding (anti-hallucination gate)
def quote_grounded(quote: str, corpus: str) -> bool:
    q = norm_ws(quote)
    return len(q) >= 3 and q in corpus


def value_supported(field_: str, value: str, quote: str) -> bool:
    if field_ in ("closing_date", "opening_date"):
        if value.startswith("Rolling"):
            return bool(ROLLING.search(quote))
        return value in {d[0] for d in find_dates(quote)}
    if field_ in ("amount", "income_limit"):
        digits = re.findall(r"\d[\d,]*(?:\.\d+)?", value)
        qd = {x.replace(",", "") for x in re.findall(r"\d[\d,]*(?:\.\d+)?", quote)}
        return all(d.replace(",", "") in qd for d in digits)
    if field_ == "application_url":
        return True
    return True


def ground(evs: dict, corpus: str):
    """Keep only evidence whose quote is verbatim in the page and whose value follows from the quote.
    Returns (accepted, rejected[(field, value, reason)])."""
    ok, rejected = {}, []
    for f, ev in evs.items():
        if ev.method == "derived":
            ev.grounded = False
            ok[f] = ev                      # kept but never counts as grounded evidence
            continue
        if not quote_grounded(ev.quote, corpus):
            rejected.append((f, ev.value, "quote not found verbatim in source"))
            continue
        if not value_supported(f, ev.value, ev.quote):
            rejected.append((f, ev.value, "value not derivable from quote"))
            continue
        ev.grounded = True
        ok[f] = ev
    return ok, rejected


# ----------------------------------------------------------------------------- scholarship page detection
KW = re.compile(r"scholarship|fellowship|stipend|financial\s+assistance|bursary|\bgrant\b", I)


def looks_like_scholarship(p: Page, name: str | None) -> bool:
    n = len(KW.findall(p.full))
    return (n >= 3 and len(p.full) >= 400 and bool(re.search(r"eligib|who can apply|criteria", p.full, I))
            and bool(re.search(r"apply|application", p.full, I))
            and bool(name and re.search(r"scholar|fellow|scheme|yojana|award|prize|grant|stipend|bursar|assistance|programme|program", name, I)))
