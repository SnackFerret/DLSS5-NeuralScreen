r"""NVOFA keeps the motion of a slowly moving, softly shaded face (#160).

The zero-motion test (native/nvofa.inl, kNvofaExpand) keeps a vector only
where it explains the pixel's 3x3 neighbourhood better than no motion. With an
absolute margin of one grey level per pixel it also took the vectors away from
soft shading that moves slowly - a face's shadow side changes by less than a
level a pixel per frame - so the network and the stabilizer treated the face
as still and its shadows trailed. Where the picture did change, a vector that
explains most of the change is now kept; a still background still gets none.

Checked on the worker with NVOFA and a captured window: a softly shaded disc
(a slow gradient, almost no texture) moves 2 px a frame over a smooth still
background. In the motion fields the worker builds, most of the disc carries
motion pointing the right way, and the still background far from it carries
(almost) none. Nothing shows: the target is a ghost window and the overlay is
off every monitor. Needs an RTX GPU with the Optical Flow Accelerator.

Run:  runtime\python.exe tests\test_nvofa_soft_motion.py
"""
import ctypes
import os
import shutil
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

import offscreen_target as ot  # noqa: E402
from paths import WORKER_EXE  # noqa: E402

W, H = 640, 360
GW, GH = 320, 180
R = 70              # the disc's radius
STEP = 2            # px per paint, along x
Y0 = 180            # the disc's centre row
X_START = 140


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def _background() -> np.ndarray:
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    return 105.0 + 20.0 * np.sin(xs / 97.0) + 12.0 * np.cos(ys / 61.0)


def _disc(cx: float) -> tuple:
    """(shade, coverage) of the disc centred at (cx, Y0): lit from the left,
    its shadow side a slow gradient."""
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    nx, ny = (xs - cx) / R, (ys - Y0) / R
    r = np.sqrt(nx * nx + ny * ny)
    nz = np.sqrt(np.clip(1.0 - r * r, 0.0, 1.0))
    lam = np.clip(-0.6 * nx - 0.3 * ny + 0.74 * nz, 0.0, 1.0)
    return 80.0 + 90.0 * lam, np.clip((1.0 - r) * R + 0.5, 0.0, 1.0)


