"""Free web-search discovery. No API key: DuckDuckGo's HTML endpoint, or a self-hosted SearXNG instance."""
import logging
from urllib.parse import urlsplit, parse_qs, unquote

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("atlas.search")


def _unwrap(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    q = parse_qs(urlsplit(href).query)
    return unquote(q["uddg"][0]) if "uddg" in q else href


def search(query: str, cfg: dict, n: int = 10) -> list:
    ua = cfg.get("user_agent", "AtlasScholarshipBot/1.0")
    searx = cfg.get("discovery", {}).get("searxng_url")
    try:
        if searx:
            r = requests.get(searx.rstrip("/") + "/search", params={"q": query, "format": "json"}, headers={"User-Agent": ua}, timeout=20)
            return [x["url"] for x in r.json().get("results", [])][:n]
        r = requests.post("https://html.duckduckgo.com/html/", data={"q": query}, headers={"User-Agent": ua}, timeout=20)
        soup = BeautifulSoup(r.text, "lxml")
        urls = [_unwrap(a["href"]) for a in soup.select("a.result__a") if a.get("href")]
        return [u for u in urls if u.startswith("http")][:n]
    except Exception as e:
        log.warning("search failed for %r: %s", query, e)
        return []
