from __future__ import annotations
import logging, re, sys
from logging.handlers import RotatingFileHandler
from .config import AppConfig

SECRET_PATTERNS = [
    (re.compile(r"(api_key=)[^&\s]+", re.I), r"\1<redacted>"),
    (re.compile(r"(password=)[^&\s]+", re.I), r"\1<redacted>"),
    (re.compile(r"(refresh_token=)[^&\s]+", re.I), r"\1<redacted>"),
    (re.compile(r"(access_token=)[^&\s]+", re.I), r"\1<redacted>"),
    (re.compile(r"(Authorization: Bearer )[^;\s]+", re.I), r"\1<redacted>"),
    (re.compile(r"(Cookie: )(.+)", re.I), r"\1<redacted>"),
]

def _redact(value):
    if isinstance(value, str):
        for pattern, repl in SECRET_PATTERNS:
            value = pattern.sub(repl, value)
    return value

class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(x) for x in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _redact(v) for k, v in record.args.items()}
        return True


def setup_logging(config: AppConfig) -> None:
    level = getattr(logging, config.log_level, logging.INFO)
    root = logging.getLogger()
    root.handlers.clear()
    root.filters.clear()
    root.setLevel(level)
    root.addFilter(RedactingFilter())
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    file_handler = RotatingFileHandler(config.logs_dir / "bbd.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level)
    file_handler.addFilter(RedactingFilter())
    root.addHandler(file_handler)
    if config.log_to_console:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(levelname)-7s | %(message)s"))
        console.setLevel(level)
        console.addFilter(RedactingFilter())
        root.addHandler(console)
