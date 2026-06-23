from __future__ import annotations
from dataclasses import dataclass, field
from typing import Iterable
from .provider import Provider


@dataclass
class RoutingResult:
    """Result of routing arbitrary URLs to providers.

    by_provider maps provider name -> list of URLs that the provider claims via supports().
    unknown holds URLs that no provider recognized.
    """
    by_provider: dict[str, list[str]] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)

    def total(self) -> int:
        return sum(len(v) for v in self.by_provider.values()) + len(self.unknown)

    def summary(self) -> str:
        parts = [f"{name}: {len(urls)}" for name, urls in self.by_provider.items() if urls]
        if self.unknown:
            parts.append(f"unknown: {len(self.unknown)}")
        return ", ".join(parts) if parts else "nothing"


def route_url(providers: Iterable[Provider], url: str) -> Provider | None:
    """Return the first provider that supports the URL, or None."""
    url = (url or "").strip()
    if not url:
        return None
    for provider in providers:
        try:
            if provider.supports(url):
                return provider
        except Exception:
            # A broken supports() must never break routing of other URLs.
            continue
    return None


def route_many(providers: Iterable[Provider], urls: Iterable[str]) -> RoutingResult:
    """Route a batch of URLs to providers using supports().

    Order of providers matters: the first match wins. Blank lines and lines
    starting with '#' are ignored. Duplicate URLs are kept (the link files
    themselves are the source of truth for dedup).
    """
    provider_list = list(providers)
    result = RoutingResult()
    for raw in urls:
        url = (raw or "").strip()
        if not url or url.startswith("#"):
            continue
        provider = route_url(provider_list, url)
        if provider is None:
            result.unknown.append(url)
        else:
            result.by_provider.setdefault(provider.name, []).append(url)
    return result


def append_links_to_files(providers: Iterable[Provider], result: RoutingResult) -> dict[str, int]:
    """Append routed URLs into each provider's links file. Returns counts written.

    Skips URLs already present in the target file so re-routing the same paste
    does not create duplicates on disk.
    """
    by_name = {p.name: p for p in providers}
    written: dict[str, int] = {}
    for name, urls in result.by_provider.items():
        provider = by_name.get(name)
        if provider is None or not urls:
            continue
        path = provider.links_file
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    existing.add(s)
        new = [u for u in urls if u not in existing]
        if new:
            with path.open("a", encoding="utf-8") as out:
                for u in new:
                    out.write(u + "\n")
        written[name] = len(new)
    return written
