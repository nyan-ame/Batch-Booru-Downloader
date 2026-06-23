from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

@dataclass
class DownloadTarget:
    url: str
    filename: str
    headers: dict[str, str] = field(default_factory=dict)
    referer: Optional[str] = None
    kind: str = "file"
    meta: dict[str, Any] = field(default_factory=dict)

@dataclass
class DownloadResult:
    ok: bool
    filename: str
    bytes_written: int = 0
    skipped: bool = False
    error: str | None = None

@dataclass
class ProviderResult:
    source_url: str
    ok: bool
    targets: list[DownloadTarget] = field(default_factory=list)
    error: str | None = None
