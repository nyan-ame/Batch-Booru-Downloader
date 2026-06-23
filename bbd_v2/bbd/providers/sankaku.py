from __future__ import annotations
import json, re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bbd.core.provider import Provider, AuthStatus
from bbd.core.models import ProviderResult, DownloadTarget
from bbd.core.filenames import ext_from_url
from bbd.core.strings import t

class SankakuProvider(Provider):
    name = "sankaku"
    links_filename = "sankaku.txt"
    api_base = "https://sankakuapi.com/"
    mobile_user_agent = "SCChannelApp/4.12 (RNAndroid; black)"

    def __init__(self, config):
        super().__init__(config)
        self.token_file = config.tokens_dir / "sankaku_token.json"
        self.auth_url = urljoin(self.api_base, "auth/token")
        self.posts_url = urljoin(self.api_base, "posts")
        self.http.session.headers.update({
            "User-Agent": self.mobile_user_agent,
            "Referer": "https://sankaku.app/",
            "Origin": "https://sankaku.app",
            "api-version": "2",
            "Content-Type": "application/json",
        })
        self.access_token = None


    def auth_status(self) -> AuthStatus:
        try:
            token = self._ensure_token()
            if token:
                self.access_token = token
                self.http.session.headers["Authorization"] = f"Bearer {token}"
                return AuthStatus("Alive", t("sankaku.auth_ok"), True)
            return AuthStatus("Dead", t("sankaku.auth_missing"), True)
        except Exception as e:
            return AuthStatus("Dead", t("sankaku.auth_failed", error=e), True)

    def refresh_auth_interactive(self) -> AuthStatus:
        # Force re-auth by removing cached token, then use settings.env credentials.
        try:
            if self.token_file.exists():
                self.token_file.unlink()
        except OSError:
            pass
        self.access_token = None
        return self.auth_status()

    def supports(self, url: str) -> bool:
        return "sankaku" in url

    def _ensure_token(self) -> str | None:
        login = self.config.get("SANKAKU_LOGIN")
        password = self.config.get("SANKAKU_PASSWORD")
        tokens = self._load_valid_tokens()
        if not tokens and login and password:
            tokens = self._authenticate(login, password)
        return tokens.get("access_token") if tokens else None

    def _load_valid_tokens(self) -> dict | None:
        if not self.token_file.exists():
            return None
        try:
            tokens = json.loads(self.token_file.read_text(encoding="utf-8"))
            now = datetime.now()
            if tokens.get("access_token_expires_at") and now < datetime.fromisoformat(tokens["access_token_expires_at"]):
                return tokens
            if tokens.get("refresh_token_expires_at") and now < datetime.fromisoformat(tokens["refresh_token_expires_at"]):
                return self._refresh(tokens.get("refresh_token"))
        except Exception as e:
            self.log.warning("Cannot load Sankaku token: %s", e)
        return None

    def _save_tokens(self, tokens: dict) -> None:
        now = datetime.now()
        if tokens.get("access_token_ttl"):
            tokens["access_token_expires_at"] = (now + timedelta(seconds=int(tokens["access_token_ttl"]))).isoformat()
        if tokens.get("refresh_token_ttl"):
            tokens["refresh_token_expires_at"] = (now + timedelta(seconds=int(tokens["refresh_token_ttl"]))).isoformat()
        self.token_file.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")

    def _authenticate(self, login: str, password: str) -> dict | None:
        try:
            r = self.http.session.post(self.auth_url, json={"login": login, "password": password}, params={"lang": "english"}, timeout=20)
            r.raise_for_status()
            data = r.json()
            if data.get("success"):
                self._save_tokens(data)
                return data
            self.log.error("Sankaku auth failed: %s", data.get("message"))
        except Exception as e:
            self.log.error("Sankaku auth request failed: %s", e)
        return None

    def _refresh(self, refresh_token: str | None) -> dict | None:
        if not refresh_token:
            return None
        try:
            headers = {"Authorization": f"Bearer {refresh_token}"}
            r = self.http.session.post(self.auth_url, json={"refresh_token": refresh_token}, headers=headers, params={"lang": "english"}, timeout=20)
            r.raise_for_status()
            data = r.json()
            if data.get("success"):
                self._save_tokens(data)
                return data
            self.log.error("Sankaku token refresh failed: %s", data.get("message"))
        except Exception as e:
            self.log.error("Sankaku token refresh request failed: %s", e)
        return None

    def _post_id(self, url: str) -> str | None:
        m = re.search(r"/posts/([a-zA-Z0-9]+)(?:\?|$)", urlparse(url).path + ("?" if urlparse(url).query else ""))
        return m.group(1) if m else None

    def _get_post(self, post_id: str) -> dict | None:
        direct_url = urljoin(self.posts_url + "/", post_id)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": self.mobile_user_agent,
            "Referer": "https://sankaku.app/",
            "Origin": "https://sankaku.app",
            "api-version": "2",
            "content-type": "application/json",
        }
        if self.access_token is None:
            self.access_token = self._ensure_token()
            if self.access_token:
                self.http.session.headers["Authorization"] = f"Bearer {self.access_token}"
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        try:
            return self.http.get_json(direct_url, headers=headers, timeout=30)
        except Exception as first:
            self.log.warning("Sankaku direct post endpoint failed for %s: %s", post_id, first)
            try:
                data = self.http.get_json(self.posts_url, params={"tags": f"id:{post_id}", "limit": 1}, headers=headers, timeout=30)
                if isinstance(data, list) and data:
                    return data[0]
            except Exception as second:
                self.log.error("Sankaku fallback endpoint failed for %s: %s", post_id, second)
        return None

    def resolve(self, url: str) -> ProviderResult:
        post_id = self._post_id(url)
        if not post_id:
            return ProviderResult(url, False, error=t("sankaku.bad_url"))
        post = self._get_post(post_id)
        if not post:
            return ProviderResult(url, False, error=t("sankaku.not_found", post_id=post_id))
        file_url = post.get("file_url") or post.get("sample_url") or post.get("preview_url")
        if not file_url:
            return ProviderResult(url, False, error=t("sankaku.no_media", post_id=post_id))
        ext = ext_from_url(file_url)
        return ProviderResult(url, True, [DownloadTarget(file_url, f"sankaku__{post_id}{ext}", referer="https://sankaku.app/")])