class SoftDisc(ot.Target):
    """A ghost target: a smooth still background, a soft disc moving right."""

    def __init__(self):
        self.x = X_START
        self._bg = _background()
        super().__init__(W, H, name="NsSoftMotion", ghost=True)

    def _paint(self):
        shade, a = _disc(self.x)
        grey = self._bg * (1 - a) + shade * a
        bgra = np.empty((H, W, 4), np.uint8)
        bgra[..., 0] = np.clip(grey * 1.05 + 0.5, 0, 255)
        bgra[..., 1] = np.clip(grey * 0.97 + 0.5, 0, 255)
        bgra[..., 2] = np.clip(grey * 0.90 + 0.5, 0, 255)
        bgra[..., 3] = 255
        bih = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), W, -H, 1, 32, 0,
                               0, 0, 0, 0, 0)
        dc = ot.user32.GetDC(self.hwnd)
        ot.gdi32.SetDIBitsToDevice(dc, 0, 0, W, H, 0, 0, 0, H,
                                   bgra.ctypes.data_as(ctypes.c_void_p),
                                   ctypes.byref(bih), 0)
        ot.user32.ReleaseDC(self.hwnd, dc)
        if self.animate:
            self.x = X_START + (self.x - X_START + STEP) % 360


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    ot.gdi32.SetDIBitsToDevice.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.UINT]
    dump = Path(tempfile.mkdtemp(prefix="ns-nvofa-soft-"))
    os.environ["NS_WINDOW_POS"] = "-30000,-30000"
    os.environ["NS_NR_SMALL"] = "0"
    os.environ["NS_HDR"] = "0"
    os.environ["NS_MOTION_BACKEND"] = "nvofa"
    os.environ["NS_NVOFA_DUMP"] = str(dump)
    from pipeline import shutdown_worker, start_worker
    from protocol import (SharedFrameBuffer, send_frame, send_gray,
                          send_motion_size, send_wgc, send_window)
    from settings_io import PROFILES

    failures: list = []
    target = SoftDisc()
    shm = SharedFrameBuffer(W, H)
    worker, logs, reader, stop = start_worker(dict(PROFILES["Natural"]), W, H, 2,
                                              0, 0, shm)
    for key in ("NS_MOTION_BACKEND", "NS_NVOFA_DUMP"):
        os.environ.pop(key, None)
    motion = np.zeros((GH, GW, 2), np.float16)
    try:
        send_wgc(worker, target.hwnd)
        reader.wait_wgak(15)
        shm.open_gray(GW, GH)
        send_gray(worker, GW, GH, shm.gray_name)
        reader.wait_gak(10)
        send_motion_size(worker, GW, GH)
        reader.wait_mack(10)
        send_window(worker, W, H)
        reader.wait_wack(10)
        target.animate_interval = 1.0 / 30.0
        target.animate = True
        for i in range(70):
            send_frame(worker, i, None, motion, i == 0, i, no_color=True, motion_small=True)
            reader.recv(i, timeout=10.0)
    except Exception as exc:
        failures.append(f"the run raised {type(exc).__name__}: {exc}")
    finally:
        target.animate = False
        shutdown_worker(worker, stop)
        shm.close()
        target.close()

    if not any("[nvofa] active" in ln for ln in logs):
        if any("[nvofa] unavailable" in ln for ln in logs) and not failures:
            print("SKIP: NVOFA did not run here (no Optical Flow Accelerator)")
            shutil.rmtree(dump, ignore_errors=True)
            return 0
        failures.append("NVOFA never became active")
    flows = {p.name.split("-")[1] for p in dump.glob("flow-*.bin")}
    fields = [p for p in sorted(dump.glob("motion-*.bin"))
              if p.name.split("-")[1] in flows][10:]      # measured fields, warm
    disc_right = disc_total = far_moving = far_total = 0
    for path in fields:
        field = np.fromfile(path, np.float16).astype(np.float32)
        if field.size != W * H * 2:
            continue
        field = field.reshape(H, W, 2)
        moving = np.hypot(field[..., 0], field[..., 1]) > 0
        # The disc's rows, inner band; its columns are where that band moves
        # most - or, if nothing moves, nowhere (counted as lost).
        band = moving[Y0 - R // 2:Y0 + R // 2]
        cols = np.where(band.mean(axis=0) > 0.3)[0]
        rows = slice(Y0 - R // 2, Y0 + R // 2)
        if cols.size:
            x0, x1 = int(cols.min()), int(cols.max())
            inner = field[rows, x0:x1 + 1, 0]
            disc_total += inner.size
            disc_right += int((inner < -0.5).sum())     # current -> previous: from the left
        else:
            disc_total += R * R
        far = np.zeros((H, W), bool)
        far[:Y0 - R - 30, :] = True
        far[Y0 + R + 30:, :] = True
        far_total += int(far.sum())
        far_moving += int((moving & far).sum())
    shutil.rmtree(dump, ignore_errors=True)
    if not fields:
        failures.append("the worker dumped no measured motion fields")
    else:
        disc_frac = disc_right / max(1, disc_total)
        far_frac = far_moving / max(1, far_total)
        print(f"    {len(fields)} fields: soft disc with the right motion "
              f"{100 * disc_frac:.1f}%, still background with motion {100 * far_frac:.2f}%")
        if disc_frac < 0.5:
            failures.append(f"the slowly moving soft disc lost its motion "
                            f"({100 * disc_frac:.1f}% of it points the right way)")
        if far_frac > 0.02:
            failures.append(f"{100 * far_frac:.2f}% of the still background carried motion")
    for f in failures:
        print("FAIL:", f)
    if failures:
        print("worker log (tail):", *logs[-8:], sep="\n  ")
        return 1
    print("OK: a slowly moving soft face keeps its motion; the still background gets none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
