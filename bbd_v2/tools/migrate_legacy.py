from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD_KEYS = {
    "DANBOORU_USERNAME", "DANBOORU_API_KEY",
    "GELBOORU_USERNAME", "GELBOORU_API_KEY", "GELBOORU_USER_ID",
    "KONACHAN_USERNAME", "KONACHAN_API_KEY",
    "SANKAKU_LOGIN", "SANKAKU_PASSWORD",
    "PIXIV_UGOIRA_CONVERSION_MODE",
}

def main() -> int:
    old = ROOT / "settings" / "settings.json"
    env = ROOT / ".env"
    if not old.exists():
        print("settings/settings.json not found")
        return 1
    data = json.loads(old.read_text(encoding="utf-8"))
    existing = set()
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                existing.add(line.split("=", 1)[0].strip())
    with env.open("a", encoding="utf-8") as f:
        f.write("\n# Migrated from settings/settings.json\n")
        for key in sorted(OLD_KEYS):
            if key in data and key not in existing:
                f.write(f"{key}={data[key]}\n")
    print(f"Migrated values to {env}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
