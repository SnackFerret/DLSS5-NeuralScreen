"""Picking another window keeps the worker when nothing else has to change.

Switching the processed window from the menu went through a full rebuild: the
worker was killed and a new one started, NGX initialised again - 17-38 s with
a big model on a busy card, measured by the reporter of #157 (#148 part 2).
Yet the probe that measures the new window already re-points the running
worker's capture at it; what is left is what a window resize does live (RNSZ,
the guides, the shared sections, the overlay) - 0.6-0.9 s in that report.

What this pins, with no worker and no window of our own (real HWNDs of the
desktop and the shell only, so IsWindow says yes):

* window -> window on the same monitor, NR on, nothing recording: live -
  no teardown, no rebuild, the new window is the processed one, the veil the
  switch raised comes down;
* the old road stays for everything a live switch cannot know it can do: from
  the desktop, to another monitor, while recording, with NR off (the idle
  worker's capture is closed), and when RNSZ fails.

Run:  runtime\\python.exe tests\\test_window_switch_live.py
"""
import ctypes
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import channels  # noqa: E402
import pipeline  # noqa: E402

OLD = ctypes.windll.user32.GetDesktopWindow()
NEW = ctypes.windll.user32.GetShellWindow() or ctypes.windll.user32.GetForegroundWindow()
SIZE = (1600, 900)


class _Display:
    def __init__(self):
        self.calls = []
        self.menu = types.SimpleNamespace(visible=False)

    def enter_switch_mode(self, *a, **k):
        self.calls.append("veil up")

    def exit_switch_mode(self):
        self.calls.append("veil down")

    def resize(self, w, h):
        self.calls.append(f"resize {w}x{h}")

    def alert(self, *a, **k):
        self.calls.append("alert")


def _state(window=OLD, off=False, recorder=None, dda_mode=True):
    return types.SimpleNamespace(
        window_hwnd=window, want_dda=True, dda_mode=dda_mode, off_suspended=off,
        recorder=recorder, worker_failed=False, present_mode=False,
        motion_small=True, nr_small=False, nr_direct=False, work_scale=0.65,
        width=1280, height=720, work_w=832, work_h=468, params={"intensity": 1.0},
        follow_pos=(0, 0), follow_size=(1280, 720), follow_resize=None,
        frame_index=7, pts=99, work_frame=object(), last_restart=0.0,
        output_rgba=None, capture=types.SimpleNamespace(resolution=(2560, 1440)),
        worker=types.SimpleNamespace(poll=lambda: None),
        reader=types.SimpleNamespace(wait_rack=lambda timeout=0: None,
                                     set_output_size=lambda w, h: None),
        display=_Display(), cfg={"profile": "Natural"}, lang="en")


def _run(st, same_monitor=True, rack_raises=False) -> list:
    calls: list = []
    names = ("send_resize", "teardown_pipeline", "rebuild_pipeline",
             "TemporalGuideGenerator", "window_frame_rect", "_same_monitor")
    saved = {n: getattr(pipeline, n) for n in names}
    saved_ch = {n: getattr(channels, n) for n in
                ("probe_window_capture", "sync_motion_size", "sync_gray",
                 "forget_out", "enable_out_shm", "enable_present")}

    def resize_fn(*a, **k):
        calls.append("rnsz")
        if rack_raises:
            raise TimeoutError("no RACK")

    try:
        pipeline.send_resize = resize_fn
        pipeline.teardown_pipeline = lambda st: calls.append("teardown")
        pipeline.rebuild_pipeline = lambda st, note: calls.append("rebuild")
        pipeline.TemporalGuideGenerator = lambda w, h, **kw: types.SimpleNamespace()
        pipeline.window_frame_rect = lambda hwnd: (0, 0, SIZE[0], SIZE[1])
        pipeline._same_monitor = lambda a, b: same_monitor
        channels.probe_window_capture = lambda st, hwnd: calls.append("probe") or SIZE
        for n in ("sync_motion_size", "sync_gray", "forget_out", "enable_out_shm",
                  "enable_present"):
            setattr(channels, n, lambda st: None)
        pipeline.switch_window(st, NEW)
    finally:
        for n, fn in saved.items():
            setattr(pipeline, n, fn)
        for n, fn in saved_ch.items():
            setattr(channels, n, fn)
    return calls


def main() -> int:
    failures = []
    if not OLD or not NEW or OLD == NEW:
        print("SKIP: no two real windows to stand in for the old and the new one")
        return 0

    st = _state()
    calls = _run(st)
    print(f"    window -> window, same monitor: {calls}, display {st.display.calls}")
    if "teardown" in calls or "rebuild" in calls:
        failures.append("a plain window -> window switch still rebuilt the pipeline")
    if "rnsz" not in calls:
        failures.append("the live switch did not resize the feature to the new window")
    if st.window_hwnd != NEW:
        failures.append("after the live switch the old window is still the processed one")
    if (st.width, st.height) != SIZE or st.follow_size != SIZE:
        failures.append(f"the pipeline size is {(st.width, st.height)}, follow "
                        f"{st.follow_size} - not the new window's")
    if "veil down" not in st.display.calls:
        failures.append("the veil the switch raised was left up")

    for label, st, kwargs in (
            ("from the desktop", _state(window=None), {}),
            ("to another monitor", _state(), {"same_monitor": False}),
            ("while recording", _state(recorder=object()), {}),
            ("with NR off", _state(off=True), {}),
            ("when RNSZ fails", _state(), {"rack_raises": True})):
        calls = _run(st, **kwargs)
        if "teardown" not in calls or "rebuild" not in calls:
            failures.append(f"{label}: the switch did not take the full rebuild ({calls})")
        if st.window_hwnd != NEW:
            failures.append(f"{label}: the new window is not the processed one")
    print("    the full rebuild stays for: the desktop, another monitor, a recording, "
          "NR off, a failed RNSZ")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: another window on the same monitor is switched to live; everything "
          "else still rebuilds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
