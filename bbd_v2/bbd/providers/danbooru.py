from __future__ import annotations
import re
import time
from urllib.parse import urlparse
from bbd.core.provider import Provider, AuthStatus
from bbd.core.models import ProviderResult, DownloadTarget
from bbd.core.filenames import ext_from_url
from bbd.core.strings import t

class DanbooruProvider(Provider):
    name = "Danbooru"
    links_filename = "danbooru.txt"

    def __init__(self, config):
        super().__init__(config)
        self._maybe_enable_curl_cffi()
        ua = config.get("DANBOORU_USER_AGENT") or config.user_agent
        self.http.session.headers.update({
            "User-Agent": ua,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
            "Referer": "https://danbooru.donmai.us/",
            "Origin": "https://danbooru.donmai.us",
            "X-Requested-With": "XMLHttpRequest",
        })
        try:
            self.cookie_ttl_minutes = float(config.get("DANBOORU_COOKIE_TTL_MINUTES", "30") or 30)
        except ValueError:
            self.cookie_ttl_minutes = 30.0
        self.runtime_cookie = self._normalize_cookie_header(config.get("DANBOORU_COOKIE"))
        self.cookie_set_at = time.time() if self.runtime_cookie else None
        self.http.set_cookie_header(self.runtime_cookie)

    def _maybe_enable_curl_cffi(self) -> None:
        if not self.config.bool("DANBOORU_USE_CURL_CFFI", True):
            return
        try:
            from curl_cffi import requests as curl_requests
        except Exception as e:
            self.log.debug("DANBOORU_USE_CURL_CFFI=true, but curl_cffi is unavailable: %s", e)
            return
        impersonate = self.config.get("DANBOORU_IMPERSONATE", "chrome") or "chrome"
        self.http.session = curl_requests.Session(impersonate=impersonate)


    @staticmethod
    def _normalize_cookie_header(raw: str) -> str:
        """Accept either a normal Cookie header or a copied Set-Cookie-ish value.

        Correct format is: name=value; name2=value2. If the user pasted attributes
        like domain/path/expires/secure/httponly/samesite, strip them.
        """
        if not raw:
            return ""
        ignored = {"domain", "path", "expires", "max-age", "secure", "httponly", "samesite", "priority"}
        parts = []
        for item in raw.replace("\n", ";").split(";"):
            item = item.strip()
            if not item:
                continue
            key = item.split("=", 1)[0].strip().lower()
            if key in ignored:
                continue
            # Bare cookie attributes without '=' should be ignored too.
            if "=" not in item:
                continue
            parts.append(item)
        return "; ".join(parts)


    def set_runtime_cookie(self, cookie: str) -> None:
        self.runtime_cookie = self._normalize_cookie_header(cookie)
        self.cookie_set_at = time.time() if self.runtime_cookie else None
        self.http.set_cookie_header(self.runtime_cookie)

    def cookie_info(self) -> tuple[float, float, float] | None:
        """Return (age_seconds, remaining_seconds, ttl_minutes) or None.

        cf_clearance does not expose a reliable expiry in its value, so we track
        when the cookie was provided and assume a soft TTL
        (DANBOORU_COOKIE_TTL_MINUTES, default 30). This is an estimate, not a
        guarantee.
        """
        if not getattr(self, "runtime_cookie", "") or not getattr(self, "cookie_set_at", None):
            return None
        age = time.time() - self.cookie_set_at
        remaining = self.cookie_ttl_minutes * 60 - age
        return (age, remaining, self.cookie_ttl_minutes)

    def auth_status(self) -> AuthStatus:
        if not self.config.get("DANBOORU_USERNAME") or not self.config.get("DANBOORU_API_KEY"):
            return AuthStatus("Dead", t("danbooru.auth_no_creds"), True)
        if not getattr(self, "runtime_cookie", ""):
            return AuthStatus("Dead", t("danbooru.auth_no_cookie"), True)
        try:
            self.http.get_json("https://danbooru.donmai.us/posts/1.json", params={
                "login": self.config.get("DANBOORU_USERNAME"),
                "api_key": self.config.get("DANBOORU_API_KEY"),
            }, timeout=15)
            return AuthStatus("Alive", t("danbooru.auth_ok"), True)
        except Exception as e:
            return AuthStatus("Dead", t("danbooru.auth_failed", error=e), True)

    def refresh_auth_interactive(self) -> AuthStatus:
        import webbrowser
        print(t("danbooru.refresh_title"))
        print(t("danbooru.refresh_1"))
        print(t("danbooru.refresh_2"))
        print(t("danbooru.refresh_3"))
        print(t("danbooru.refresh_4"))
        try:
            webbrowser.open("https://danbooru.donmai.us/posts")
        except Exception:
            pass
        cookie = input(t("danbooru.cookie_prompt")).strip()
        if not cookie:
            return AuthStatus("Dead", t("danbooru.cookie_missing"), True)
        self.set_runtime_cookie(cookie)
        return self.auth_status()

    def supports(self, url: str) -> bool:
        return "danbooru.donmai.us" in url

    def _post_id(self, url: str) -> str | None:
        m = re.search(r"/posts/(\d+)", urlparse(url).path)
        return m.group(1) if m else None

    def resolve(self, url: str) -> ProviderResult:
        post_id = self._post_id(url)
        if not post_id:
            return ProviderResult(url, False, error=t("danbooru.bad_url"))
        api = "https:/" + "/" + f"danbooru.donmai.us/posts/{post_id}.json"
        params = {}
        if self.config.get("DANBOORU_USERNAME"):
            params["login"] = self.config.get("DANBOORU_USERNAME")
        if self.config.get("DANBOORU_API_KEY"):
            params["api_key"] = self.config.get("DANBOORU_API_KEY")
        try:
            post = self.http.get_json(api, params=params)
        except Exception as e:
            return ProviderResult(url, False, error=t("danbooru.api_error", error=e))
        file_url = None
        asset = post.get("media_asset") if isinstance(post, dict) else None
        if asset and asset.get("variants"):
            variants = asset["variants"]
            for preferred in ("original", "sample", "thumbnail"):
                for v in variants:
                    if v.get("type") == preferred and v.get("url"):
                        file_url = v["url"]; break
                if file_url: break
        file_url = file_url or post.get("file_url") or post.get("large_file_url") or post.get("preview_file_url")
        if not file_url:
            return ProviderResult(url, False, error=t("danbooru.no_media", post_id=post_id))
        ext = ext_from_url(file_url, "." + post.get("file_ext", "bin"))
        return ProviderResult(url, True, [DownloadTarget(file_url, f"danbooru__{post_id}{ext}", referer="https://danbooru.donmai.us/")])
