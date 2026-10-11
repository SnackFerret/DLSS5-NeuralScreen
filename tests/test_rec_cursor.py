r"""A GPU recording shows the pointer where it was (#131).

The capture never holds the pointer - Desktop Duplication hands it over
apart from the picture and window capture runs with it off, because the
overlay already shows the real pointer over the processed picture. So the
recordings had none. The worker now draws the system pointer's own shape on
the copy that goes to the encoder (native/cursor.inl), and nowhere else.

Checked on the real worker with a captured window: a flat grey ghost window
in the primary monitor's corner, the pointer moved over its middle, a second
of GPU recording. In the file, the patch under the pointer holds pixels far
from the grey (the arrow's outline and fill) - and with the pointer pass
switched off for the run (NS_TEST_FAIL_STAGE=no-rec-cursor) the same patch is
grey like the rest. The pointer is put back where it was.

The CPU recording (the fallback when the GPU one cannot run) draws it in the
client (app/pointer.py): over a grey frame covering the screen's corner the
pixels under the pointer change, and over an area the pointer is not in
nothing does.

Run:  runtime\python.exe tests\test_rec_cursor.py
"""
import ctypes
import os
import shutil
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import av  # noqa: E402
import numpy as np  # noqa: E402

import offscreen_target as ot  # noqa: E402
from paths import WORKER_EXE  # noqa: E402

W, H = 640, 360
GW, GH = 160, 90
GREY = 0x808080


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]


def _pointer_showing() -> bool:
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(ci)
    return bool(ctypes.windll.user32.GetCursorInfo(ctypes.byref(ci))) and bool(ci.flags & 1)


def _record(folder: Path, name: str, fail_stage: str = "") -> Path:
    from pipeline import shutdown_worker, start_worker
    from protocol import send_frame, send_motion_size, send_wgc
    from recorder import GpuRecorder
    from settings_io import PROFILES

    target = ot.Target(W, H, name=f"NsRecCursor{name}", ghost=True)
    target.set_bars((GREY, GREY, GREY, GREY))
    if fail_stage:
        os.environ["NS_TEST_FAIL_STAGE"] = fail_stage
    try:
        worker, logs, reader, stop = start_worker(dict(PROFILES["Natural"]), W, H, 2, 0, 0, None)
    finally:
        os.environ.pop("NS_TEST_FAIL_STAGE", None)
    path = folder / f"{name}.mp4"
    try:
        send_wgc(worker, target.hwnd)
        reader.wait_wgak(15)
        send_motion_size(worker, GW, GH)
        reader.wait_mack(10)
        motion = np.zeros((GH, GW, 2), np.float16)
        send_frame(worker, 0, None, motion, True, 0, no_color=True, motion_small=True)
        reader.recv(0, timeout=60.0)
        target.animate_interval = 1.0 / 30.0
        target.animate = True
        rec = GpuRecorder(worker, reader, str(path), fps=30, audio=False)
        t0 = time.perf_counter()
        i = 1
        while time.perf_counter() - t0 < 1.2:
            send_frame(worker, i, None, motion, False, i, no_color=True, motion_small=True)
            reader.recv(i, timeout=10.0)
            i += 1
        rec.finish()
        rec.wait(40.0)
    finally:
        target.animate = False
        shutdown_worker(worker, stop)
        target.close()
    return path


def _patch_spread(path: Path, x: int, y: int) -> tuple:
    """(max distance from the frame's median in the patch under the pointer,
    the same in a patch far from it), over the middle frames."""
    near, far = [], []
    with av.open(str(path)) as c:
        frames = [f.to_ndarray(format="rgb24").astype(int) for f in c.decode(video=0)]
    for rgb in frames[len(frames) // 3: 2 * len(frames) // 3 + 1]:
        med = np.median(rgb.reshape(-1, 3), axis=0)
        near.append(np.abs(rgb[y:y + 24, x:x + 16] - med).max())
        far.append(np.abs(rgb[y + 120:y + 144, x - 200:x - 184] - med).max())
    return (max(near) if near else 0), (max(far) if far else 0)


def _cpu_path(failures: list, px: int, py: int) -> None:
    import pointer
    frame = np.full((H, W, 4), 128, np.uint8)
    drew = pointer.draw_pointer(frame, (0, 0, W, H))
    spread = int(np.abs(frame[py:py + 24, px:px + 16, :3].astype(int) - 128).max())
    print(f"    CPU recording: drawn={drew}, {spread} codes from the grey under the pointer")
    if not drew or spread < 80:
        failures.append(f"the CPU recording path drew no pointer (drawn={drew}, {spread})")
    elsewhere = np.full((H, W, 4), 128, np.uint8)
    if pointer.draw_pointer(elsewhere, (W * 4, H * 4, W, H)) or (elsewhere != 128).any():
        failures.append("the CPU path drew a pointer that is not over the captured area")


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    if not _pointer_showing():
        print("SKIP: no visible pointer on this desktop")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    os.environ["NS_WINDOW_POS"] = "-30000,-30000"
    os.environ["NS_HDR"] = "0"
    failures = []
    saved = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(saved))
    folder = Path(tempfile.mkdtemp(prefix="ns-rec-cursor-"))
    px, py = W // 2, H // 2
    try:
        ctypes.windll.user32.SetCursorPos(px, py)
        _cpu_path(failures, px, py)
        with_pointer = _record(folder, "on")
        ctypes.windll.user32.SetCursorPos(px, py)
        without = _record(folder, "off", fail_stage="no-rec-cursor")
        on_near, on_far = _patch_spread(with_pointer, px, py)
        off_near, off_far = _patch_spread(without, px, py)
        print(f"    under the pointer: {on_near} codes from the grey with the pass, "
              f"{off_near} without; far from it {on_far} / {off_far}")
        if on_near < 80:
            failures.append(f"the recording shows no pointer ({on_near} codes from the grey "
                            f"under it)")
        if off_near > 40:
            failures.append(f"without the pointer pass the patch is not grey ({off_near}) - "
                            f"the check proves nothing")
        if on_far > 40:
            failures.append(f"the pass changed the picture away from the pointer ({on_far})")
    except Exception as exc:
        failures.append(f"the run raised {type(exc).__name__}: {exc}")
    finally:
        ctypes.windll.user32.SetCursorPos(saved.x, saved.y)
        shutil.rmtree(folder, ignore_errors=True)
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a GPU recording carries the pointer, drawn where it was")
    return 0


if __name__ == "__main__":
    sys.exit(main())
