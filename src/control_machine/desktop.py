"""Capture and drive this Mac's GUI session."""

from __future__ import annotations

import ctypes
import subprocess
import tempfile
import time
from io import BytesIO
from pathlib import Path

from PIL import Image

_KEY_CODES = {
    "return": 36,
    "enter": 36,
    "tab": 48,
    "escape": 53,
    "esc": 53,
    "space": 49,
    "delete": 51,
    "backspace": 51,
}

_K_MOUSE_MOVED = 5
_K_LEFT_DOWN = 1
_K_LEFT_UP = 2
_K_MOUSE_LEFT = 0
_K_HID = 0
_K_SOURCE_HID = 1
_K_MOUSE_CLICK_STATE = 1
_K_FLAG_CONTROL = 0x00040000
_K_CONTROL = 59
_K_UP = 126
_K_F2 = 120
_K_F3 = 99

_last_image_size: tuple[int, int] | None = None
_last_desktop_origin = (0.0, 0.0)
_last_desktop_size = (0.0, 0.0)
_apps_open = False
_menu_open = False


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


class DesktopError(RuntimeError):
    """Screen capture or input failed (usually a Privacy permission)."""


def capture_jpeg(*, page: int = 1, max_width: int = 1280, quality: int = 55) -> tuple[bytes, int, int]:
    """Grab one display (page 1, page 2, …) as a JPEG. Returns (jpeg, width, height)."""
    global _last_image_size, _last_desktop_origin, _last_desktop_size
    index, origin_x, origin_y, logical_w, logical_h = _select_display(page)
    shot = _capture_display(index)
    if shot is None:
        raise DesktopError(
            "Could not capture the screen. Grant Screen Recording to the process "
            "running control-machine in System Settings → Privacy & Security."
        )
    image = shot
    if image.width > max_width:
        height = round(image.height * max_width / image.width)
        image = image.resize((max_width, max(1, height)), Image.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    _last_image_size = (image.width, image.height)
    _last_desktop_origin = (origin_x, origin_y)
    _last_desktop_size = (logical_w, logical_h)
    return buffer.getvalue(), image.width, image.height


def display_count() -> int:
    return max(1, len(_display_layout()))


def _select_display(page: int) -> tuple[int, float, float, float, float]:
    displays = _display_layout()
    if not displays:
        raise DesktopError("No displays available to capture.")
    index = min(max(int(page), 1), len(displays)) - 1
    return displays[index]


def click_at(x: int, y: int, *, image_width: int | None = None, image_height: int | None = None) -> None:
    """Click in screenshot-image coordinates, mapped onto the real display."""
    _require_accessibility()
    width, height = image_width, image_height
    if not (width and height) and _last_image_size:
        width, height = _last_image_size
    px, py = float(x), float(y)
    origin_x, origin_y = _last_desktop_origin
    logical_w, logical_h = _last_desktop_size
    if not (logical_w and logical_h):
        origin_x, origin_y, logical_w, logical_h = _desktop_frame()
    if width and height and logical_w and logical_h:
        px = origin_x + px / width * logical_w
        py = origin_y + py / height * logical_h
    try:
        _cg_click(px, py)
    except OSError as exc:
        raise DesktopError(
            "Click failed. Grant Accessibility to the process running control-machine "
            f"(Terminal, iTerm, or Cursor) in System Settings → Privacy & Security → Accessibility. ({exc})"
        ) from exc


def type_text(text: str) -> None:
    if not text:
        return
    _require_accessibility()
    try:
        _cg_type(text)
    except OSError as exc:
        raise DesktopError(
            "Type failed. Grant Accessibility to the process running control-machine "
            "in System Settings → Privacy & Security → Accessibility."
        ) from exc


def press_key(key: str) -> None:
    code = _KEY_CODES.get(key.strip().lower())
    if code is None:
        raise DesktopError(f"Unsupported key {key!r}.")
    _require_accessibility()
    try:
        _cg_key_code(code)
    except OSError as exc:
        raise DesktopError(f"Key failed ({exc}).") from exc


def show_apps() -> None:
    """Toggle Mission Control so every open window is visible."""
    global _apps_open
    _require_accessibility()
    if _apps_open:
        _press_escape()
        _apps_open = False
        return
    result = subprocess.run(
        ["open", "-a", "Mission Control"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        result = subprocess.run(
            ["open", "/System/Applications/Mission Control.app"],
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        result = _osascript('tell application "System Events" to key code 126 using control down')
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "open failed").strip()
        raise DesktopError(f"Could not open Mission Control ({err}).")
    _apps_open = True


def show_menu(*, page: int = 1) -> None:
    """Toggle the Apple menu on the display currently shown in the live view."""
    global _menu_open
    _require_accessibility()
    if _menu_open:
        _press_escape()
        _menu_open = False
        return
    _, origin_x, origin_y, _, _ = _select_display(page)
    try:
        _cg_click(origin_x + 22.0, origin_y + 11.0)
    except OSError as exc:
        script = (
            'tell application "System Events" to tell '
            '(first application process whose frontmost is true) to '
            "click menu bar item 1 of menu bar 1"
        )
        result = _osascript(script)
        if result.returncode != 0:
            raise DesktopError(f"Could not open the menu bar ({exc}).") from exc
    _menu_open = True


def show_dock() -> None:
    """Move keyboard focus to the Dock (reveals it if hidden)."""
    _require_accessibility()
    try:
        _cg_hotkey(_K_F3, _K_FLAG_CONTROL)
    except OSError as exc:
        raise DesktopError(f"Could not open the Dock ({exc}).") from exc


def input_is_trusted() -> bool:
    return _ax_trusted()


def _cg() -> ctypes.CDLL:
    return ctypes.CDLL(
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    )


def _cf() -> ctypes.CDLL:
    return ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )


def _ax_trusted() -> bool:
    try:
        app = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        app.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(app.AXIsProcessTrusted())
    except OSError:
        return False


def _require_accessibility() -> None:
    if _ax_trusted():
        return
    raise DesktopError(
        "This Mac is visible, but clicks and typing are blocked. Grant Accessibility "
        "to the app running control-machine (Terminal, iTerm, or Cursor) in "
        "System Settings → Privacy & Security → Accessibility, then restart control-machine."
    )


def _display_layout() -> list[tuple[int, float, float, float, float]]:
    """Return (screencapture -D index, origin_x, origin_y, width, height) in points."""
    cg = _cg()
    cg.CGGetActiveDisplayList.restype = ctypes.c_int32
    cg.CGGetActiveDisplayList.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    cg.CGDisplayBounds.restype = CGRect
    cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
    maximum = 16
    ids = (ctypes.c_uint32 * maximum)()
    count = ctypes.c_uint32()
    if cg.CGGetActiveDisplayList(maximum, ids, ctypes.byref(count)) != 0 or count.value < 1:
        width, height = _logical_screen_size()
        return [(1, 0.0, 0.0, width, height)]
    layout: list[tuple[int, float, float, float, float]] = []
    for index in range(count.value):
        bounds = cg.CGDisplayBounds(ids[index])
        layout.append(
            (
                index + 1,
                float(bounds.origin.x),
                float(bounds.origin.y),
                float(bounds.size.width),
                float(bounds.size.height),
            )
        )
    return layout


def _desktop_frame() -> tuple[float, float, float, float]:
    displays = _display_layout()
    origin_x = min(item[1] for item in displays)
    origin_y = min(item[2] for item in displays)
    max_x = max(item[1] + item[3] for item in displays)
    max_y = max(item[2] + item[4] for item in displays)
    return origin_x, origin_y, max_x - origin_x, max_y - origin_y


def _logical_screen_size() -> tuple[float, float]:
    cg = _cg()
    cg.CGMainDisplayID.restype = ctypes.c_uint32
    cg.CGDisplayBounds.restype = CGRect
    cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
    bounds = cg.CGDisplayBounds(cg.CGMainDisplayID())
    width = float(bounds.size.width)
    height = float(bounds.size.height)
    if width > 1 and height > 1:
        return width, height
    cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
    cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
    display = cg.CGMainDisplayID()
    return float(cg.CGDisplayPixelsWide(display)), float(cg.CGDisplayPixelsHigh(display))


def _press_escape() -> None:
    try:
        _cg_key_code(_KEY_CODES["escape"])
    except OSError:
        _osascript('tell application "System Events" to key code 53')


def _osascript(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["osascript", "-e", source],
        capture_output=True,
        text=True,
        check=False,
    )


def _capture_display(index: int) -> Image.Image | None:
    handle = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    handle.close()
    dest = Path(handle.name)
    try:
        result = subprocess.run(
            ["screencapture", "-x", "-D", str(index), "-t", "jpg", str(dest)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
            return None
        return Image.open(dest).convert("RGB")
    finally:
        dest.unlink(missing_ok=True)


def _cg_hotkey(key_code: int, flags: int) -> None:
    cg = _cg()
    cf = _cf()
    cg.CGEventSourceCreate.restype = ctypes.c_void_p
    cg.CGEventSourceCreate.argtypes = [ctypes.c_int32]
    cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    source = cg.CGEventSourceCreate(_K_SOURCE_HID)
    control = cg.CGEventCreateKeyboardEvent(source, _K_CONTROL, True)
    if control:
        cg.CGEventPost(_K_HID, control)
        cf.CFRelease(control)
    time.sleep(0.02)
    for down in (True, False):
        event = cg.CGEventCreateKeyboardEvent(source, int(key_code), down)
        if not event:
            raise OSError("CGEventCreateKeyboardEvent returned null")
        cg.CGEventSetFlags(event, flags)
        cg.CGEventPost(_K_HID, event)
        cf.CFRelease(event)
        time.sleep(0.03)
    control_up = cg.CGEventCreateKeyboardEvent(source, _K_CONTROL, False)
    if control_up:
        cg.CGEventPost(_K_HID, control_up)
        cf.CFRelease(control_up)
    if source:
        cf.CFRelease(source)


def _cg_click(x: float, y: float) -> None:
    cg = _cg()
    cf = _cf()
    cg.CGEventSourceCreate.restype = ctypes.c_void_p
    cg.CGEventSourceCreate.argtypes = [ctypes.c_int32]
    cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    cg.CGEventCreateMouseEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        CGPoint,
        ctypes.c_uint32,
    ]
    cg.CGEventSetIntegerValueField.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int64,
    ]
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cg.CGWarpMouseCursorPosition.argtypes = [CGPoint]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    point = CGPoint(x, y)
    source = cg.CGEventSourceCreate(_K_SOURCE_HID)
    cg.CGWarpMouseCursorPosition(point)
    for kind in (_K_MOUSE_MOVED, _K_LEFT_DOWN, _K_LEFT_UP):
        event = cg.CGEventCreateMouseEvent(source, kind, point, _K_MOUSE_LEFT)
        if not event:
            raise OSError("CGEventCreateMouseEvent returned null")
        if kind in (_K_LEFT_DOWN, _K_LEFT_UP):
            cg.CGEventSetIntegerValueField(event, _K_MOUSE_CLICK_STATE, 1)
        cg.CGEventPost(_K_HID, event)
        cf.CFRelease(event)
        if kind == _K_LEFT_DOWN:
            time.sleep(0.03)
    if source:
        cf.CFRelease(source)


def _cg_type(text: str) -> None:
    cg = _cg()
    cf = _cf()
    cg.CGEventSourceCreate.restype = ctypes.c_void_p
    cg.CGEventSourceCreate.argtypes = [ctypes.c_int32]
    cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    cg.CGEventKeyboardSetUnicodeString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_uint16),
    ]
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    source = cg.CGEventSourceCreate(_K_SOURCE_HID)
    for char in text:
        codes = (ctypes.c_uint16 * 1)(ord(char) & 0xFFFF)
        for down in (True, False):
            event = cg.CGEventCreateKeyboardEvent(source, 0, down)
            if not event:
                raise OSError("CGEventCreateKeyboardEvent returned null")
            cg.CGEventKeyboardSetUnicodeString(event, 1, codes)
            cg.CGEventPost(_K_HID, event)
            cf.CFRelease(event)
        time.sleep(0.01)
    if source:
        cf.CFRelease(source)


def _cg_key_code(code: int) -> None:
    cg = _cg()
    cf = _cf()
    cg.CGEventSourceCreate.restype = ctypes.c_void_p
    cg.CGEventSourceCreate.argtypes = [ctypes.c_int32]
    cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    source = cg.CGEventSourceCreate(_K_SOURCE_HID)
    for down in (True, False):
        event = cg.CGEventCreateKeyboardEvent(source, int(code), down)
        if not event:
            raise OSError("CGEventCreateKeyboardEvent returned null")
        cg.CGEventPost(_K_HID, event)
        cf.CFRelease(event)
        if down:
            time.sleep(0.03)
    if source:
        cf.CFRelease(source)
