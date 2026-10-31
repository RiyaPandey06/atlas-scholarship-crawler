"""Local FICTIONAL scholarship site used only for the offline demo/tests.
Two 'versions' simulate the same site between two crawl runs. Dates are relative to today so the demo never rots.
Nothing here is a real scholarship; every page says so."""
import datetime as dt
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"version": 1}
PORT = [8765]
TODAY = lambda: dt.date.today()


def fmt(d: dt.date) -> str:
    return f"{d.day} {d:%B %Y}"


def catalogue(v: int):
    t = TODAY()
    c = {
        "alpha": dict(name="DEMO Merit Scholarship for Undergraduate Students", amount="₹50,000 per annum", close=t + dt.timedelta(60)),
        "beta": dict(name="DEMO Rural Talent Scholarship", amount="₹30,000 per annum", close=t + dt.timedelta(45 if v == 1 else 75)),
        "gamma": dict(name="DEMO Women in Engineering Scholarship", amount="₹40,000 per annum", close=t + dt.timedelta(50)),
        "epsilon": dict(name="DEMO Legacy Merit Scholarship 2025", amount="₹25,000 per annum", close=t - dt.timedelta(20)),
        "zeta": dict(name="DEMO Science Excellence Scholarship", amount="₹35,000 per annum", close=t - dt.timedelta(5), closed=True),
        "eta": dict(name="DEMO Digital Skills Scholarship", amount="₹40,000 per annum" if v == 1 else "₹60,000 per annum", close=t + dt.timedelta(90)),
        "theta": dict(name="DEMO Final Year Project Scholarship", amount="₹20,000 per annum", close=t + dt.timedelta(7)),
    }
    if v == 2:
        del c["gamma"]                                       # removed from the official site
        c["delta"] = dict(name="DEMO Green Energy Research Scholarship", amount="₹75,000 per annum", close=t + dt.timedelta(120))
    return c


def page(slug, s):
    t = TODAY()
    closed = "<p>Applications are now closed for this academic year.</p>" if s.get("closed") else ""
    return f"""<html><head><title>{s['name']} | Demo Education Trust (TEST FIXTURE)</title></head><body>
<h1>{s['name']}</h1>
<p>This is a fictional test-fixture scholarship offered by Demo Education Trust. It is not a real scholarship.</p>
<p>The scholarship supports meritorious students with a yearly financial assistance award.</p>
{closed}
<table>
<tr><th>Scholarship amount</th><td>{s['amount']}</td></tr>
<tr><th>Applications open</th><td>{fmt(t - dt.timedelta(30))}</td></tr>
<tr><th>Last date to apply</th><td>{fmt(s['close'])}</td></tr>
</table>
<h2>Eligibility</h2>
<ul>
<li>Candidates must be Indian citizens pursuing an undergraduate degree (B.Tech or B.Sc) at a recognised university.</li>
<li>Annual family income should not exceed ₹6,00,000.</li>
<li>Candidates must have secured at least 75% marks in Class 12.</li>
<li>Age should not exceed 25 years.</li>
</ul>
<h2>Documents Required</h2>
<ul><li>Aadhaar card</li><li>Class 12 marksheet</li><li>Income certificate</li><li>Bank passbook copy</li></ul>
<p>Selection will be based on merit and a shortlist interview.</p>
<p>The scholarship is renewable each year subject to securing 70% marks in the previous year.</p>
<p><a href="/apply/{slug}">Apply Now</a></p>
</body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    do_HEAD = lambda self: self.do_GET()

    def do_GET(self):
        v, cat, p = STATE["version"], catalogue(STATE["version"]), self.path.split("?")[0]
        if p == "/":
            links = "".join(f'<li><a href="/sch/{k}">{s["name"]}</a></li>' for k, s in cat.items() if k != "delta")
            return self._send(200, f"<html><head><title>Demo Education Trust - Scholarships</title></head><body><h1>Our scholarships</h1><ul>{links}</ul></body></html>")
        if p == "/aggregator":
            extra = ""
            if v == 2:   # the aggregator knows about a scholarship the official listing page does not link to
                extra = (f'<li><a href="http://127.0.0.1:{PORT[0]}/sch/delta">DEMO Green Energy Research Scholarship - apply at official website</a>'
                         ' (aggregator claims last date 1 January 2030 - ignored, never stored)</li>')
            return self._send(200, f"<html><head><title>Top scholarships (aggregator)</title></head><body><h1>Scholarships roundup</h1><ul>{extra}</ul></body></html>")
        if p.startswith("/sch/"):
            slug = p[5:]
            return self._send(200, page(slug, cat[slug])) if slug in cat else self._send(404, "<h1>Not found</h1>")
        if p.startswith("/apply/"):
            return self._send(200, "<html><body><h1>Application portal</h1></body></html>")
        return self._send(404, "<h1>Not found</h1>")


def start(port=8765, version=1):
    STATE["version"] = version
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    PORT[0] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    import sys
    start(version=int(sys.argv[1]) if len(sys.argv) > 1 else 1)
    print("mock site on http://127.0.0.1:8765  (Ctrl-C to stop)")
    threading.Event().wait()
