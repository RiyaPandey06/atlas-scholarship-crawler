"""Polite HTTP fetcher: robots.txt, per-domain delay, retries, PDF support."""
import io
import time
import logging
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlsplit
import requests

from .util import host_of, now_iso

log = logging.getLogger("atlas.fetch")


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0
    content_type: str = ""
    text: str = ""            # HTML source, or extracted PDF text
    is_pdf: bool = False
    error: str = ""
    ssl_unverified: bool = False
    kind: str = "ok"          # ok | not_found | blocked | transient | invalid_content
    last_modified: str = ""
    fetched_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.text)


class Fetcher:
    def __init__(self, cfg: dict):
        c = cfg.get("crawl", {})
        self.ua = cfg.get("user_agent", "AtlasScholarshipBot/1.0")
        self.delay = float(c.get("per_domain_delay_sec", 2.0))
        self.timeout = int(c.get("timeout_sec", 25))
        self.respect_robots = bool(c.get("respect_robots", True))
        self.insecure_ok = tuple(c.get("allow_insecure_ssl_for", []))
        self.max_bytes = int(c.get("max_bytes", 6_000_000))
        if c.get("use_os_trust_store"):
            try:        # fixes many .gov.in "unable to get local issuer certificate" errors (pip install truststore)
                import truststore
                truststore.inject_into_ssl()
            except Exception as e:
                log.warning("truststore unavailable: %s", e)
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": self.ua, "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.5",
                               "Accept-Language": "en-IN,en;q=0.9"})
        self._last: dict = {}
        self._robots: dict = {}

    def _wait(self, host: str):
        gap = time.time() - self._last.get(host, 0)
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last[host] = time.time()

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        s = urlsplit(url)
        base = f"{s.scheme}://{s.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.s.get(base + "/robots.txt", timeout=10)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                    self._robots[base] = rp
                elif r.status_code in (401, 403):
                    rp.parse(["User-agent: *", "Disallow: /"])
                    self._robots[base] = rp
                else:
                    self._robots[base] = None
            except Exception:
                self._robots[base] = None
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.ua, url)

    def _get(self, url, verify=True, method="GET", stream=False):
        return self.s.request(method, url, timeout=self.timeout, verify=verify,
                              allow_redirects=True, stream=stream)

    def fetch(self, url: str) -> FetchResult:
        res = FetchResult(url=url, final_url=url)
        if not self.allowed(url):
            res.error, res.kind = "blocked by robots.txt", "blocked"
            return res
        host = host_of(url)
        verify = True
        for attempt in range(3):
            self._wait(host)
            try:
                r = self._get(url, verify=verify, stream=True)
                body = b""
                for chunk in r.iter_content(65536):
                    body += chunk
                    if len(body) > self.max_bytes:
                        break
                res.status, res.final_url = r.status_code, r.url
                res.content_type = r.headers.get("Content-Type", "").lower()
                res.last_modified = r.headers.get("Last-Modified", "")
                head = body[:1024].lstrip()
                declared_pdf = "pdf" in res.content_type or r.url.lower().split("?")[0].endswith(".pdf")
                if head.startswith(b"%PDF"):
                    res.is_pdf = True
                    res.text = self._pdf_text(body)
                    if not res.text.strip():
                        res.error, res.kind = "PDF has no extractable text (scanned image or encrypted)", "invalid_content"
                elif declared_pdf and not head[:1] == b"<":
                    res.error, res.kind = "declared PDF but body is not a valid PDF", "invalid_content"
                elif declared_pdf or "html" in res.content_type or "text" in res.content_type or not res.content_type:
                    enc = r.encoding or r.apparent_encoding or "utf-8"      # includes servers that send an HTML page for a .pdf URL
                    res.text = body.decode(enc, errors="replace")
                else:
                    res.error, res.kind = f"unsupported content-type {res.content_type}", "invalid_content"
                if res.status in (404, 410) or (400 <= res.status < 500 and res.status not in (401, 403, 408, 429)):
                    res.kind = "not_found"
                elif res.status in (401, 403):
                    res.kind = "blocked"
                elif res.status in (408, 429) or res.status >= 500:
                    res.kind = "transient"
                if res.status >= 500 and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                return res
            except requests.exceptions.SSLError as e:
                if any(host.endswith(sfx) for sfx in self.insecure_ok) and verify:
                    verify = False      # many .gov.in sites have broken chains; flagged and penalised in scoring
                    res.ssl_unverified = True
                    continue
                res.error, res.kind = f"ssl error: {e}"[:200], "transient"
                return res
            except Exception as e:
                res.error, res.kind = f"{type(e).__name__}: {e}"[:200], "transient"
                time.sleep(2 ** attempt)
        return res

    @staticmethod
    def _pdf_text(body: bytes) -> str:
        try:
            from pypdf import PdfReader
            rd = PdfReader(io.BytesIO(body))
            return "\n".join((p.extract_text() or "") for p in rd.pages[:30])
        except Exception as e:
            log.warning("pdf parse failed: %s", e)
            return ""

    def check_url(self, url: str) -> int:
        """HTTP status of an application URL (0 = unreachable). HEAD first, GET fallback."""
        if not self.allowed(url):
            return 0
        host = host_of(url)
        for method in ("HEAD", "GET"):
            self._wait(host)
            try:
                r = self._get(url, method=method, stream=(method == "GET"))
                if method == "GET":
                    r.close()
                if r.status_code < 400 or method == "GET":
                    return r.status_code
            except requests.exceptions.SSLError:
                if not host.endswith(self.insecure_ok):
                    return 0
                try:
                    r = self._get(url, verify=False, method="GET", stream=True)
                    r.close()
                    return r.status_code
                except Exception:
                    return 0
            except Exception:
                if method == "GET":
                    return 0
        return 0
