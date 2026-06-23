from __future__ import annotations
import logging, time
from pathlib import Path
from typing import Callable, Optional
import requests
from .config import AppConfig
from .events import Event
from .models import DownloadTarget, DownloadResult
from .filenames import safe_filename

EventSink = Callable[[Event], None]

class HttpClient:
    def __init__(self, config: AppConfig, logger_name: str = "bbd.http"):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.user_agent})
        self.log = logging.getLogger(logger_name)

    def set_cookie_header(self, cookie: str) -> None:
        if cookie:
            self.session.headers.update({"Cookie": cookie})

    def get_json(self, url: str, *, params=None, headers=None, timeout=30):
        last = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=timeout)
                if r.status_code == 429:
                    delay = self._retry_delay(r, attempt)
                    self.log.warning("429 for %s, sleeping %.1fs", url, delay)
                    time.sleep(delay)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last = e
                self.log.warning("GET JSON failed (%s/%s): %s", attempt, self.config.max_retries, e)
                if attempt < self.config.max_retries:
                    time.sleep(self.config.request_delay * attempt)
        raise last  # type: ignore[misc]

    def get_text(self, url: str, *, params=None, headers=None, timeout=30) -> str:
        last = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=timeout)
                if r.status_code == 429:
                    delay = self._retry_delay(r, attempt)
                    self.log.warning("429 for %s, sleeping %.1fs", url, delay)
                    time.sleep(delay)
                    continue
                r.raise_for_status()
                return r.text
            except Exception as e:
                last = e
                self.log.warning("GET text failed (%s/%s): %s", attempt, self.config.max_retries, e)
                if attempt < self.config.max_retries:
                    time.sleep(self.config.request_delay * attempt)
        raise last  # type: ignore[misc]

    def download(self, target: DownloadTarget, out_dir: Path, emit: EventSink | None = None) -> DownloadResult:
        filename = safe_filename(target.filename)
        filepath = out_dir / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        if filepath.exists() and filepath.stat().st_size > 0:
            return DownloadResult(True, filename, filepath.stat().st_size, skipped=True)
        tmp = filepath.with_suffix(filepath.suffix + ".part")
        headers = dict(target.headers)
        if target.referer:
            headers.setdefault("Referer", target.referer)
        last_error = None
        for attempt in range(1, self.config.max_retries + 1):
            downloaded = 0
            start = time.time()
            try:
                r = self.session.get(target.url, headers=headers or None, stream=True, timeout=60)
                try:
                    if r.status_code == 429:
                        delay = self._retry_delay(r, attempt)
                        time.sleep(delay)
                        continue
                    r.raise_for_status()
                    ctype = r.headers.get("Content-Type", "")
                    total = int(r.headers.get("Content-Length") or 0)
                    if "text/html" in ctype.lower() and total < 2_000_000:
                        snippet = r.text[:200].replace("\n", " ")
                        raise RuntimeError(f"server returned HTML instead of media: {snippet}")
                    with tmp.open("wb") as f:
                        last_emit = 0.0
                        for chunk in r.iter_content(chunk_size=self.config.chunk_size):
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded += len(chunk)
                            now = time.time()
                            if emit and (now - last_emit >= 0.12 or (total and downloaded >= total)):
                                speed = downloaded / max(now - start, 0.001)
                                emit(Event("file_progress", provider="", filename=filename, downloaded=downloaded, total=total, speed_bps=speed))
                                last_emit = now
                finally:
                    close = getattr(r, "close", None)
                    if callable(close):
                        close()
                tmp.replace(filepath)
                return DownloadResult(True, filename, downloaded)
            except Exception as e:
                last_error = str(e)
                self.log.warning("Download failed %s (%s/%s): %s", target.url, attempt, self.config.max_retries, e)
                if tmp.exists():
                    try: tmp.unlink()
                    except OSError: pass
                if attempt < self.config.max_retries:
                    time.sleep(self.config.request_delay * attempt)
        return DownloadResult(False, filename, 0, error=last_error)

    @staticmethod
    def _retry_delay(response: requests.Response, attempt: int) -> float:
        val = response.headers.get("Retry-After")
        if val:
            try: return float(val)
            except ValueError: pass
        return min(60.0, 2.0 ** attempt)
