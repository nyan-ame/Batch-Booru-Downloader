from __future__ import annotations
import re
from pathlib import Path
from urllib.parse import urlparse
from bbd.core.provider import Provider
from bbd.core.models import ProviderResult, DownloadTarget, DownloadResult
from bbd.core.events import Event
from bbd.core.strings import t

class TwitterProvider(Provider):
    name = "twitter"
    links_filename = "twitter.txt"

    def supports(self, url: str) -> bool:
        return any(x in url for x in ("twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com")) and "/status/" in url

    def _tweet_id(self, url: str) -> str | None:
        m = re.search(r"/status/(\d+)", url)
        return m.group(1) if m else None

    def resolve(self, url: str) -> ProviderResult:
        tweet_id = self._tweet_id(url)
        if not tweet_id:
            return ProviderResult(url, False, error=t("twitter.bad_url"))
        targets: list[DownloadTarget] = []
        if self.config.bool("TWITTER_USE_FXTWITTER", True):
            try:
                data = self.http.get_json("https:/" + "/" + f"api.fxtwitter.com/status/{tweet_id}", timeout=20)
                tweet = data.get("tweet", {}) if isinstance(data, dict) else {}
                media = tweet.get("media", {}) if isinstance(tweet, dict) else {}
                for i, p in enumerate(media.get("photos") or []):
                    original = (p.get("url") or "").split("?")[0]
                    if not original: continue
                    ext = Path(urlparse(original).path).suffix.lstrip(".") or "jpg"
                    targets.append(DownloadTarget(f"{original}?format={ext}&name=orig", f"twitter__{tweet_id}_p{i}.{ext}", referer="https://x.com/"))
                # Videos are intentionally handled by yt-dlp from original tweet URL in download_result.
                for i, _v in enumerate(media.get("videos") or []):
                    targets.append(DownloadTarget(url, f"twitter__{tweet_id}_v{i}.mp4", kind="yt_dlp"))
            except Exception as e:
                self.log.warning("FxTwitter failed for %s: %s", tweet_id, e)
        if not targets:
            # Fallback: let yt-dlp try the whole post. It may require cookies in user environment.
            targets.append(DownloadTarget(url, f"twitter__{tweet_id}.%(ext)s", kind="yt_dlp"))
        return ProviderResult(url, True, targets)

    def download_result(self, result: ProviderResult, emit=None) -> list[DownloadResult]:
        normal = ProviderResult(result.source_url, True, [t for t in result.targets if t.kind != "yt_dlp"])
        results = []
        if normal.targets:
            results.extend(super().download_result(normal, emit))
        for target in [t for t in result.targets if t.kind == "yt_dlp"]:
            results.append(self._download_ytdlp(target, emit))
        return results

    def _download_ytdlp(self, target: DownloadTarget, emit=None) -> DownloadResult:
        try:
            import yt_dlp
        except ImportError:
            return DownloadResult(False, target.filename, error=t("twitter.no_ytdlp"))
        outtmpl = str(self.config.download_dir / target.filename)
        if emit:
            emit(Event("status", provider=self.name, message=t("twitter.ytdlp_status")))

        def progress_hook(d: dict) -> None:
            if not emit or d.get("status") != "downloading":
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            speed = d.get("speed") or 0.0
            name = Path(d.get("filename") or target.filename).name
            emit(Event("file_progress", provider=self.name, filename=name, downloaded=int(done), total=int(total), speed_bps=float(speed)))

        try:
            opts = {"format": "best", "outtmpl": outtmpl, "quiet": True, "no_warnings": True, "noprogress": True, "progress_hooks": [progress_hook]}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target.url, download=True)
            # filename can be different when %(ext)s is used
            filename = Path(ydl.prepare_filename(info)).name if info else target.filename
            path = self.config.download_dir / filename
            return DownloadResult(True, filename, path.stat().st_size if path.exists() else 0)
        except Exception as e:
            return DownloadResult(False, target.filename, error=str(e))
