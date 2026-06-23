from __future__ import annotations
import logging, time
from typing import Callable
from .config import AppConfig
from .strings import t
from .events import Event
from .link_source import LinkFileSource
from .provider import Provider

EventSink = Callable[[Event], None]

class DownloadRunner:
    def __init__(self, config: AppConfig, providers: list[Provider], emit: EventSink | None = None):
        self.config = config
        self.providers = {p.name: p for p in providers}
        self.emit = emit or (lambda event: None)
        self.log = logging.getLogger("bbd.runner")

    def run_provider(self, provider_name: str) -> None:
        provider = self.providers[provider_name]
        source = LinkFileSource(provider.links_file)
        done = 0
        total_bytes = 0
        self.emit(Event("status", provider=provider.name, message=t("run.links_file", path=provider.links_file)))
        new_links = source.poll_new()
        if not new_links:
            self.emit(Event("status", provider=provider.name, message=t("run.no_new")))
            return
        self.emit(Event("status", provider=provider.name, message=t("run.new_count", n=len(new_links))))
        for url in new_links:
            try:
                result = provider.resolve(url)
            except Exception as e:
                self.log.exception("Provider %s crashed while resolving %s", provider.name, url)
                self.emit(Event("error", provider=provider.name, message=t("run.resolve_crash", error=e), level="error"))
                continue
            if not result.ok:
                self.emit(Event("error", provider=provider.name, message=result.error or t("run.resolve_failed", url=url), level="error"))
                continue
            for dr in provider.download_result(result, self.emit):
                if dr.ok:
                    done += 1
                    total_bytes += dr.bytes_written
                    suffix = t("run.already_had") if dr.skipped else ""
                    self.emit(Event("status", provider=provider.name, message=t("run.ok", filename=dr.filename, suffix=suffix)))
                else:
                    self.emit(Event("error", provider=provider.name, message=t("run.fail", filename=dr.filename, error=dr.error), level="error"))
                self.emit(Event("overall", provider=provider.name, done=done, total_items=done, downloaded=total_bytes))
            time.sleep(self.config.request_delay)
