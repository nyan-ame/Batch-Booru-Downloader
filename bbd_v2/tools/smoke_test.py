from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bbd.core.config import AppConfig
from bbd.providers import make_providers

cfg = AppConfig.load()
providers = make_providers(cfg)
print("providers:", ", ".join(p.name for p in providers))
for p in providers:
    assert p.links_file.exists(), p.links_file
print("OK")
