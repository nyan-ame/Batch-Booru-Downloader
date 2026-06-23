from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable
import logging
from .config import AppConfig
from .strings import t
from .events import Event
from .http import HttpClient, EventSink
from .models import ProviderResult, DownloadResult
from dataclasses import dataclass

@dataclass
class AuthStatus:
    state: str  # Alive / Dead / N/A
    message: str = ""
    can_refresh: bool = False

class Provider(ABC):
    name = "base"
    links_filename = "base.txt"
    domains: tuple[str, ...] = ()

    def __init__(self, config: AppConfig):
        self.config = config
        self.log = logging.getLogger(f"bbd.provider.{self.name}")
        self.http = HttpClient(config, f"bbd.http.{self.name}")

    @abstractmethod
    def supports(self, url: str) -> bool: ...

    @abstractmethod
    def resolve(self, url: str) -> ProviderResult: ...

    def auth_status(self) -> AuthStatus:
        return AuthStatus("N/A", t("prov.auth_not_required"), False)

    def refresh_auth_interactive(self) -> AuthStatus:
        return self.auth_status()

    def download_result(self, result: ProviderResult, emit: EventSink | None = None) -> list[DownloadResult]:
        out = self.config.download_dir
        results = []
        for target in result.targets:
            if emit:
                emit(Event("status", provider=self.name, message=t("prov.downloading", filename=target.filename)))
            results.append(self.http.download(target, out, emit))
        return results

    @property
    def links_file(self) -> Path:
        return self.config.links_dir / self.links_filename
