from __future__ import annotations
import hashlib
import re
from urllib.parse import urlparse
from bbd.core.provider import Provider, AuthStatus
from bbd.core.models import ProviderResult, DownloadTarget
from bbd.core.filenames import ext_from_url
from bbd.core.strings import t

class GelbooruProvider(Provider):
    name = "Gelbooru"
    links_filename = "gelbooru.txt"
    api_base = "https://gelbooru.com/index.php"

    def __init__(self, config):
        super().__init__(config)
        self.http.session.headers.update({"Referer": "https://gelbooru.com/"})


    def auth_status(self) -> AuthStatus:
        login = self.config.get("GELBOORU_USERNAME")
        api_key = self.config.get("GELBOORU_API_KEY")
        user_id = self.config.get("GELBOORU_USER_ID")
        creds = [login, api_key, user_id]
        if not all(creds) or any(v.lower() == "template" for v in creds if v):
            return AuthStatus("Dead", t("gelbooru.auth_missing"), False)
        return AuthStatus("Alive", t("gelbooru.auth_ok"), False)

    def supports(self, url: str) -> bool:
        return "gelbooru.com" in url

    def _post_id(self, url: str) -> str | None:
        m = re.search(r"[?&]id=(\d+)", url)
        return m.group(1) if m else None

    def resolve(self, url: str) -> ProviderResult:
        post_id = self._post_id(url)
        if not post_id:
            return ProviderResult(url, False, error=t("gelbooru.bad_url"))
        params = {"page": "dapi", "s": "post", "q": "index", "json": "1", "id": post_id}
        login = self.config.get("GELBOORU_USERNAME")
        api_key = self.config.get("GELBOORU_API_KEY")
        user_id = self.config.get("GELBOORU_USER_ID")
        creds = [login, api_key, user_id]
        if not all(creds) or any(v.lower() == "template" for v in creds if v):
            return ProviderResult(
                url,
                False,
                error=t("gelbooru.need_creds"),
            )
        params.update({"login": login, "api_key": api_key, "user_id": user_id})
        try:
            data = self.http.get_json(self.api_base, params=params)
        except Exception as e:
            return ProviderResult(url, False, error=t("gelbooru.api_error", error=e))
        post = None
        if isinstance(data, dict):
            posts = data.get("post") or []
            post = posts[0] if posts else None
        if not post:
            return ProviderResult(url, False, error=t("gelbooru.not_found", post_id=post_id))
        file_url = post.get("file_url") or post.get("sample_url") or post.get("preview_url")
        if not file_url:
            return ProviderResult(url, False, error=t("gelbooru.no_media", post_id=post_id))
        ext = ext_from_url(file_url, "." + post.get("file_ext", "bin"))
        return ProviderResult(url, True, [DownloadTarget(file_url, f"gelbooru__{post_id}{ext}", referer="https://gelbooru.com/")])

class MoebooruProvider(Provider):
    """Shared provider for yande.re / konachan-like post.json APIs."""
    name = "moebooru"
    links_filename = "moebooru.txt"
    host = ""
    url_regex = r"/post/show/(\d+)"
    # Moebooru auth = login + password_hash, where the hash is
    # sha1(salt.format(password)). The salt differs per site.
    password_salt = ""

    def __init__(self, config):
        super().__init__(config)
        self.http.session.headers.update({"Referer": "https:/" + "/" + self.host + "/"})

    def _cred(self, suffix: str) -> str:
        val = (self.config.get(f"{self.name.upper()}_{suffix}") or "").strip()
        if not val or val.lower() == "template":
            return ""
        return val

    def _auth_params(self) -> dict:
        """Build moebooru auth query params from configured credentials.

        Prefers a precomputed PASSWORD_HASH, otherwise derives it from a plain
        PASSWORD using the site salt. Falls back to the legacy API_KEY field.
        """
        login = self._cred("USERNAME")
        if not login:
            return {}
        password_hash = self._cred("PASSWORD_HASH")
        password = self._cred("PASSWORD")
        if not password_hash and password and self.password_salt:
            password_hash = hashlib.sha1(
                self.password_salt.format(password).encode("utf-8")
            ).hexdigest()
        if password_hash:
            return {"login": login, "password_hash": password_hash}
        api_key = self._cred("API_KEY")
        if api_key:
            return {"login": login, "api_key": api_key}
        return {}

    def auth_status(self) -> AuthStatus:
        login = self._cred("USERNAME")
        if not login:
            return AuthStatus("N/A", t("moebooru.auth_optional"), False)
        if self._auth_params():
            return AuthStatus("Alive", t("moebooru.auth_ok", name=self.name), False)
        return AuthStatus(
            "Dead",
            t("moebooru.auth_incomplete", name=self.name),
            False,
        )

    def supports(self, url: str) -> bool:
        return self.host in url

    def _post_id(self, url: str) -> str | None:
        m = re.search(self.url_regex, urlparse(url).path)
        return m.group(1) if m else None

    def resolve(self, url: str) -> ProviderResult:
        post_id = self._post_id(url)
        if not post_id:
            return ProviderResult(url, False, error=t("moebooru.bad_url", name=self.name))
        api = "https:/" + "/" + self.host + "/post.json"
        params = {"tags": f"id:{post_id}"}
        # Auth is optional for public posts but recommended to avoid throttling.
        params.update(self._auth_params())
        data = self.http.get_json(api, params=params)
        post = data[0] if isinstance(data, list) and data else None
        if not post:
            return ProviderResult(url, False, error=t("moebooru.not_found", name=self.name, post_id=post_id))
        file_url = post.get("file_url") or post.get("jpeg_url") or post.get("sample_url") or post.get("preview_url")
        if not file_url:
            return ProviderResult(url, False, error=t("moebooru.no_media", name=self.name, post_id=post_id))
        ext = ext_from_url(file_url, "." + post.get("file_ext", "bin"))
        return ProviderResult(url, True, [DownloadTarget(file_url, f"{self.name}__{post_id}{ext}", referer="https:/" + "/" + self.host + "/")])

class KonachanProvider(MoebooruProvider):
    name = "Konachan"
    links_filename = "konachan.txt"
    host = "konachan.com"
    password_salt = "So-I-Heard-You-Like-Mupkids-?--{}--"

class YandereProvider(MoebooruProvider):
    name = "Yandere"
    links_filename = "yandere.txt"
    host = "yande.re"
    password_salt = "choujin-steiner--{}--"
