"""Source classification: is this host an official/primary source, and of what kind?"""
from dataclasses import dataclass
from .util import host_of, registrable_domain

GOVERNMENT = "GOVERNMENT"
UNIVERSITY = "UNIVERSITY"
CSR = "CORPORATE_CSR"
FOUNDATION = "FOUNDATION_TRUST"
INTERNATIONAL = "INTERNATIONAL"
AGGREGATOR = "AGGREGATOR"
UNKNOWN = "UNKNOWN"
FIXTURE = "TEST_FIXTURE"


@dataclass
class SourceInfo:
    source_type: str
    strength: float      # 1.0 = curated/structural official source, 0 = not a primary source
    reason: str
    is_fixture: bool = False

    @property
    def official(self) -> bool:
        return self.strength >= 0.9


def _match(host: str, entries) -> bool:
    for e in entries or []:
        e = e.lower()
        if e.startswith("."):
            if host.endswith(e) or host == e[1:]:
                return True
        elif host == e or host.endswith("." + e):
            return True
    return False


def classify(url: str, cfg: dict) -> SourceInfo:
    """Classification is by hostname *suffix* (never substring), so 'gov.in.evil.com' is not government."""
    s = cfg.get("sources", {})
    host = host_of(url)
    if not host:
        return SourceInfo(UNKNOWN, 0.0, "no host")
    if _match(host, s.get("fixture_hosts")):
        return SourceInfo(FIXTURE, 1.0, "local test-fixture host", True)
    if _match(host, s.get("aggregator_hosts")):
        return SourceInfo(AGGREGATOR, 0.0, f"{registrable_domain(host)} is a known aggregator/news/social site")
    if _match(host, s.get("government_hosts")) or _match(host, s.get("government_suffixes")):
        return SourceInfo(GOVERNMENT, 1.0, f"{host} matches government domain rules")
    if _match(host, s.get("international_hosts")) or _match(host, s.get("international_suffixes")):
        return SourceInfo(INTERNATIONAL, 1.0, f"{host} is a curated international/foreign-government provider")
    if _match(host, s.get("university_suffixes")):
        return SourceInfo(UNIVERSITY, 1.0, f"{host} matches academic domain rules")
    if _match(host, s.get("csr_hosts")):
        return SourceInfo(CSR, 1.0, f"{host} is a curated corporate/CSR provider")
    if _match(host, s.get("foundation_hosts")):
        return SourceInfo(FOUNDATION, 1.0, f"{host} is a curated foundation/trust provider")
    if any(t in host for t in ("scholarship", "scholar", "buddy", "apply")):
        return SourceInfo(AGGREGATOR, 0.0, "hostname suggests a third-party scholarship listing site")
    if any(t in host for t in ("foundation", "trust", "csr")):
        return SourceInfo(FOUNDATION, 0.6, "foundation-like hostname not in curated list (needs human review)")
    return SourceInfo(UNKNOWN, 0.3, "host not recognised as official")
