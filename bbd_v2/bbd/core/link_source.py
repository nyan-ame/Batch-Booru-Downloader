from __future__ import annotations
from pathlib import Path
import time

class LinkFileSource:
    def __init__(self, path: Path):
        self.path = path
        self.seen: set[str] = set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def poll_new(self) -> list[str]:
        links: list[str] = []
        for raw in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line not in self.seen:
                self.seen.add(line)
                links.append(line)
        return links
