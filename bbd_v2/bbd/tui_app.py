"""Full-screen Textual TUI for Batch Booru Downloader.

This is the single entry point for the program (``python main.py``).

Design goals (mirrors osu!collect / Surge-style TUIs):
- Two tabs: Home (providers + live download + log) and Config (editable settings).
- A providers table with auth state and queued-link counts.
- A per-provider status line that adapts to the selected provider.
- A download view with an overall counter and a per-file ASCII progress bar + speed.
- A status log and a footer with key bindings.

Threading model: downloads run in a background thread (DownloadRunner). The
runner emits Events from that thread; we marshal every UI mutation back onto the
Textual event loop via ``App.call_from_thread`` so the UI stays responsive.

Interactive auth (browser + cookie/token paste) cannot run inside the alternate
screen, so ``action_refresh_auth`` uses ``App.suspend()`` to drop back to the
normal terminal. We clear the terminal on the way in and out so the operation
is not confused with leftover shell output.

Reboot: ``action_reboot`` exits the app and ``run_app`` re-creates it in a loop
(reloading settings.env). We intentionally avoid ``os.execv`` because on Windows
consoles it tended to leave the terminal in a state where input stopped working.

Glyphs: to survive terminals/fonts without box-drawing glyphs we use ASCII
borders, a block-character progress bar (█/░), hide the tab underline bar and
the header icon, and disable the command palette. Popups are navigated with
arrow keys (no focus-dependent buttons). See the README for the recommended font.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Static,
    TabbedContent,
    TabPane,
)

# RichLog was named TextLog before Textual 0.49; support both.
try:  # pragma: no cover - import shim
    from textual.widgets import RichLog as _LogWidget
except Exception:  # pragma: no cover
    from textual.widgets import TextLog as _LogWidget  # type: ignore

from .core.config import AppConfig, TRUE_VALUES
from .core.strings import t
from .core.events import Event
from .core.logging_setup import setup_logging
from .core.runner import DownloadRunner
from .providers import make_providers
from .core.package_updates import check_package_update, pip_upgrade_capture

_MB = 1024 * 1024
_BAR_WIDTH = 36
_UPDATE_PACKAGES = ("gppt", "pixivpy3", "yt-dlp", "curl_cffi")

_UGOIRA_LABELS = {"zip_only": "ZIP", "gif_only": "GIF", "both": "ZIP+GIF"}


@dataclass
class SettingSpec:
    """A single user-editable setting surfaced on the Config tab."""

    key: str
    label: str
    kind: str  # "choice" | "bool" | "text" | "number"
    default: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    restart: bool = False
    apply: Callable[["BBDApp", str], None] | None = None


# Settings that can be changed live from the Config tab. Items flagged
# ``restart=True`` only take effect after a reboot (press 'p').
SETTINGS: list[SettingSpec] = [
    SettingSpec(
        "PIXIV_UGOIRA_CONVERSION_MODE", t("set.pixiv_ugoira"), "choice",
        default="gif_only",
        choices=(("zip_only", "ZIP"), ("gif_only", "GIF"), ("both", "ZIP+GIF")),
        apply=lambda app, v: app._apply_pixiv_mode(v),
    ),
    SettingSpec(
        "DANBOORU_COOKIE_TTL_MINUTES", t("set.danbooru_ttl"), "number",
        default="30",
        apply=lambda app, v: app._apply_danbooru_ttl(v),
    ),
    SettingSpec(
        "TWITTER_YTDLP_FORMAT", t("set.twitter_format"), "text",
        default="best", restart=True,
    ),
    SettingSpec(
        "APP_LOG_LEVEL", t("set.log_level"), "choice",
        default="INFO",
        choices=(("DEBUG", "DEBUG"), ("INFO", "INFO"), ("WARNING", "WARNING"), ("ERROR", "ERROR")),
        restart=True,
    ),
    SettingSpec(
        "REQUEST_DELAY", t("set.request_delay"), "number",
        default="1.1", restart=True,
    ),
    SettingSpec(
        "MAX_RETRIES", t("set.max_retries"), "number",
        default="3", restart=True,
    ),
    SettingSpec(
        "CHECK_LIBRARY_UPDATES", t("set.check_updates"), "bool",
        default="true",
    ),
]
SETTINGS_BY_KEY = {s.key: s for s in SETTINGS}


def _count_queued(path: Path) -> int:
    """Number of non-comment, non-empty lines in a links file."""
    if not path.exists():
        return 0
    n = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if s and not s.startswith("#"):
            n += 1
    return n


def _render_bar(frac: float) -> str:
    """ASCII/block progress bar that renders even without fancy glyphs."""
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * _BAR_WIDTH))
    bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
    return f"[green]{bar}[/green] {int(frac * 100):3d}%"


def _is_true(value: str) -> bool:
    return (value or "").strip().lower() in TRUE_VALUES


class _TuiLogHandler(logging.Handler):
    """Route Python logging records into the on-screen log (DEBUG mode only).

    The console logging handler is disabled while the TUI owns the screen, so
    without this nothing reaches the log panel. We only attach it when the user
    selects DEBUG, to keep the panel clean at INFO. Writes are marshalled onto
    the UI thread when they originate from a background (download) thread.
    """

    def __init__(self, app: "BBDApp") -> None:
        super().__init__()
        self.app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = escape(self.format(record))
        except Exception:
            return
        color = {"DEBUG": "dim", "WARNING": "yellow", "ERROR": "red", "CRITICAL": "red"}.get(record.levelname)
        out = f"[{color}]{text}[/{color}]" if color else text
        app = self.app
        try:
            if threading.get_ident() == getattr(app, "_ui_thread_id", None):
                app._safe_log(out)
            else:
                app.call_from_thread(app._safe_log, out)
        except Exception:
            pass


class ChoiceModal(ModalScreen[str]):
    """Pick one option from a horizontal list using the arrow keys.

    Buttons require focus/Tab navigation which proved fiddly, so this popup is
    driven entirely by ←/→ + Enter (Esc cancels). Dismisses with the chosen
    value, or None on cancel.
    """

    BINDINGS = [
        Binding("left", "prev", "←", show=False),
        Binding("right", "next", "→", show=False),
        Binding("enter", "confirm", "OK", show=False),
        Binding("escape", "cancel", "Esc", show=False),
    ]

    def __init__(self, title: str, choices: tuple[tuple[str, str], ...], current: str) -> None:
        super().__init__()
        self.title_text = title
        self.choices = list(choices)
        self.index = 0
        for i, (value, _label) in enumerate(self.choices):
            if value == current:
                self.index = i
                break

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self.title_text, id="dialog_title")
            yield Static(self._choices_markup(), id="dialog_choices")
            yield Static(t("modal.choice_hint"), id="dialog_hint")

    def _choices_markup(self) -> str:
        parts = []
        for i, (_value, label) in enumerate(self.choices):
            if i == self.index:
                parts.append(f"[reverse] {label} [/reverse]")
            else:
                parts.append(f" {label} ")
        return "   ".join(parts)

    def _update_choices(self) -> None:
        self.query_one("#dialog_choices", Static).update(self._choices_markup())

    def action_prev(self) -> None:
        self.index = (self.index - 1) % len(self.choices)
        self._update_choices()

    def action_next(self) -> None:
        self.index = (self.index + 1) % len(self.choices)
        self._update_choices()

    def action_confirm(self) -> None:
        self.dismiss(self.choices[self.index][0])

    def action_cancel(self) -> None:
        self.dismiss(None)


class InputModal(ModalScreen[str]):
    """Single-line text/number input popup. Enter confirms, Esc cancels."""

    BINDINGS = [Binding("escape", "cancel", "Esc", show=False)]

    def __init__(self, title: str, current: str) -> None:
        super().__init__()
        self.title_text = title
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self.title_text, id="dialog_title")
            yield Input(value=self.current, id="dialog_input")
            yield Static(t("modal.input_hint"), id="dialog_hint")

    def on_mount(self) -> None:
        self.query_one("#dialog_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class BBDApp(App):
    TITLE = t("app.title")

    # The command palette hint uses a glyph many consoles can't render; drop it.
    ENABLE_COMMAND_PALETTE = False

    # ASCII borders + hidden glyph-heavy chrome so plain consoles don't show '?'.
    CSS = """
    Screen { layout: vertical; }
    HeaderIcon { visibility: hidden; }
    Input { border: ascii $accent; }
    #providers { height: 1fr; border: ascii $primary; }
    #dl_panel { height: auto; border: ascii $accent; padding: 0 1; }
    #provider_status { color: $text-muted; }
    #dl_overall { color: $text-muted; }
    #dl_file { text-style: bold; }
    #dl_bar { color: $accent; }
    #log { height: 1fr; border: ascii $secondary; }
    #config_pane { height: 1fr; }
    #config_help { height: auto; padding: 0 1; }
    #settings { height: 1fr; border: ascii $primary; }
    #config_readonly { height: auto; border: ascii $secondary; padding: 1 1; }
    ChoiceModal, InputModal { align: center middle; }
    #dialog { width: 70; height: auto; border: ascii $accent; background: $panel; padding: 1 2; }
    #dialog_title { text-style: bold; }
    #dialog_choices { padding: 1 0; }
    #dialog_hint { color: $text-muted; }
    """

    BINDINGS = [
        Binding("d", "download", t("bind.download")),
        Binding("r", "reload_links", t("bind.reload_links")),
        Binding("a", "refresh_auth", t("bind.auth")),
        Binding("c", "recheck_auth", t("bind.recheck_auth")),
        Binding("u", "check_updates", t("bind.check_updates")),
        Binding("U", "upgrade_packages", t("bind.upgrade")),
        Binding("p", "reboot", t("bind.reboot")),
        Binding("1", "show_home", t("bind.home"), show=False),
        Binding("2", "show_config", t("bind.config"), show=False),
        Binding("q", "quit", t("bind.quit")),
    ]

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.cfg = config
        self.providers = make_providers(config)
        self.by_name = {p.name: p for p in self.providers}
        self.provider_names = [p.name for p in self.providers]
        self._auth_cache: dict[str, tuple[str, str]] = {n: ("…", "") for n in self.provider_names}
        self._busy = False
        self._upgrading = False
        self._reboot = False

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="home"):
            with TabPane(t("tab.home"), id="home"):
                yield DataTable(id="providers", cursor_type="row", zebra_stripes=True)
                with Vertical(id="dl_panel"):
                    yield Static("", id="provider_status")
                    yield Static(t("dl.queue_idle"), id="dl_overall")
                    yield Static(t("dl.ready"), id="dl_file")
                    yield Static(_render_bar(0.0), id="dl_bar")
                yield _LogWidget(id="log", highlight=False, markup=True)
            with TabPane(t("tab.config"), id="config"):
                with Vertical(id="config_pane"):
                    yield Static(
                        t("config.help"),
                        id="config_help",
                    )
                    yield DataTable(id="settings", cursor_type="row", zebra_stripes=True)
                    yield Static(self._readonly_text(), id="config_readonly")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#providers", DataTable)
        table.add_column(t("col.provider"), key="provider", width=10)
        table.add_column(t("col.auth"), key="auth", width=7)
        table.add_column(t("col.queued"), key="queued", width=10)
        table.add_column(t("col.links"), key="links")
        for p in self.providers:
            table.add_row(
                p.name,
                Text("…"),
                str(_count_queued(p.links_file)),
                str(p.links_file),
                key=p.name,
            )
        settings = self.query_one("#settings", DataTable)
        settings.add_column(t("col.param"), key="param", width=42)
        settings.add_column(t("col.value"), key="value", width=14)
        for spec in SETTINGS:
            suffix = t("restart.marker") if spec.restart else ""
            settings.add_row(spec.label + suffix, self._setting_display(spec), key=spec.key)
        # Hide the moving tab underline bar (uses glyphs some fonts lack).
        try:
            for u in self.query("Underline"):
                u.display = False
        except Exception:
            pass
        self._update_provider_status()
        self.set_interval(1, self._update_provider_status)
        # In DEBUG mode, mirror Python logging into the on-screen log panel.
        self._ui_thread_id = threading.get_ident()
        if self.cfg.log_level == "DEBUG":
            handler = _TuiLogHandler(self)
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(logging.Formatter("%(levelname)s | %(name)s | %(message)s"))
            logging.getLogger().addHandler(handler)
            self._log(t("log.debug_on"))
        # Check auth in the background so the UI never blocks on network.
        threading.Thread(target=self._auth_scan, daemon=True).start()
        if self.cfg.bool("CHECK_LIBRARY_UPDATES", True):
            self._log(t("log.checking_updates"))
            threading.Thread(target=self._updates_run, daemon=True).start()
        self.call_after_refresh(self._focus_active_tab)

    # ------------------------------------------------------------------ focus / tabs
    def _focus_active_tab(self) -> None:
        active = self.query_one(TabbedContent).active
        try:
            if active == "home":
                self.query_one("#providers", DataTable).focus()
            elif active == "config":
                self.query_one("#settings", DataTable).focus()
        except Exception:
            pass

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.call_after_refresh(self._focus_active_tab)

    def _switch_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab
        self.call_after_refresh(self._focus_active_tab)

    def action_show_home(self) -> None:
        self._switch_tab("home")

    def action_show_config(self) -> None:
        self._switch_tab("config")

    # ------------------------------------------------------------------ helpers
    def _log(self, text: str) -> None:
        self.query_one("#log", _LogWidget).write(text)

    def _safe_log(self, text: str) -> None:
        """Best-effort log write that never raises (used by the logging handler)."""
        try:
            self.query_one("#log", _LogWidget).write(text)
        except Exception:
            pass

    def _log_from_thread(self, text: str) -> None:
        self.call_from_thread(self._log, text)

    def _clear_terminal(self) -> None:
        try:
            os.system("cls" if os.name == "nt" else "clear")
        except Exception:
            print("\033[2J\033[3J\033[H", end="")

    def _readonly_text(self) -> str:
        c = self.cfg
        return "\n".join([
            t("config.readonly_header"),
            f"download_dir : {c.download_dir}",
            f"links_dir    : {c.links_dir}",
            f"logs_dir     : {c.logs_dir}",
            f"tokens_dir   : {c.tokens_dir}",
        ])

    def _setting_display(self, spec: SettingSpec) -> str:
        val = self.cfg.get(spec.key, spec.default)
        if spec.kind == "bool":
            return t("val.on") if _is_true(val) else t("val.off")
        if spec.kind == "choice":
            for value, label in spec.choices:
                if value == val:
                    return label
            return val or t("val.default")
        return val or t("val.default")

    def _selected_name(self) -> str | None:
        table = self.query_one("#providers", DataTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self.provider_names):
            return None
        return self.provider_names[row]

    def _set_auth_cell(self, name: str, state: str, message: str) -> None:
        color = {"Alive": "green", "N/A": "yellow", "Dead": "red"}.get(state, "white")
        table = self.query_one("#providers", DataTable)
        table.update_cell(name, "auth", Text(state, style=color))
        self._auth_cache[name] = (state, message)
        if message:
            self._log(t("log.auth_status", name=name, state=state, message=message))
        self._update_provider_status()

    def _refresh_queued(self, name: str | None = None) -> None:
        table = self.query_one("#providers", DataTable)
        names = [name] if name else self.provider_names
        for n in names:
            table.update_cell(n, "queued", str(_count_queued(self.by_name[n].links_file)))

    def _provider_by_key(self, key: str):
        """Look up a provider by its canonical lowercase key, regardless of display-name casing."""
        key = key.lower()
        for pname, prov in self.by_name.items():
            if pname.lower() == key:
                return prov
        return None

    def _provider_extra(self, name: str) -> str:
        """Provider-specific detail appended to the status line."""
        key = name.lower()
        if key == "danbooru":
            prov = self._provider_by_key("danbooru")
            info = prov.cookie_info() if prov is not None and hasattr(prov, "cookie_info") else None
            if info is None:
                return t("extra.cookie_none")
            age, remaining, ttl = info
            if remaining > 0:
                total = int(remaining)
                return t("extra.cookie_live", mm=total // 60, ss=total % 60, ttl=int(ttl))
            return t("extra.cookie_expired")
        if key == "pixiv":
            mode = self.cfg.get("PIXIV_UGOIRA_CONVERSION_MODE", "gif_only")
            return t("extra.ugoira", mode=_UGOIRA_LABELS.get(mode, mode))
        if key == "twitter":
            return t("extra.twitter", fmt=self.cfg.get("TWITTER_YTDLP_FORMAT", "best"))
        return ""

    def _update_provider_status(self) -> None:
        try:
            widget = self.query_one("#provider_status", Static)
        except Exception:
            return
        name = self._selected_name()
        if not name:
            widget.update("")
            return
        state, message = self._auth_cache.get(name, ("…", ""))
        color = {"Alive": "green", "N/A": "yellow", "Dead": "red"}.get(state, "white")
        line = t("status.auth", name=name, color=color, state=state)
        if message:
            line += t("status.auth_message", message=message)
        line += self._provider_extra(name)
        widget.update(line)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "providers":
            self._update_provider_status()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "settings":
            key = event.row_key.value
            if key:
                self._edit_setting(key)

    # ------------------------------------------------------------------ settings editing
    def _edit_setting(self, key: str) -> None:
        spec = SETTINGS_BY_KEY.get(key)
        if spec is None:
            return
        current = self.cfg.get(spec.key, spec.default)
        if spec.kind == "bool":
            self._save_setting(spec, "false" if _is_true(current) else "true")
        elif spec.kind == "choice":
            self.push_screen(
                ChoiceModal(spec.label, spec.choices, current),
                lambda value, s=spec: self._save_setting(s, value),
            )
        else:
            self.push_screen(
                InputModal(spec.label, current),
                lambda value, s=spec: self._save_setting(s, value),
            )

    def _save_setting(self, spec: SettingSpec, value: str | None) -> None:
        if value is None:
            return
        value = value.strip()
        if spec.kind == "number":
            try:
                float(value)
            except ValueError:
                self._log(t("log.need_number", label=spec.label, value=value))
                return
        try:
            self.cfg.set_env_value(spec.key, value)
        except Exception as e:  # pragma: no cover - defensive
            self._log(t("log.save_failed", error=e))
            return
        if spec.apply is not None:
            try:
                spec.apply(self, value)
            except Exception as e:  # pragma: no cover - defensive
                self._log(t("log.apply_failed", label=spec.label, error=e))
        try:
            self.query_one("#settings", DataTable).update_cell(spec.key, "value", self._setting_display(spec))
        except Exception:
            pass
        self._update_provider_status()
        hint = t("log.restart_hint") if spec.restart else ""
        self._log(t("log.setting_saved", label=spec.label, value=self._setting_display(spec), hint=hint))

    def _apply_pixiv_mode(self, value: str) -> None:
        prov = self._provider_by_key("pixiv")
        if prov is not None:
            prov.ugoira_mode = value

    def _apply_danbooru_ttl(self, value: str) -> None:
        prov = self._provider_by_key("danbooru")
        if prov is not None:
            try:
                prov.cookie_ttl_minutes = float(value)
            except ValueError:
                pass

    # ------------------------------------------------------------------ auth scan
    def _auth_scan(self) -> None:
        for p in self.providers:
            try:
                st = p.auth_status()
                self.call_from_thread(self._set_auth_cell, p.name, st.state, st.message)
            except Exception as e:  # pragma: no cover - defensive
                self.call_from_thread(self._set_auth_cell, p.name, "Dead", f"check failed: {e}")

    def action_recheck_auth(self) -> None:
        self._log(t("log.checking_auth"))
        threading.Thread(target=self._auth_scan, daemon=True).start()

    # ------------------------------------------------------------------ links
    def action_reload_links(self) -> None:
        name = self._selected_name()
        if not name:
            return
        self._refresh_queued(name)
        n = _count_queued(self.by_name[name].links_file)
        self._log(t("log.reloaded_links", name=name, n=n))

    # ------------------------------------------------------------------ auth refresh (interactive)
    def action_refresh_auth(self) -> None:
        if self._busy:
            self._log(t("log.auth_busy"))
            return
        name = self._selected_name()
        if not name:
            return
        provider = self.by_name[name]
        status = provider.auth_status()
        if not status.can_refresh:
            self._log(t("log.auth_no_refresh", name=name, message=status.message))
            return
        new_status = None
        # Leave the alternate screen so browser prompts and input() work. Clear
        # the terminal on the way in and out so old shell output can't confuse.
        with self.suspend():
            self._clear_terminal()
            print("=" * 64)
            print(t("auth.suspend_title", name=name))
            print("=" * 64)
            print(t("auth.suspend_intro"))
            try:
                new_status = provider.refresh_auth_interactive()
                print(t("auth.suspend_result", state=new_status.state, message=new_status.message))
            except Exception as e:  # pragma: no cover - defensive
                new_status = None
                print(t("auth.suspend_error", error=e))
            try:
                input(t("auth.suspend_return"))
            except Exception:
                pass
            self._clear_terminal()
        if new_status is not None:
            self._set_auth_cell(name, new_status.state, new_status.message)
        self._update_provider_status()

    # ------------------------------------------------------------------ downloads
    def action_download(self) -> None:
        name = self._selected_name()
        if name:
            self._start(name)

    def _start(self, name: str) -> None:
        if self._busy:
            self._log(t("log.already_downloading"))
            return
        self._busy = True
        self._log(t("log.start", name=name))
        self.query_one("#dl_bar", Static).update(_render_bar(0.0))
        threading.Thread(target=self._download_run, args=(name,), daemon=True).start()

    def _download_run(self, name: str) -> None:
        try:
            provider = self.by_name[name]
            runner = DownloadRunner(self.cfg, [provider], self._emit)
            try:
                st = provider.auth_status()
            except Exception as e:
                self._emit(Event("error", provider=name, message=t("event.auth_check_failed", error=e), level="error"))
                return
            if st.state not in ("Alive", "N/A"):
                self._emit(Event("error", provider=name, message=t("event.auth_not_ready", message=st.message), level="error"))
                return
            runner.run_provider(name)
        finally:
            self.call_from_thread(self._download_done)

    def _download_done(self) -> None:
        self._busy = False
        self.query_one("#dl_file", Static).update(t("dl.ready"))
        self._refresh_queued()
        self._log(t("log.download_done"))

    # Called from the download thread; marshal to the UI thread.
    def _emit(self, event: Event) -> None:
        self.call_from_thread(self._handle_event, event)

    def _handle_event(self, event: Event) -> None:
        if event.type == "file_progress":
            speed = event.speed_bps / _MB
            done_mb = event.downloaded / _MB
            if event.total > 0:
                frac = event.downloaded / event.total
                total_mb = event.total / _MB
                self.query_one("#dl_bar", Static).update(_render_bar(frac))
                self.query_one("#dl_file", Static).update(
                    t("dl.file_progress", filename=event.filename, done=done_mb, total=total_mb, speed=speed)
                )
            else:
                # Unknown size (HLS etc.) -> indeterminate shaded bar.
                self.query_one("#dl_bar", Static).update(
                    "[yellow]" + "▒" * _BAR_WIDTH + "[/yellow]" + t("dl.size_unknown")
                )
                self.query_one("#dl_file", Static).update(
                    t("dl.file_progress_nosize", filename=event.filename, done=done_mb, speed=speed)
                )
            return
        if event.type == "overall":
            self.query_one("#dl_overall", Static).update(
                t("dl.overall", done=event.done, mb=event.downloaded / _MB)
            )
            return
        prefix = (event.provider or "bbd").upper()
        if event.type == "error":
            self._log(t("log.event_error", prefix=prefix, message=event.message))
        else:
            self._log(t("log.event_info", prefix=prefix, message=event.message))

    # ------------------------------------------------------------------ updates (logged to Home)
    def action_check_updates(self) -> None:
        self._switch_tab("home")
        self._log(t("log.checking_updates"))
        threading.Thread(target=self._updates_run, daemon=True).start()

    def _updates_run(self) -> None:
        for pkg in _UPDATE_PACKAGES:
            try:
                status = check_package_update(pkg)
                color = "yellow" if status.update_available else "green"
                self._log_from_thread(f"[{color}]{status.message}[/{color}]")
            except Exception as e:  # pragma: no cover - defensive
                self._log_from_thread(t("log.pkg_error", pkg=pkg, error=e))

    def action_upgrade_packages(self) -> None:
        if self._upgrading:
            self._log(t("log.upgrade_busy"))
            return
        self._upgrading = True
        self._switch_tab("home")
        self._log(t("log.upgrade_start"))
        threading.Thread(target=self._upgrade_run, daemon=True).start()

    def _upgrade_run(self) -> None:
        try:
            upgraded_any = False
            for pkg in _UPDATE_PACKAGES:
                try:
                    status = check_package_update(pkg)
                except Exception as e:
                    self._log_from_thread(t("log.pkg_check_failed", pkg=pkg, error=e))
                    continue
                if not status.update_available:
                    self._log_from_thread(t("log.pkg_uptodate", pkg=pkg, installed=status.installed))
                    continue
                upgraded_any = True
                self._log_from_thread(
                    t("log.pkg_upgrading", pkg=pkg, installed=status.installed, latest=status.latest)
                )
                try:
                    code, out = pip_upgrade_capture(pkg)
                except Exception as e:
                    self._log_from_thread(t("log.pkg_pip_failed", pkg=pkg, error=e))
                    continue
                tail = "\n".join([l for l in out.splitlines() if l.strip()][-2:])
                tail = tail.replace("[", "\\[")  # keep RichLog markup happy
                if code == 0:
                    self._log_from_thread(t("log.pkg_done", pkg=pkg, tail=tail))
                else:
                    self._log_from_thread(t("log.pkg_pip_code", pkg=pkg, code=code, tail=tail))
            if not upgraded_any:
                self._log_from_thread(t("log.all_uptodate"))
            self._log_from_thread(t("log.restart_to_apply"))
        finally:
            self.call_from_thread(self._upgrade_finished)

    def _upgrade_finished(self) -> None:
        self._upgrading = False

    # ------------------------------------------------------------------ reboot
    def action_reboot(self) -> None:
        if self._busy:
            self._log(t("log.reboot_busy"))
            return
        self._reboot = True
        self.exit()


def run_app(argv: list[str] | None = None) -> int:
    # Re-create the app on reboot so settings.env changes take effect, without
    # os.execv (which broke terminal input on Windows consoles).
    while True:
        cfg = AppConfig.load()
        # The TUI owns the terminal screen; console logging would corrupt it, so
        # we force file-only logging regardless of APP_LOG_TO_CONSOLE.
        cfg.log_to_console = False
        setup_logging(cfg)
        app = BBDApp(cfg)
        app.run()
        if not getattr(app, "_reboot", False):
            return 0
