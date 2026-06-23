from __future__ import annotations
import json
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from importlib import metadata

try:
    from packaging.version import Version
except Exception:  # packaging обычно приходит вместе с pip, но оставляем fallback
    Version = None  # type: ignore


@dataclass(frozen=True)
class PackageUpdateStatus:
    package: str
    installed: str | None
    latest: str | None
    update_available: bool
    message: str


def get_installed_version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def get_pypi_latest_version(package: str, timeout: float = 5.0) -> str | None:
    url = f"https://pypi.org/pypi/{package}/json"
    req = urllib.request.Request(url, headers={"User-Agent": "Batch-Booru-Downloader/update-check"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("info", {}).get("version") or None


def _is_newer(latest: str, installed: str) -> bool:
    if Version is not None:
        return Version(latest) > Version(installed)
    return latest != installed


def check_package_update(package: str, timeout: float = 5.0) -> PackageUpdateStatus:
    installed = get_installed_version(package)
    if not installed:
        return PackageUpdateStatus(package, None, None, False, f"{package} is not installed")
    try:
        latest = get_pypi_latest_version(package, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        return PackageUpdateStatus(package, installed, None, False, f"Cannot check {package} updates: {e}")
    if not latest:
        return PackageUpdateStatus(package, installed, None, False, f"Cannot read latest {package} version from PyPI")
    update_available = _is_newer(latest, installed)
    if update_available:
        msg = f"{package} update available: installed {installed}, latest {latest}"
    else:
        msg = f"{package} is up to date: {installed}"
    return PackageUpdateStatus(package, installed, latest, update_available, msg)


def pip_upgrade(package: str) -> int:
    return subprocess.call([sys.executable, "-m", "pip", "install", "--upgrade", package])


def pip_upgrade_capture(package: str) -> tuple[int, str]:
    """Upgrade a package, capturing output instead of writing to the console.

    The TUI owns the terminal screen, so pip output must not be printed directly
    (it would corrupt the interface). Returns (returncode, combined_output).
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", package],
        capture_output=True,
        text=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
