from __future__ import annotations
import asyncio, inspect, json, re, shutil, subprocess, sys, time, zipfile
from pathlib import Path
from urllib.parse import urlparse
from bbd.core.provider import Provider, AuthStatus
from bbd.core.models import ProviderResult, DownloadTarget, DownloadResult
from bbd.core.events import Event
from bbd.core.strings import t

class PixivProvider(Provider):
    name = "pixiv"
    links_filename = "pixiv.txt"

    def __init__(self, config):
        super().__init__(config)
        self.token_file = config.tokens_dir / "pixiv_token.json"
        self.ugoira_mode = config.get("PIXIV_UGOIRA_CONVERSION_MODE", "gif_only")
        self.api = None

    def supports(self, url: str) -> bool:
        return "pixiv.net" in url and "artworks/" in url

    def _illust_id(self, url: str) -> str | None:
        m = re.search(r"artworks/(\d+)", url)
        return m.group(1) if m else None

    def _make_api(self):
        try:
            from pixivpy3 import AppPixivAPI
        except ImportError as e:
            raise RuntimeError(t("pixiv.no_pixivpy")) from e
        return AppPixivAPI()

    def _read_token(self) -> str | None:
        token = self.config.get("PIXIV_REFRESH_TOKEN") or None
        if token and token.lower() != "template":
            return token.strip()
        if self.token_file.exists():
            try:
                token = json.loads(self.token_file.read_text(encoding="utf-8")).get("refresh_token")
                return token.strip() if token else None
            except Exception as e:
                self.log.warning("Cannot read Pixiv token file: %s", e)
        return None

    def set_refresh_token(self, token: str) -> None:
        token = (token or "").strip()
        if not token:
            raise RuntimeError(t("pixiv.empty_token"))
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps({"refresh_token": token}, indent=2), encoding="utf-8")
        self.api = None

    def clear_token(self) -> None:
        self.api = None
        if self.token_file.exists():
            self.token_file.unlink()

    def _auth_with_token(self, token: str):
        api = self._make_api()
        api.auth(refresh_token=token)
        # pixivpy may rotate/refresh token; preserve newest one if available.
        if getattr(api, "refresh_token", None):
            self.set_refresh_token(api.refresh_token)
        self.api = api
        return api

    def _probe_api(self, api) -> None:
        # app-api has no ideal 'me' endpoint in pixivpy3; illust_recommended is a lightweight authenticated call.
        try:
            api.illust_recommended(req_auth=True)
        except TypeError:
            api.illust_recommended()

    def auth_status(self) -> AuthStatus:
        token = self._read_token()
        if not token:
            return AuthStatus("Dead", t("pixiv.auth_no_token"), True)
        try:
            api = self._auth_with_token(token)
            self._probe_api(api)
            return AuthStatus("Alive", t("pixiv.auth_ok"), True)
        except Exception as e:
            self.api = None
            return AuthStatus("Dead", t("pixiv.auth_failed", error=e), True)

    def test_auth(self) -> str:
        status = self.auth_status()
        if status.state != "Alive":
            raise RuntimeError(status.message)
        return "Pixiv auth OK"

    @staticmethod
    def _extract_gppt_response(res):
        return getattr(res, "response", res)

    def login_via_gppt(self, *, headless: bool = False, username: str | None = None, password: str | None = None) -> str:
        try:
            from gppt import GetPixivToken
        except ImportError as e:
            raise RuntimeError(t("pixiv.no_gppt")) from e
        g = GetPixivToken(headless=headless, username=username, password=password)
        res = g.login(headless=headless, username=username, password=password)
        # gppt 4.x can return a coroutine from the library API. If we don't await it,
        # the browser flow never starts and refresh_token is never produced.
        if inspect.isawaitable(res):
            try:
                res = asyncio.run(res)
            except RuntimeError:
                # Fallback for unusual environments with an already running event loop.
                loop = asyncio.new_event_loop()
                try:
                    res = loop.run_until_complete(res)
                finally:
                    loop.close()
        payload = self._extract_gppt_response(res)
        refresh_token = payload.get("refresh_token") if isinstance(payload, dict) else None
        if not refresh_token:
            raise RuntimeError(t("pixiv.gppt_no_token"))
        self.set_refresh_token(refresh_token)
        self.test_auth()
        return "Pixiv gppt login OK; token saved"

    def refresh_auth_interactive(self) -> AuthStatus:
        print(t("pixiv.refresh_title"))
        print(t("pixiv.refresh_intro"))
        try:
            self.login_via_gppt(headless=False)
        except Exception as e:
            return AuthStatus("Dead", t("pixiv.gppt_login_failed", error=e), True)
        return self.auth_status()

    def _login(self):
        if self.api:
            return self.api
        token = self._read_token()
        if token:
            return self._auth_with_token(token)
        raise RuntimeError(t("pixiv.token_missing_refresh"))

    def resolve(self, url: str) -> ProviderResult:
        illust_id = self._illust_id(url)
        if not illust_id:
            return ProviderResult(url, False, error=t("pixiv.bad_url"))
        try:
            api = self._login()
            data = api.illust_detail(illust_id)
            if data.get("error") or not data.get("illust"):
                return ProviderResult(url, False, error=t("pixiv.not_found", illust_id=illust_id))
            illust = data.illust
            targets: list[DownloadTarget] = []
            if illust.type == "ugoira":
                meta = api.ugoira_metadata(illust_id).ugoira_metadata
                zip_url = meta.zip_urls.medium
                targets.append(DownloadTarget(zip_url, f"ugoira/zip/pixiv__{illust_id}.zip", kind="pixiv_ugoira", meta={"frames": [dict(f) for f in meta.frames], "gif": f"ugoira/gif/pixiv__{illust_id}.gif"}))
            elif illust.meta_pages:
                for i, page in enumerate(illust.meta_pages):
                    u = page.image_urls.original
                    ext = Path(urlparse(u).path).suffix or ".bin"
                    targets.append(DownloadTarget(u, f"pixiv__{illust_id}_p{i}{ext}", headers={"Referer": "https://app-api.pixiv.net/"}))
            elif illust.meta_single_page and illust.meta_single_page.original_image_url:
                u = illust.meta_single_page.original_image_url
                ext = Path(urlparse(u).path).suffix or ".bin"
                targets.append(DownloadTarget(u, f"pixiv__{illust_id}{ext}", headers={"Referer": "https://app-api.pixiv.net/"}))
            if not targets:
                return ProviderResult(url, False, error=t("pixiv.no_media", illust_id=illust_id))
            return ProviderResult(url, True, targets)
        except Exception as e:
            return ProviderResult(url, False, error=str(e))

    def download_result(self, result: ProviderResult, emit=None) -> list[DownloadResult]:
        api = self._login()
        results: list[DownloadResult] = []
        for target in result.targets:
            path = self.config.download_dir / target.filename
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 0:
                results.append(DownloadResult(True, target.filename, path.stat().st_size, skipped=True))
                continue
            try:
                start_time = time.time()
                with api.requests_call("GET", target.url, stream=True, headers={"Referer": "https://app-api.pixiv.net/"}) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("content-length") or 0)
                    done = 0
                    last_emit = 0.0
                    with path.open("wb") as f:
                        for chunk in r.iter_content(chunk_size=self.config.chunk_size):
                            if not chunk:
                                continue
                            f.write(chunk)
                            done += len(chunk)
                            now = time.time()
                            if emit and (now - last_emit >= 0.12 or (total and done >= total)):
                                speed_bps = done / max(now - start_time, 0.001)
                                emit(Event("file_progress", provider=self.name, filename=target.filename, downloaded=done, total=total, speed_bps=speed_bps))
                                last_emit = now
                if target.kind == "pixiv_ugoira" and self.ugoira_mode in {"gif_only", "both"}:
                    self._convert_ugoira(path, self.config.download_dir / target.meta["gif"], target.meta["frames"])
                    if self.ugoira_mode == "gif_only":
                        try: path.unlink()
                        except OSError: pass
                results.append(DownloadResult(True, target.filename, done))
            except Exception as e:
                results.append(DownloadResult(False, target.filename, error=str(e)))
        return results

    def _convert_ugoira(self, zip_path: Path, gif_path: Path, frames: list[dict]) -> None:
        from PIL import Image
        gif_path.parent.mkdir(parents=True, exist_ok=True)
        if gif_path.exists():
            return
        with zipfile.ZipFile(zip_path, "r") as archive:
            imgs = [Image.open(archive.open(f["file"])).convert("RGBA") for f in frames]
            delays = [int(f["delay"]) for f in frames]
        imgs[0].save(gif_path, save_all=True, append_images=imgs[1:], duration=delays, loop=0, optimize=False)
