from __future__ import annotations
import re
from pathlib import Path
from urllib.parse import urlparse, unquote

INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON","PRN","AUX","NUL",*(f"COM{i}" for i in range(1,10)),*(f"LPT{i}" for i in range(1,10))}

def safe_filename(name: str, fallback: str = "file.bin") -> str:
    name = unquote(name).strip().replace("\n", " ").replace("\r", " ")
    name = INVALID.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = fallback
    stem = Path(name).stem.upper()
    if stem in RESERVED:
        name = "_" + name
    return name[:220]

def ext_from_url(url: str, default: str = ".bin") -> str:
    ext = Path(urlparse(url).path).suffix
    return ext if ext else default
