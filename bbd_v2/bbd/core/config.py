from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os, sys, shutil

TRUE_VALUES = {"1", "true", "yes", "y", "on", "да"}


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        data[key] = value
    return data

@dataclass
class AppConfig:
    root: Path
    values: dict[str, str]
    download_dir: Path
    links_dir: Path
    logs_dir: Path
    tokens_dir: Path
    log_level: str
    log_to_console: bool
    request_delay: float
    max_retries: int
    chunk_size: int
    user_agent: str

    @classmethod
    def load(cls, root: Path | None = None, env_name: str = "settings.env") -> "AppConfig":
        root = root or app_root()
        env_path = root / env_name
        if not env_path.exists() and env_name == "settings.env":
            legacy = root / ".env"
            example = root / "settings.env.example"
            old_example = root / ".env.example"
            if legacy.exists():
                env_path = legacy
            elif example.exists():
                env_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            elif old_example.exists():
                env_path.write_text(old_example.read_text(encoding="utf-8"), encoding="utf-8")
        values = parse_env_file(env_path)
        # real environment wins over file for easy temporary overrides
        merged = {**values, **{k: v for k, v in os.environ.items() if k in values or k.startswith(("APP_", "DANBOORU_", "GELBOORU_", "KONACHAN_", "SANKAKU_", "PIXIV_", "TWITTER_"))}}
        def get(key: str, default: str = "") -> str:
            return merged.get(key, default)
        def path(key: str, default: str) -> Path:
            p = Path(get(key, default))
            return p if p.is_absolute() else root / p
        cfg = cls(
            root=root,
            values=merged,
            download_dir=path("DOWNLOAD_DIR", "downloads"),
            links_dir=path("LINKS_DIR", "links"),
            logs_dir=path("LOGS_DIR", "logs"),
            tokens_dir=path("TOKENS_DIR", "tokens"),
            log_level=get("APP_LOG_LEVEL", "INFO").upper(),
            log_to_console=get("APP_LOG_TO_CONSOLE", "true").lower() in TRUE_VALUES,
            request_delay=float(get("REQUEST_DELAY", "1.1") or 1.1),
            max_retries=int(get("MAX_RETRIES", "3") or 3),
            chunk_size=int(get("CHUNK_SIZE", "262144") or 262144),
            user_agent=get("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36"),
        )
        for d in [cfg.download_dir, cfg.links_dir, cfg.logs_dir, cfg.tokens_dir]:
            d.mkdir(parents=True, exist_ok=True)
        return cfg

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def set_env_value(self, key: str, value: str) -> None:
        """Persist KEY=value into settings.env and update the in-memory value."""
        set_env_value(self.root / "settings.env", key, value)
        self.values[key] = value

    def bool(self, key: str, default: bool = False) -> bool:
        val = self.values.get(key)
        if val is None:
            return default
        return val.lower() in TRUE_VALUES


def set_env_value(path: Path, key: str, value: str) -> None:
    """Insert or update ``KEY=value`` in an env file, preserving other lines.

    Comments, blank lines and ordering are kept. If the key is absent it is
    appended at the end. Used by the TUI to persist settings edits.
    """
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    out: list[str] = []
    found = False
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            existing_key = stripped.split("=", 1)[0].strip()
            if existing_key == key:
                out.append(f"{key}={value}")
                found = True
                continue
        out.append(raw)
    if not found:
        out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
