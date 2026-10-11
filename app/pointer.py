"""The pointer in a CPU recording (#131), drawn the way the worker draws it
into a GPU recording (native/cursor.inl).

The capture never holds the pointer - the overlay shows the real one over the
processed picture - so a recorded frame has none unless it is drawn on. The
shape is the system pointer's own (any size, colour or monochrome), read once
per HCURSOR: DrawIconEx over black and over white gives the colour and the
coverage at once; inverting pixels come out white. A hidden pointer (a game
that captures the mouse) is not drawn.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

import numpy as np

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", ctypes.c_void_p)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
user32.GetIconInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ICONINFO)]
user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HANDLE,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.HBRUSH,
                              wintypes.UINT]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE,
                                   wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetStockObject.restype = wintypes.HGDIOBJ

_cache: dict = {}   # HCURSOR -> (premultiplied RGBA float32 h x w x 4, hot_x, hot_y)


def _shape(hcursor: int):
    hit = _cache.get(hcursor)
    if hit is not None:
        return hit
    ii = ICONINFO()
    if not user32.GetIconInfo(hcursor, ctypes.byref(ii)):
        return None
    bm = BITMAP()
    w = h = 0
    if ii.hbmColor and gdi32.GetObjectW(ii.hbmColor, ctypes.sizeof(bm), ctypes.byref(bm)):
        w, h = bm.bmWidth, bm.bmHeight
    elif ii.hbmMask and gdi32.GetObjectW(ii.hbmMask, ctypes.sizeof(bm), ctypes.byref(bm)):
        w, h = bm.bmWidth, bm.bmHeight // 2
    for b in (ii.hbmColor, ii.hbmMask):
        if b:
            gdi32.DeleteObject(b)
    if w <= 0 or h <= 0:
        return None
    w, h = min(w, 256), min(h, 256)
    bih = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    planes = []
    try:
        for brush in (4, 0):          # BLACK_BRUSH, WHITE_BRUSH
            bits = ctypes.c_void_p()
            dib = gdi32.CreateDIBSection(screen, ctypes.byref(bih), 0, ctypes.byref(bits), None, 0)
            if not dib:
                return None
            old = gdi32.SelectObject(dc, dib)
            r = wintypes.RECT(0, 0, w, h)
            user32.FillRect(dc, ctypes.byref(r), gdi32.GetStockObject(brush))
            ok = user32.DrawIconEx(dc, 0, 0, hcursor, w, h, 0, None, 3)   # DI_NORMAL
            gdi32.GdiFlush()
            buf = np.ctypeslib.as_array(ctypes.cast(bits, ctypes.POINTER(ctypes.c_uint8)),
                                        shape=(h, w, 4)).astype(np.int32)
            gdi32.SelectObject(dc, old)
            gdi32.DeleteObject(dib)
            if not ok:
                return None
            planes.append(buf)
    finally:
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(None, screen)
    black, white = planes
    cover = np.clip(255 - (white[..., :3] - black[..., :3]).max(-1), 0, 255)
    invert = black[..., 0] > white[..., 0]
    rgba = np.empty((h, w, 4), np.float32)
    rgba[..., 0] = np.where(invert, 255, black[..., 2])
    rgba[..., 1] = np.where(invert, 255, black[..., 1])
    rgba[..., 2] = np.where(invert, 255, black[..., 0])
    rgba[..., 3] = np.where(invert, 255, cover)
    rgba /= 255.0
    hit = (rgba, int(ii.xHotspot), int(ii.yHotspot))
    if len(_cache) > 32:
        _cache.clear()
    _cache[hcursor] = hit
    return hit


def draw_pointer(frame: np.ndarray, area: tuple) -> bool:
    """Draw the pointer onto `frame` (H x W x 3/4 uint8, RGB order first) when
    it is visible over `area` - (left, top, width, height) of the captured
    picture on the screen. Returns whether it drew."""
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(ci)
    if not user32.GetCursorInfo(ctypes.byref(ci)) or not (ci.flags & 1) or not ci.hCursor:
        return False
    left, top, aw, ah = area
    if aw <= 0 or ah <= 0:
        return False
    shape = _shape(int(ci.hCursor))
    if shape is None:
        return False
    rgba, hx, hy = shape
    fh, fw = frame.shape[:2]
    scale = fw / float(aw)
    if abs(scale - 1.0) > 1e-3:
        import cv2
        rgba = cv2.resize(rgba, (max(1, round(rgba.shape[1] * scale)),
                                 max(1, round(rgba.shape[0] * scale))),
                          interpolation=cv2.INTER_LINEAR)
    x = int((ci.ptScreenPos.x - hx - left) * scale)
    y = int((ci.ptScreenPos.y - hy - top) * scale)
    ph, pw = rgba.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(fw, x + pw), min(fh, y + ph)
    if x0 >= x1 or y0 >= y1:
        return False
    src = rgba[y0 - y:y1 - y, x0 - x:x1 - x]
    dst = frame[y0:y1, x0:x1, :3].astype(np.float32)
    a = src[..., 3:4]
    frame[y0:y1, x0:x1, :3] = np.clip(src[..., :3] * 255.0 + dst * (1.0 - a) + 0.5,
                                      0, 255).astype(np.uint8)
    return True
