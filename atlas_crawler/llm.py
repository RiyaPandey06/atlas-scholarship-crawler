"""Optional free/local LLM extractor (Ollama). It is a *proposer only*: whatever it returns is passed through
the same grounding gate as the rule extractor (verbatim quote required, value must follow from the quote)."""
import json
import logging
import re

import requests

from .extract import Ev, find_dates, Page

log = logging.getLogger("atlas.llm")

LLM_FIELDS = ["name", "provider", "amount", "closing_date", "opening_date", "income_limit", "age_criteria",
              "education_level", "eligibility_text", "gender_criteria", "category_criteria", "domicile",
              "academic_requirements", "documents_required", "selection_process", "renewal_requirements"]

PROMPT = """You extract scholarship facts from a web page. Rules:
- Return ONLY JSON: {{"<field>": {{"value": "...", "quote": "..."}}, ...}}
- "quote" MUST be copied character-for-character from the page text below. If you cannot quote it, omit the field.
- Never infer or guess. If the page does not state a field, omit it.
- Dates: value must be YYYY-MM-DD and the quote must contain the date as written on the page.
Fields: {fields}

PAGE TEXT:
{text}
"""


class OllamaExtractor:
    def __init__(self, cfg: dict):
        c = cfg.get("llm", {})
        self.enabled = bool(c.get("enabled", False))
        self.url = c.get("url", "http://localhost:11434")
        self.model = c.get("model", "llama3.1:8b")
        self.max_chars = int(c.get("max_chars", 12000))
        self._ok = None

    def available(self) -> bool:
        if not self.enabled:
            return False
        if self._ok is None:
            try:
                self._ok = requests.get(self.url + "/api/tags", timeout=3).status_code == 200
            except Exception:
                self._ok = False
            if not self._ok:
                log.warning("Ollama not reachable at %s; continuing without LLM", self.url)
        return self._ok

    def extract(self, page: Page) -> dict:
        if not self.available():
            return {}
        try:
            r = requests.post(self.url + "/api/generate", timeout=180, json={
                "model": self.model, "stream": False, "format": "json", "options": {"temperature": 0},
                "prompt": PROMPT.format(fields=", ".join(LLM_FIELDS), text=page.full[: self.max_chars])})
            return parse_llm_json(r.json().get("response", "{}"))
        except Exception as e:
            log.warning("LLM extraction failed: %s", e)
            return {}


def parse_llm_json(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            data = json.loads(m.group(0)) if m else {}
        except Exception:
            return {}
    out = {}
    for f in LLM_FIELDS:
        v = data.get(f)
        if not isinstance(v, dict) or not v.get("value") or not v.get("quote"):
            continue
        val, quote = str(v["value"]).strip(), str(v["quote"]).strip()
        if f in ("closing_date", "opening_date"):
            ds = find_dates(quote)
            if not ds or val not in {d[0] for d in ds}:
                continue
        out[f] = Ev(val, quote, "llm")
    return out
