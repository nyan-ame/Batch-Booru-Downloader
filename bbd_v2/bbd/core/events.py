from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

@dataclass
class Event:
    type: Literal["status", "file_progress", "overall", "error"]
    message: str = ""
    level: str = "info"
    provider: str = ""
    filename: str = ""
    downloaded: int = 0
    total: int = 0
    speed_bps: float = 0.0
    done: int = 0
    total_items: int = 0
