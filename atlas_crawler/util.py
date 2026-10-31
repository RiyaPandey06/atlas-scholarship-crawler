import hashlib
import re
import datetime as dt
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

NOT_SPECIFIED = "Not specified"
TWO_LEVEL = {"gov.in", "nic.in", "ac.in", "edu.in", "org.in", "co.in", "res.in", "net.in",
             "gov.uk", "ac.uk", "co.uk", "org.uk", "gov.au", "edu.au", "com.au"}
_TRACKING = re.compile(r"^(utm_|fbclid|gclid|sessionid|phpsessid|jsessionid)", re.I)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_ws(s: str) -> str:
    return re.sub(r"[\s\u00a0\u200b\u200c\u200d\ufeff]+", " ", s or "").strip()


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "ignore")).hexdigest()


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def registrable_domain(host: str) -> str:
    if not host or re.fullmatch(r"[\d.]+", host) or "." not in host:
        return host
    p = host.split(".")
    if len(p) >= 3 and ".".join(p[-2:]) in TWO_LEVEL:
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def canonical_url(url: str) -> str:
    s = urlsplit(url.strip())
    netloc = (s.hostname or "").lower()
    if s.port and s.port not in (80, 443):
        netloc += f":{s.port}"
    q = [(k, v) for k, v in parse_qsl(s.query, keep_blank_values=True) if not _TRACKING.match(k)]
    path = s.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((s.scheme.lower(), netloc, path, urlencode(q), ""))


def norm_text_key(s: str) -> str:
    """Whitespace/punctuation-insensitive key used to compare long text fields."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())
