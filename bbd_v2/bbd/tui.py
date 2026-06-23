"""Single entry point for Batch Booru Downloader.

The program is driven entirely by the full-screen Textual TUI (``bbd.tui_app``).
There is no separate CLI: provider listing, auth refresh, downloads, link reload
and helper-package update checks all live inside the interface.

Run with::

    python main.py

If Textual is not installed, we print a short, friendly install hint instead of
a traceback.
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    try:
        from .tui_app import run_app
    except ModuleNotFoundError as e:
        missing = getattr(e, "name", "") or str(e)
        print(
            "Интерфейс требует пакет 'textual'.\n"
            f"Не хватает модуля: {missing}\n"
            "Установите зависимости: pip install -r requirements.txt\n"
            "или только интерфейс: pip install textual",
            file=sys.stderr,
        )
        return 2
    return run_app(argv)


if __name__ == "__main__":
    raise SystemExit(main())
