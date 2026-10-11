"""Pop-up notifications: all, only the errors, or none (#157).

A setting on the Program tab picks which pop-ups show. "Errors" keeps the
ones that report something going wrong - by their i18n keys, matched in every
language, a formatted one with its placeholders standing for anything - and
hides the rest (NR on/off, "settings applied", "recording started"); "None"
hides every one. A hidden pop-up still goes to the log.

Checked without a window:

* the filter (Display.alert on a bare Display): every error pop-up of every
  language, filled in, shows under "errors"; the info ones do not; "none"
  shows nothing; a hidden one is printed;
* the menu: the Program tab has the all/errors/none row, a pick asks for
  ("notifications", value), and the handler stores it and tells the display;
* the config: an unknown value falls back to "all", and the choice is saved.

Run:  runtime\\python.exe tests\\test_notification_levels.py
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))

import pygame  # noqa: E402

INFO_KEYS = ("nr_on", "nr_off", "started", "settings_applied", "win_mode_on",
             "record_started", "shot_saved", "fg_on", "preset_saved")


def _filled(text: str) -> str:
    return re.sub(r"\{[^{}]*\}", "X1", text)


def _check_filter(failures: list) -> None:
    import display
    from i18n import STRINGS

    def bare(level):
        d = object.__new__(display.Display)
        d._alerts = []
        d.set_alert_level(level)
        return d

    for lang, table in STRINGS.items():
        for key in display.ALERT_ERROR_KEYS:
            if key not in table:
                failures.append(f"{lang}: the error key {key!r} is not in the strings")
                continue
            d = bare("errors")
            d.alert(_filled(table[key]))
            if not d._alerts:
                failures.append(f"{lang}: the error pop-up {key!r} was hidden under 'errors'")
        for key in INFO_KEYS:
            d = bare("errors")
            with contextlib.redirect_stdout(io.StringIO()):
                d.alert(_filled(table[key]))
            if d._alerts:
                failures.append(f"{lang}: the info pop-up {key!r} showed under 'errors'")
    d = bare("errors")
    d.alert("REC ERROR: disk full")
    if not d._alerts:
        failures.append("'REC ERROR: ...' was hidden under 'errors'")
    d = bare("none")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        d.alert(STRINGS["en"]["win_fail"])
    if d._alerts:
        failures.append("'none' still showed a pop-up")
    if STRINGS["en"]["win_fail"] not in out.getvalue():
        failures.append("a hidden pop-up did not go to the log")
    d = bare("all")
    d.alert(STRINGS["en"]["nr_on"])
    if not d._alerts:
        failures.append("'all' hid a pop-up")
    print(f"    filter: {len(display.ALERT_ERROR_KEYS)} error pop-ups kept and "
          f"{len(INFO_KEYS)} info ones hidden in {len(STRINGS)} languages")


def _check_menu(failures: list) -> None:
    import commands
    import fonts
    import overlay_ui as ui
    menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False: fonts.load(size, mono=mono))
    menu.visible = True
    menu.page = "settings"
    menu.settings_tab = "app"
    menu.set_state({"nr": True, "gpu_ok": True, "profile": "Natural",
                    "profiles": ["Natural"], "lang": "en", "notifications": "errors"})
    menu.layout(1920, 1080)
    row = next((it for it in menu.items
                if it.kind == "segmented" and it.key == "notifications"), None)
    if row is None or list(row.payload or []) != ["all", "errors", "none"]:
        failures.append(f"the Program tab has no all/errors/none row "
                        f"({row and row.payload})")
        return
    actions = menu._pick("notifications", "none")
    if actions != [("notifications", "none")]:
        failures.append(f"picking 'none' asked for {actions}")
    levels = []
    st = types.SimpleNamespace(cfg={}, display=types.SimpleNamespace(
        set_alert_level=levels.append))
    with contextlib.redirect_stdout(io.StringIO()):
        commands.apply_menu_action(st, ("notifications", "errors"))
    if st.cfg.get("notifications") != "errors" or levels != ["errors"]:
        failures.append(f"the handler stored {st.cfg} and told the display {levels}")


def _check_config(failures: list) -> None:
    import settings_io
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text('{"notifications": "loud"}', encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            got = settings_io.load_config(path).get("notifications")
        if got != "all":
            failures.append(f"an unknown level loaded as {got!r}, not 'all'")
        path.write_text("{}", encoding="utf-8")
        menu = types.SimpleNamespace(user_scale=1.0, user_height=None, state={}, mini=False,
                                     mini_rows=(), offset=(0, 0))
        st = types.SimpleNamespace(
            cfg_path=path, cfg={"profile": "Natural", "notifications": "none"},
            params=dict(settings_io.PROFILES["Natural"], style=1), monitor=0, lang="en",
            work_scale=1.0, split_pos=0.0, startup_menu=False, nr_small=True,
            display=types.SimpleNamespace(menu=menu))
        settings_io.save_menu_layout(st)
        if json.loads(path.read_text(encoding="utf-8")).get("notifications") != "none":
            failures.append("the notification level was not saved")


def main() -> int:
    failures: list = []
    pygame.init()
    pygame.display.set_mode((64, 64))
    try:
        _check_filter(failures)
        _check_menu(failures)
    finally:
        pygame.quit()
    _check_config(failures)
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the notification setting keeps all, only the errors, or none - and "
          "the log keeps everything")
    return 0


if __name__ == "__main__":
    sys.exit(main())
