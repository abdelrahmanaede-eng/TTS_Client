"""Tkinter dialogs for TimeTracker.

Runs on a dedicated single UI thread with a thread-safe message queue.
All dialogs are instantiated as Toplevel windows off a hidden master root,
ensuring 100% thread safety without random Tcl crashes or freezing.

Provides:
- Transparency & Privacy Consent Dialog (first run)
- Settings Dialog (working hours, work days, holidays, reminders, auto-backup/export, app tracking)
- Retroactive Past Meeting Dialog (add missed meetings)
- Meeting End Note Dialog (optional note for meetings)
- Overtime Prompt with Reason Selection (Project, Emergency, Compensation, General)
- Pause Tracking Dialog (temporary personal use)
- Custom Date Range Export Dialog
- About Dialog (Bussma branding)
"""

from __future__ import annotations

import ctypes
import logging
import queue
import re
import sys
import threading
import time
import uuid
import tkinter as tk
from tkinter import messagebox, ttk
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from time_tracker.core.config import Config
from time_tracker.localization.i18n import t
from time_tracker.system.updater import safe_version_slug

logger = logging.getLogger(__name__)

# Single dedicated UI Thread and Message Queue
_ui_queue: queue.Queue = queue.Queue()
_ui_thread: threading.Thread | None = None
_ui_root: tk.Tk | None = None
_ui_lock = threading.Lock()
_root_ready = threading.Event()
_tk_available: bool = True

# Marks a Canvas created by make_scrollable so the wheel dispatcher can find it.
_SCROLL_CANVAS_ATTR = "_tt_scroll_canvas"
# Cached scrollbar pack side; apply_rtl mirrors it so _sync can re-pack correctly.
_SCROLLBAR_SIDE_ATTR = "_tt_scrollbar_side"

MB_OK = 0x00000000
MB_OKCANCEL = 0x00000001
MB_YESNO = 0x00000004
MB_ICONERROR = 0x00000010
MB_ICONQUESTION = 0x00000020
MB_ICONWARNING = 0x00000030
MB_ICONINFORMATION = 0x00000040
MB_TOPMOST = 0x00040000
MB_SETFOREGROUND = 0x00010000

IDOK = 1
IDCANCEL = 2
IDYES = 6
IDNO = 7


def win32_message_box(title: str, text: str, flags: int = 0) -> int:
    """Display native Windows MessageBoxW directly via Win32 API.
    
    Serves as a reliable fallback when Tcl/Tk is corrupted or missing on
    stripped/LTSC/WinPE Windows installations, with error details logged
    in timetracker.log.
    """
    if sys.platform == "win32":
        try:
            full_flags = flags | MB_TOPMOST | MB_SETFOREGROUND
            return int(ctypes.windll.user32.MessageBoxW(0, str(text), str(title), full_flags))
        except Exception as exc:
            logger.error("Win32 MessageBoxW fallback failed: %s", exc, exc_info=True)
    logger.info("[UI FALLBACK] %s: %s", title, text)
    return IDOK


def is_tkinter_available() -> bool:
    """Check if Tkinter is initialized and operational."""
    return _tk_available


_dialog_lock = threading.RLock()
_is_dialog_active = False
# Re-entrant: a dialog claims the flag and then refreshes its own heartbeat, both
# of which take this lock (a plain Lock deadlocks on the second acquire).
# Watchdog: the flag above must be refreshed by the dialog that owns it.  If
# the UI thread dies between setting the flag and running the dialog (or the
# process crashes mid-dialog), the flag would otherwise stay True forever and
# silently block every future popup.  Heartbeats arrive every 5s at worst, so
# a 60s silence means the owner is gone.
_dialog_heartbeat_ts: float = 0.0
_DIALOG_STALE_SECONDS = 60.0


def _touch_dialog_heartbeat() -> None:
    global _dialog_heartbeat_ts
    with _dialog_lock:
        _dialog_heartbeat_ts = time.monotonic()


def _dispatch_mousewheel(event: Any) -> str | None:
    """Scroll the scrollable panel under the pointer (BUG-06 fix).

    Tk does not bubble events to ancestor widgets, so one handler is installed
    with ``add="+"`` (never ``bind_all`` per panel, never ``unbind_all``) and
    walks up from the event widget to find the nearest scroll wrapper.  It is
    behaviourally scoped: widgets outside a registered panel, and native-
    scrolling widgets (lists, text, spinboxes, trees, combos), are left alone.
    """
    widget = event.widget
    if isinstance(widget, (tk.Listbox, tk.Text, tk.Spinbox, ttk.Treeview, ttk.Combobox)):
        return None
    delta = int(getattr(event, "delta", 0) or 0)
    if not delta:
        return None
    steps = -1 if delta > 0 else 1
    ancestor: Any = widget
    while ancestor is not None:
        canvas = getattr(ancestor, _SCROLL_CANVAS_ATTR, None)
        if canvas is not None:
            try:
                canvas.yview_scroll(steps, "units")
            except Exception:
                return None
            return "break"
        ancestor = getattr(ancestor, "master", None)
    return None


def _scroll_fraction_for_focus(
    top: float,
    bottom: float,
    viewport_h: float,
    content_h: float,
    current_top: float,
) -> float | None:
    """New yview fraction to reveal a focused widget, or None if already visible.

    Pure so keyboard-focus scrolling can be tested without a Tk display.
    """
    if viewport_h <= 0 or content_h <= 0:
        return None
    if top >= current_top and bottom <= current_top + viewport_h:
        return None
    visible = min(viewport_h, content_h)
    new_top = top if (bottom - top >= visible or top < current_top) else bottom - visible
    # Clamp to the last full page so yview never asks for an impossible offset.
    max_fraction = max(0.0, 1.0 - visible / content_h)
    return max(0.0, min(max_fraction, new_top / content_h))


def _ui_worker() -> None:
    global _ui_root, _tk_available
    try:
        _ui_root = tk.Tk()
        _ui_root.withdraw()  # Hidden master root window
        # Installed once, add="+" keeps any other wheel handler intact.
        _ui_root.bind_all("<MouseWheel>", _dispatch_mousewheel, add="+")
        _tk_available = True
        _root_ready.set()

        def _poll() -> None:
            try:
                while True:
                    task = _ui_queue.get_nowait()
                    func, args, kwargs = task
                    try:
                        func(*args, **kwargs)
                    except Exception as err:
                        logger.error("Exception in UI task %s: %s", func, err, exc_info=True)
                    finally:
                        _ui_queue.task_done()
            except queue.Empty:
                pass
            if _ui_root:
                _ui_root.after(50, _poll)

        _ui_root.after(50, _poll)
        _ui_root.mainloop()
    except Exception as exc:
        _tk_available = False
        _ui_root = None
        _root_ready.set()
        logger.error(
            "Tcl/Tk UI worker initialization or event loop failed: %s. "
            "Switching to native Windows MessageBoxW / console fallback. "
            "See timetracker.log for full details.",
            exc,
            exc_info=True,
        )
    finally:
        _ui_root = None


def ensure_ui_thread() -> tk.Tk | None:
    """Ensure the dedicated UI thread is running and return the hidden root (or None if Tk unavailable)."""
    global _ui_thread, _tk_available
    if not _tk_available:
        return None
    with _ui_lock:
        if _ui_thread is None or not _ui_thread.is_alive():
            _root_ready.clear()
            _ui_thread = threading.Thread(target=_ui_worker, name="TimeTrackerUIThread", daemon=True)
            _ui_thread.start()
            _root_ready.wait(timeout=5.0)
            if not _tk_available:
                return None
    return _ui_root


def dispatch_to_ui(
    func: Callable[..., Any],
    *args: Any,
    fallback: Callable[[], None] | None = None,
    **kwargs: Any,
) -> None:
    """Queue a callable to run strictly inside the dedicated UI thread.

    BUG-05: when Tk cannot start, a bare worker would raise before firing the
    dialog's terminal callback. Dialogs that have a user-visible outcome pass
    ``fallback``, which is invoked exactly once on the caller thread instead.
    """
    root = ensure_ui_thread()
    if root is None or not _tk_available:
        logger.warning("Tkinter unavailable; using fallback for %s.", getattr(func, "__name__", str(func)))
        if fallback is not None:
            try:
                fallback()
            except Exception as exc:
                logger.error("Dialog fallback failed: %s", exc, exc_info=True)
        else:
            threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True).start()
        return
    _ui_queue.put((func, args, kwargs))


def _unavailable_notice(message: str) -> None:
    """Tell the user (natively) why a dialog could not be shown (BUG-05)."""
    win32_message_box("TimeTracker", message, MB_ICONWARNING)


def stop_ui() -> None:
    """Terminate the dedicated UI thread cleanly."""
    global _ui_root
    if _ui_root:
        _ui_queue.put((_ui_root.quit, (), {}))


def is_dialog_showing() -> bool:
    """Return whether a blocking prompt (like Overtime) is currently active on screen.

    Auto-clears the flag when its owner stopped heartbeating (dead UI thread).
    """
    global _is_dialog_active
    with _dialog_lock:
        if not _is_dialog_active:
            return False
        if time.monotonic() - _dialog_heartbeat_ts < _DIALOG_STALE_SECONDS:
            return True
        _is_dialog_active = False
    logger.warning("Stale dialog flag auto-cleared (owner dialog died without cleanup).")
    return False


# =====================================================================
# Win32 helpers for the always-on-top, non-activating presence popup
# =====================================================================

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
MONITOR_DEFAULTTONEAREST = 2


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", ctypes.c_ulong),
    ]


def setup_dpi_awareness() -> None:
    """Enable per-monitor DPI awareness before any Tk window is created (item 7)."""
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2 (Windows 8.1+)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception as exc:
        logger.debug("Could not enable DPI awareness: %s", exc)


def get_cursor_monitor_workarea() -> tuple[int, int, int, int] | None:
    """Return (left, top, width, height) of the monitor containing the cursor."""
    if sys.platform != "win32":
        return None
    try:
        point = _Point()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return None
        monitor = ctypes.windll.user32.MonitorFromPoint(point, MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if not ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        rect = info.rcWork
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return None
        return (int(rect.left), int(rect.top), width, height)
    except Exception:
        return None


# =====================================================================
# v1.3.2 – shared RTL / DPI helpers (audit items 1 and 2)
# =====================================================================

UI_FONT_CANDIDATES = ("Segoe UI", "Tahoma", "Arial")
_LRM = "\u200e"  # left-to-right mark
_RLM = "\u200f"  # right-to-left mark
_ASCII_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,:%/_\-+()]*[A-Za-z0-9]|[A-Za-z0-9]")
_font_family_cache: str | None = None


def is_rtl(lang: str) -> bool:
    """True for Arabic locales ("ar", "AR", "ar-EG")."""
    return str(lang or "en").strip().lower().startswith("ar")


def rtl_layout(lang: str) -> dict[str, Any]:
    """The layout flags every dialog uses so English stays LTR and Arabic mirrors.

    Dialogs call ``apply_rtl(window, lang)`` which derives the same flags from the
    existing widgets, so both stay in step.
    """
    rtl = is_rtl(lang)
    return {
        "rtl": rtl,
        "anchor": "e" if rtl else "w",
        "justify": "right" if rtl else "left",
        "side": "right" if rtl else "left",
        "direction": _RLM if rtl else _LRM,
    }


def ui_font_family(lang: str = "en") -> str:
    """An Arabic-capable family (Segoe UI on Windows) with a graceful fallback."""
    global _font_family_cache
    if _font_family_cache:
        return _font_family_cache
    available: set[str] = set()
    try:
        from tkinter import font as tkfont

        available = set(tkfont.families())
    except Exception:
        available = set()
    for candidate in UI_FONT_CANDIDATES:
        if candidate in available:
            _font_family_cache = candidate
            return candidate
    _font_family_cache = UI_FONT_CANDIDATES[0]
    return _font_family_cache


def bidi_text(text: str, lang: str) -> str:
    """Wrap Latin words and numbers with LRM so Arabic strings keep their order."""
    if text is None:
        return text  # type: ignore[return-value]
    clean = str(text).replace(_LRM, "").replace(_RLM, "")
    if not is_rtl(lang):
        return clean
    return _ASCII_RUN.sub(lambda m: f"{_LRM}{m.group(0)}{_LRM}", clean)


def selection_layout(kind: str, rtl: bool) -> list[tuple[str, dict[str, Any]]]:
    """Pure ttk layout for a check/radio button with the indicator mirrored."""
    side = "right" if rtl else "left"
    sticky = "e" if rtl else "w"
    return [
        (
            f"{kind}.padding",
            {
                "sticky": "nswe",
                "children": [
                    (f"{kind}.indicator", {"side": side, "sticky": ""}),
                    (
                        f"{kind}.focus",
                        {
                            "side": side,
                            "sticky": sticky,
                            "children": [(f"{kind}.label", {"sticky": "nswe"})],
                        },
                    ),
                ],
            },
        )
    ]


def configure_rtl_widget_styles(lang: str) -> None:
    """Place ttk selection indicators on the right for Arabic (RTL).

    Classic ``tk.Checkbutton``/``Radiobutton`` always draw the indicator on the
    left, which is wrong for Arabic. A ttk layout override with the indicator
    packed on the right fixes it, and is reversible on a live language switch.
    """
    rtl = is_rtl(lang)
    try:
        style = ttk.Style()
        for kind in ("Checkbutton", "Radiobutton"):
            style.layout(f"RTL.T{kind}", selection_layout(kind, rtl))
    except Exception as exc:
        logger.debug("Could not configure RTL selection styles: %s", exc)


def make_selection_widget(
    parent: tk.Misc,
    kind: str,
    *,
    text: str,
    variable: Any,
    lang: str,
    value: Any = None,
    command: Callable[[], None] | None = None,
    font: tuple[Any, ...] | None = None,
    options: dict[str, Any] | None = None,
) -> ttk.Checkbutton | ttk.Radiobutton:
    """Create a check/radio button whose indicator mirrors with the language."""
    configure_rtl_widget_styles(lang)
    style = ttk.Style()
    style.configure(f"RTL.T{kind}", font=font or ("Segoe UI", 9))
    kwargs: dict[str, Any] = {"text": text, "variable": variable, "style": f"RTL.T{kind}"}
    if value is not None:
        kwargs["value"] = value
    if command is not None:
        kwargs["command"] = command
    for key in ("anchor", "justify", "padx", "pady", "state", "width"):
        if options and key in options:
            kwargs[key] = options[key]
    if kind == "Checkbutton":
        return ttk.Checkbutton(parent, **kwargs)
    return ttk.Radiobutton(parent, **kwargs)


def flip_side(side: str, lang: str) -> str:
    """Mirror a pack side for Arabic."""
    if not is_rtl(lang):
        return side
    return {"left": "right", "right": "left"}.get(side, side)


def flip_sticky(sticky: str, lang: str) -> str:
    """Mirror the horizontal part of a grid sticky value."""
    if not is_rtl(lang):
        return sticky
    return sticky.replace("w", "\x00").replace("e", "w").replace("\x00", "e")


def mirror_column(column: int, max_column: int, lang: str) -> int:
    """Mirror a grid column index inside its parent for Arabic."""
    return max_column - column if is_rtl(lang) else column


def compute_dialog_geometry(
    base_w: int,
    base_h: int,
    scale: float = 1.0,
    screen_w: int = 1920,
    screen_h: int = 1080,
) -> tuple[int, int]:
    """Scale a base dialog size for the screen DPI, capped at 90% of the screen."""
    width = max(240, int(round(base_w * scale)))
    height = max(160, int(round(base_h * scale)))
    max_w = max(240, int(screen_w * 0.9))
    max_h = max(160, int(screen_h * 0.9))
    return min(width, max_w), min(height, max_h)


def frame_scale(window: tk.Misc) -> float:
    """Effective DPI scale of the screen the window is on (1.0 at 96 dpi)."""
    try:
        return max(1.0, float(window.winfo_fpixels("1i")) / 96.0)
    except Exception:
        return 1.0


def fit_dialog(window: tk.Misc, base_w: int, base_h: int) -> tuple[int, int]:
    """Size a dialog from its base size + DPI, center it, and cap it to the screen."""
    width, height = compute_dialog_geometry(
        base_w, base_h, frame_scale(window),
        window.winfo_screenwidth(), window.winfo_screenheight(),
    )
    workarea = get_cursor_monitor_workarea()
    if workarea:
        area_x, area_y, area_w, area_h = workarea
        x = area_x + (area_w - width) // 2
        y = area_y + (area_h - height) // 2
    else:
        x = (window.winfo_screenwidth() - width) // 2
        y = (window.winfo_screenheight() - height) // 2
    window.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
    return width, height


def make_scrollable(parent: tk.Misc, bg: str = "#f8f9fa") -> tuple[tk.Frame, tk.Frame]:
    """Canvas + scrollbar wrapper. Returns ``(inner_frame, outer_frame)``.

    ``outer_frame`` is what gets packed or added to a notebook; widgets are built
    into ``inner_frame`` so existing code keeps working unchanged.
    """
    outer = tk.Frame(parent, bg=bg)
    canvas = tk.Canvas(outer, bg=bg, highlightthickness=0, bd=0)
    vbar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    vbar.pack(side="right", fill="y")

    inner = tk.Frame(canvas, bg=bg)
    window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
    setattr(canvas, _SCROLL_CANVAS_ATTR, canvas)
    setattr(canvas, _SCROLLBAR_SIDE_ATTR, "right")

    def _sync(_event: Any = None) -> None:
        bbox = canvas.bbox("all")
        if not bbox:
            return
        canvas.configure(scrollregion=bbox)
        # No reason to show a scrollbar when everything already fits (the
        # Holidays panel is short, yet used to show an empty outer bar).
        fits = (bbox[3] - bbox[1]) <= canvas.winfo_height() + 1
        if fits and vbar.winfo_ismapped():
            vbar.pack_forget()
        elif not fits and not vbar.winfo_ismapped():
            # Never hard-code "right": apply_rtl mirrors the vbar and caches the
            # new side on the canvas, so a heavy RTL panel keeps it on the left.
            vbar.pack(side=getattr(canvas, _SCROLLBAR_SIDE_ATTR, "right"), fill="y")

    def _resize(event: Any) -> None:
        canvas.itemconfigure(window_id, width=event.width)
        _sync()

    def _reveal_focused(event: Any) -> None:
        """Keyboard a11y: scroll a Tab-focused widget into view (Tk sends
        FocusIn up the ancestor chain, so binding on outer/inner is enough)."""
        focused = event.widget
        y = 0
        node: Any = focused
        while node is not None and node is not inner:
            try:
                y += int(node.winfo_y())
            except Exception:
                pass
            node = getattr(node, "master", None)
        if node is not inner:
            return
        try:
            viewport = max(1, int(canvas.winfo_height()))
            box = canvas.bbox("all")
            content = max(1, (box[3] - box[1]) if box else int(inner.winfo_reqheight()))
            current_top = float(canvas.yview()[0]) * content
            fraction = _scroll_fraction_for_focus(
                y, y + max(1, int(focused.winfo_height())), viewport, content, current_top
            )
            if fraction is not None:
                canvas.yview_moveto(fraction)
        except Exception:
            return

    inner.bind("<Configure>", _sync)
    canvas.bind("<Configure>", _resize)
    inner.bind("<FocusIn>", _reveal_focused)
    outer.bind("<FocusIn>", _reveal_focused)
    return inner, outer


def _remember_layout(widget: tk.Misc) -> dict[str, Any]:
    """Store a widget's pre-RTL layout once so the mirror can be undone."""
    info = getattr(widget, "_tt_layout", None)
    if info is not None:
        return info
    manager = ""
    try:
        manager = widget.winfo_manager()
    except Exception:
        pass
    original: dict[str, Any] = {"manager": manager}
    try:
        if manager == "pack":
            original["pack"] = widget.pack_info()
        elif manager == "grid":
            original["grid"] = widget.grid_info()
    except Exception:
        pass
    for option in ("anchor", "justify"):
        try:
            original[option] = widget.cget(option)
        except Exception:
            pass
    try:
        text_value = widget.cget("text")
        if text_value:
            original["text"] = str(text_value)
    except Exception:
        pass
    try:
        setattr(widget, "_tt_layout", original)
    except Exception:
        pass
    return original


def apply_rtl(window: tk.Misc, lang: str) -> None:
    """Mirror a built widget tree for the given language (idempotent, reversible).

    English restores the original layout, so switching the language in Settings
    can call this again without rebuilding the window.
    """
    configure_rtl_widget_styles(lang)
    _apply_rtl_tree(window, lang)


def _apply_rtl_tree(parent: tk.Misc, lang: str) -> None:
    try:
        children = list(parent.winfo_children())
    except Exception:
        return
    max_column = 0
    for child in children:
        grid = _remember_layout(child).get("grid")
        if grid:
            try:
                max_column = max(max_column, int(grid.get("column", 0)))
            except Exception:
                pass
    for child in children:
        _apply_rtl_widget(child, lang, max_column)
        _apply_rtl_tree(child, lang)


def _apply_rtl_widget(widget: tk.Misc, lang: str, max_column: int) -> None:
    info = _remember_layout(widget)
    rtl = is_rtl(lang)

    if "anchor" in info:
        try:
            widget.configure(anchor="e" if rtl else info["anchor"])
        except Exception:
            pass
    if "justify" in info:
        try:
            widget.configure(justify="right" if rtl else info["justify"])
        except Exception:
            pass
    if "text" in info:
        try:
            widget.configure(text=bidi_text(info["text"], lang))
        except Exception:
            pass

    manager = info.get("manager")
    if manager == "pack" and "pack" in info:
        side = str(info["pack"].get("side", "top"))
        try:
            widget.pack_configure(side=flip_side(side, lang))
        except Exception:
            pass
    elif manager == "grid" and "grid" in info:
        grid = info["grid"]
        try:
            column = int(grid.get("column", 0))
            sticky = str(grid.get("sticky", ""))
            widget.grid_configure(
                column=mirror_column(column, max_column, lang),
                sticky=flip_sticky(sticky, lang),
            )
        except Exception:
            pass

    # Keep the scroll wrapper's cached side in step with the live mirror, so a
    # hidden scrollbar reappears on the correct side after an Arabic switch.
    if hasattr(widget, _SCROLL_CANVAS_ATTR) and hasattr(widget, _SCROLLBAR_SIDE_ATTR):
        setattr(widget, _SCROLLBAR_SIDE_ATTR, "left" if rtl else "right")


def _window_handle(root: tk.Misc) -> int:
    try:
        parent = ctypes.windll.user32.GetParent(root.winfo_id())
        return int(parent) or int(root.winfo_id())
    except Exception:
        return 0


def make_window_non_activating(root: tk.Misc) -> None:
    """Make a window topmost without ever stealing keyboard focus.

    ``WS_EX_NOACTIVATE`` keeps the user's typing in the application they are
    working in; the presence popup is dismissed by the global input poll or by a
    mouse click on the button.
    """
    if sys.platform != "win32":
        return
    hwnd = _window_handle(root)
    if not hwnd:
        return
    try:
        ex_style = int(ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE))
        ctypes.windll.user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE, ex_style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        )
    except Exception:
        pass
    reassert_topmost(root, show=True)


def reassert_topmost(root: tk.Misc, show: bool = False) -> None:
    """Re-apply HWND_TOPMOST without activating (needed above fullscreen apps)."""
    if sys.platform != "win32":
        return
    hwnd = _window_handle(root)
    if not hwnd:
        return
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | (SWP_SHOWWINDOW if show else 0)
    try:
        ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
    except Exception:
        pass


# =====================================================================
# 0. Active Presence Check ("Are You Active?")
# =====================================================================

def show_active_check_dialog_async(
    on_confirmed: Callable[[], None],
    on_timeout: Callable[[], None] | None = None,
    timeout_seconds: int = 60,
    lang: str = "en",
) -> None:
    """Show Cattr-style 'Are you active?' presence check popup."""
    global _is_dialog_active

    with _dialog_lock:
        if _is_dialog_active:
            return
        _is_dialog_active = True
        _touch_dialog_heartbeat()

    def _release_dialog_flag() -> None:
        """Release the single-dialog flag however the popup ends (P0-6)."""
        global _is_dialog_active
        with _dialog_lock:
            _is_dialog_active = False

    def _show() -> None:
        closed = False

        def cleanup() -> None:
            _release_dialog_flag()

        try:
            # Play a soft alert chime
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass

            master = ensure_ui_thread()
            root = tk.Toplevel(master)
            root.title(t("active_dialog_title", lang))
            root.resizable(False, False)
            root.attributes("-topmost", True)

            # v1.3.2: sized from the screen DPI and centered on the cursor monitor.
            fit_dialog(root, 360, 180)
            root.configure(bg="#ffffff")

            # Show above fullscreen apps (Teams/Zoom/PowerPoint) without taking
            # keyboard focus away from whatever the user is typing in.
            apply_rtl(root, lang)
            root.update_idletasks()
            make_window_non_activating(root)

            header_frame = tk.Frame(root, bg="#eff6ff", padx=15, pady=10)
            header_frame.pack(fill="x")

            tk.Label(
                header_frame,
                text="🔔 " + t("active_dialog_prompt", lang),
                font=("Segoe UI", 11, "bold"),
                bg="#eff6ff",
                fg="#1d4ed8",
            ).pack(anchor="center")

            tk.Label(
                root,
                text=t("active_dialog_sub", lang),
                font=("Segoe UI", 8),
                bg="#ffffff",
                fg="#64748b",
                wraplength=320,
                justify="center",
            ).pack(anchor="center", pady=(12, 10), padx=15)

            def do_confirm() -> None:
                nonlocal closed
                if closed:
                    return
                closed = True
                cleanup()
                try:
                    root.destroy()
                except Exception:
                    pass
                try:
                    on_confirmed()
                except Exception as e:
                    logger.error("Error in on_confirmed callback: %s", e)

            def do_timeout() -> None:
                nonlocal closed
                if closed:
                    return
                closed = True
                cleanup()
                try:
                    root.destroy()
                except Exception:
                    pass
                if on_timeout:
                    try:
                        on_timeout()
                    except Exception as e:
                        logger.error("Error in on_timeout callback: %s", e)

            btn_frame = tk.Frame(root, bg="#ffffff")
            btn_frame.pack(pady=8)

            confirm_btn = tk.Button(
                btn_frame,
                text=t("active_btn_confirm", lang),
                font=("Segoe UI", 10, "bold"),
                bg="#2563eb",
                fg="#ffffff",
                activebackground="#1d4ed8",
                activeforeground="#ffffff",
                relief="flat",
                cursor="hand2",
                padx=16,
                pady=6,
                command=do_confirm,
            )
            confirm_btn.pack()

            root.bind("<Return>", lambda e: do_confirm())
            root.bind("<space>", lambda e: do_confirm())
            root.protocol("WM_DELETE_WINDOW", do_timeout)

            # Never focus_force(): the popup must not capture the user's typing.
            reassert_topmost(root, show=True)

            # Input polling loop (every 200 ms).  A monotonic clock is used for the
            # timeout so a sleep/wake cycle can never make the remaining time
            # negative, and the popup is re-asserted above fullscreen windows twice
            # per second (display changes, lock/unlock, fullscreen apps).
            start_wall = time.monotonic()
            last_topmost_assert = 0.0

            class LASTINPUTINFO_POPUP(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            def _poll_input() -> None:
                nonlocal last_topmost_assert
                if closed:
                    return

                _touch_dialog_heartbeat()

                # Check timeout
                if (time.monotonic() - start_wall) >= timeout_seconds:
                    do_timeout()
                    return

                try:
                    lii = LASTINPUTINFO_POPUP()
                    lii.cbSize = ctypes.sizeof(LASTINPUTINFO_POPUP)
                    if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                        sys_ticks = int(ctypes.windll.kernel32.GetTickCount64())
                        idle_ms = (sys_ticks & 0xFFFFFFFF) - int(lii.dwTime)
                        if idle_ms < 0:
                            idle_ms += 0x100000000
                        if (idle_ms / 1000.0) < 1.0:
                            do_confirm()
                            return
                except Exception:
                    pass

                now_mono = time.monotonic()
                if (now_mono - last_topmost_assert) >= 0.5:
                    last_topmost_assert = now_mono
                    try:
                        reassert_topmost(root)
                        root.lift()
                    except Exception:
                        pass

                try:
                    root.after(200, _poll_input)
                except Exception:
                    pass

            root.after(200, _poll_input)

        except Exception as exc:
            cleanup()
            logger.error("Error displaying active check dialog: %s", exc)
            # Ensure the tracker's state machine is notified (treat as timeout)
            # so _active_check_popup_showing is released and idle mode engaged.
            if on_timeout is not None:
                try:
                    on_timeout()
                except Exception as cb_exc:
                    logger.error("Error in active check timeout callback: %s", cb_exc)

    try:
        dispatch_to_ui(_show)
    except Exception as exc:
        # UI thread could not be created/queued: never leave the flag set,
        # otherwise every future popup would be silently blocked.
        _release_dialog_flag()
        if on_timeout is not None:
            try:
                on_timeout()
            except Exception as cb_exc:
                logger.error("Error in active check timeout callback: %s", cb_exc)
        logger.error("Could not dispatch active check dialog to UI thread: %s", exc)


# =====================================================================
# 1. Overtime Prompt with Reason Selection
# =====================================================================

def show_overtime_prompt_async(
    prompt_message: str,
    on_response: Callable[[bool, str], None],
    lang: str = "en",
    timeout_seconds: int | None = None,
) -> None:
    """Show overtime dialog with quick reason choices.

    When ``timeout_seconds`` is given, an unanswered prompt resolves to "No" after
    that delay and is logged (item 4). The dialog never takes focus.
    """
    global _is_dialog_active

    with _dialog_lock:
        if _is_dialog_active:
            return
        _is_dialog_active = True
        _touch_dialog_heartbeat()

    def cleanup() -> None:
        global _is_dialog_active
        with _dialog_lock:
            _is_dialog_active = False

    if not _tk_available:
        def _native_ot_fallback() -> None:
            cleanup()
            display_msg = prompt_message
            if "ended" in prompt_message.lower():
                display_msg = f"{t('ot_work_ended', lang)}\n{t('ot_prompt_record', lang)}"
            elif "outside" in prompt_message.lower():
                display_msg = f"{t('ot_outside_hours', lang)}\n{t('ot_prompt_record', lang)}"
            res = win32_message_box(t("ot_dialog_title", lang), display_msg, MB_YESNO | MB_ICONQUESTION)
            if res == IDYES:
                on_response(True, "Overtime (Native Dialog Fallback)")
            else:
                on_response(False, "")

        threading.Thread(target=_native_ot_fallback, daemon=True).start()
        return

    def _show() -> None:
        global _is_dialog_active
        answered = False

        def _heartbeat() -> None:
            # The overtime prompt has no input poll of its own; keep the
            # watchdog fed while the window is open.
            if not answered:
                _touch_dialog_heartbeat()
                try:
                    root.after(5000, _heartbeat)
                except Exception:
                    pass

        try:
            master = ensure_ui_thread()
            if not _tk_available or master is None:
                raise RuntimeError("Tkinter unavailable")
            root = tk.Toplevel(master)
            root.title(t("ot_dialog_title", lang))
            root.resizable(False, False)
            root.attributes("-topmost", True)

            fit_dialog(root, 460, 230)
            root.configure(bg="#f8f9fa")

            # Localize prompt message if standard
            display_msg = prompt_message
            if "ended" in prompt_message.lower():
                display_msg = f"{t('ot_work_ended', lang)}\n{t('ot_prompt_record', lang)}"
            elif "outside" in prompt_message.lower():
                display_msg = f"{t('ot_outside_hours', lang)}\n{t('ot_prompt_record', lang)}"

            msg_label = tk.Label(
                root,
                text=display_msg,
                font=("Segoe UI", 10, "bold"),
                bg="#f8f9fa",
                fg="#212529",
                wraplength=420,
                justify="center",
            )
            msg_label.pack(pady=(15, 10), padx=15)

            reason_frame = tk.Frame(root, bg="#f8f9fa")
            reason_frame.pack(pady=5)

            tk.Label(
                reason_frame,
                text=t("ot_reason_label", lang),
                font=("Segoe UI", 9),
                bg="#f8f9fa",
                fg="#495057",
            ).pack(side="left", padx=5)

            if lang == "ar":
                reasons = ["إنجاز مشروع", "حالة طارئة", "تعويض ساعات", "عمل عام"]
            else:
                reasons = ["Project Work", "Urgent Emergency", "Work Compensation", "General Work"]

            reason_var = tk.StringVar(value=reasons[0])
            reason_cb = ttk.Combobox(
                reason_frame,
                textvariable=reason_var,
                values=reasons,
                state="readonly",
                width=24,
                font=("Segoe UI", 9),
            )
            reason_cb.pack(side="left", padx=5)

            btn_frame = tk.Frame(root, bg="#f8f9fa")
            btn_frame.pack(pady=15)

            def on_yes() -> None:
                nonlocal answered
                answered = True
                selected_reason = reason_var.get()
                cleanup()
                root.destroy()
                on_response(True, selected_reason)

            def on_no() -> None:
                nonlocal answered
                answered = True
                cleanup()
                root.destroy()
                on_response(False, "")

            def on_close() -> None:
                nonlocal answered
                if not answered:
                    answered = True
                    cleanup()
                    on_response(False, "")
                root.destroy()

            root.protocol("WM_DELETE_WINDOW", on_close)

            yes_btn = tk.Button(
                btn_frame,
                text=t("ot_yes", lang),
                font=("Segoe UI", 10, "bold"),
                width=18 if lang == "ar" else 16,
                bg="#007bff",
                fg="#ffffff",
                relief="flat",
                command=on_yes,
            )
            yes_btn.pack(side="left", padx=8)

            no_btn = tk.Button(
                btn_frame,
                text=t("ot_no", lang),
                font=("Segoe UI", 10),
                width=10,
                bg="#e9ecef",
                fg="#212529",
                relief="flat",
                command=on_no,
            )
            no_btn.pack(side="left", padx=8)

            apply_rtl(root, lang)
            yes_btn.focus_set()
            root.lift()

            if timeout_seconds and int(timeout_seconds) > 0:
                def _auto_decline() -> None:
                    nonlocal answered
                    if answered:
                        return
                    answered = True
                    logger.info(
                        "Overtime prompt timed out after %ss with no response; "
                        "recording it as declined.",
                        timeout_seconds,
                    )
                    cleanup()
                    try:
                        root.destroy()
                    except Exception:
                        pass
                    on_response(False, "")

                root.after(int(timeout_seconds) * 1000, _auto_decline)

            _heartbeat()

        except Exception as exc:
            logger.error("Error in overtime prompt dialog: %s. Using Win32 MessageBoxW fallback.", exc, exc_info=True)
            if not answered:
                answered = True
                cleanup()
                display_msg = prompt_message
                if "ended" in prompt_message.lower():
                    display_msg = f"{t('ot_work_ended', lang)}\n{t('ot_prompt_record', lang)}"
                elif "outside" in prompt_message.lower():
                    display_msg = f"{t('ot_outside_hours', lang)}\n{t('ot_prompt_record', lang)}"
                res = win32_message_box(t("ot_dialog_title", lang), display_msg, MB_YESNO | MB_ICONQUESTION)
                if res == IDYES:
                    on_response(True, "Overtime (Native Dialog Fallback)")
                else:
                    on_response(False, "")

    try:
        dispatch_to_ui(_show)
    except Exception as exc:
        cleanup()
        logger.error("Could not dispatch overtime prompt to UI thread: %s", exc, exc_info=True)
        try:
            display_msg = prompt_message
            if "ended" in prompt_message.lower():
                display_msg = f"{t('ot_work_ended', lang)}\n{t('ot_prompt_record', lang)}"
            elif "outside" in prompt_message.lower():
                display_msg = f"{t('ot_outside_hours', lang)}\n{t('ot_prompt_record', lang)}"
            res = win32_message_box(t("ot_dialog_title", lang), display_msg, MB_YESNO | MB_ICONQUESTION)
            if res == IDYES:
                on_response(True, "Overtime (Native Dialog Fallback)")
            else:
                on_response(False, "")
        except Exception:
            pass


def show_smart_meeting_prompt_async(
    app_label: str,
    on_start: Callable[[], None],
    on_ignore: Callable[[], None] | None = None,
    lang: str = "en",
    timeout_seconds: int = 30,
) -> None:
    """Show non-intrusive smart meeting prompt offering 1-click meeting start.
    
    Includes native Win32 MessageBoxW fallback if Tcl/Tk is broken or unavailable.
    """
    global _is_dialog_active
    with _dialog_lock:
        if _is_dialog_active:
            return
        _is_dialog_active = True
        _touch_dialog_heartbeat()

    def cleanup() -> None:
        global _is_dialog_active
        with _dialog_lock:
            _is_dialog_active = False

    def _native_fallback() -> None:
        cleanup()
        title = t("smart_meeting_title", lang)
        msg = t("smart_meeting_prompt", lang, app=app_label)
        res = win32_message_box(title, msg, MB_YESNO | MB_ICONQUESTION)
        if res == IDYES:
            try:
                on_start()
            except Exception as e:
                logger.error("Error in on_start meeting callback: %s", e)
        else:
            if on_ignore:
                try:
                    on_ignore()
                except Exception as e:
                    logger.error("Error in on_ignore meeting callback: %s", e)

    if not _tk_available:
        threading.Thread(target=_native_fallback, daemon=True).start()
        return

    def _show() -> None:
        closed = False
        try:
            master = ensure_ui_thread()
            if master is None or not _tk_available:
                _native_fallback()
                return

            root = tk.Toplevel(master)
            root.title(t("smart_meeting_title", lang))
            root.resizable(False, False)
            root.attributes("-topmost", True)
            fit_dialog(root, 380, 190)
            root.configure(bg="#ffffff")
            apply_rtl(root, lang)
            make_window_non_activating(root)

            # Header
            header_frame = tk.Frame(root, bg="#f0fdf4", padx=15, pady=10)
            header_frame.pack(fill="x")
            title_lbl = tk.Label(
                header_frame,
                text="📹 " + t("smart_meeting_title", lang),
                font=("Segoe UI", 11, "bold"),
                bg="#f0fdf4",
                fg="#166534",
            )
            title_lbl.pack(anchor="w" if lang != "ar" else "e")

            # Body text
            body_frame = tk.Frame(root, bg="#ffffff", padx=15, pady=12)
            body_frame.pack(fill="both", expand=True)
            msg_text = t("smart_meeting_prompt", lang, app=app_label)
            prompt_lbl = tk.Label(
                body_frame,
                text=msg_text,
                font=("Segoe UI", 9),
                bg="#ffffff",
                fg="#1e293b",
                wraplength=340,
                justify="left" if lang != "ar" else "right",
            )
            prompt_lbl.pack(anchor="w" if lang != "ar" else "e")

            def do_start() -> None:
                nonlocal closed
                if closed:
                    return
                closed = True
                cleanup()
                try:
                    root.destroy()
                except Exception:
                    pass
                try:
                    on_start()
                except Exception as e:
                    logger.error("Error in on_start: %s", e)

            def do_ignore() -> None:
                nonlocal closed
                if closed:
                    return
                closed = True
                cleanup()
                try:
                    root.destroy()
                except Exception:
                    pass
                if on_ignore:
                    try:
                        on_ignore()
                    except Exception as e:
                        logger.error("Error in on_ignore: %s", e)

            # Wire the window-close button to the ignore path now that it is defined.
            root.protocol("WM_DELETE_WINDOW", do_ignore)

            # Buttons
            btn_frame = tk.Frame(root, bg="#ffffff", padx=15, pady=10)
            btn_frame.pack(fill="x")

            start_btn = tk.Button(
                btn_frame,
                text="▶ " + t("smart_meeting_start", lang),
                font=("Segoe UI", 9, "bold"),
                bg="#16a34a",
                fg="#ffffff",
                activebackground="#15803d",
                activeforeground="#ffffff",
                relief="flat",
                padx=14,
                pady=5,
                cursor="hand2",
                command=do_start,
            )
            start_btn.pack(side="left" if lang != "ar" else "right", padx=5)

            ignore_btn = tk.Button(
                btn_frame,
                text=t("smart_meeting_ignore", lang),
                font=("Segoe UI", 9),
                bg="#e2e8f0",
                fg="#334155",
                activebackground="#cbd5e1",
                activeforeground="#1e293b",
                relief="flat",
                padx=12,
                pady=5,
                cursor="hand2",
                command=do_ignore,
            )
            ignore_btn.pack(side="right" if lang != "ar" else "left", padx=5)

            # Auto close after timeout
            root.after(timeout_seconds * 1000, do_ignore)

            def _heartbeat() -> None:
                if closed:
                    return
                _touch_dialog_heartbeat()
                try:
                    root.after(5000, _heartbeat)
                except Exception:
                    pass

            _touch_dialog_heartbeat()
            root.after(5000, _heartbeat)

        except Exception as exc:
            logger.error("Tkinter error creating smart meeting dialog: %s. Using fallback.", exc, exc_info=True)
            _native_fallback()

    try:
        dispatch_to_ui(_show)
    except Exception as exc:
        logger.error("Could not dispatch smart meeting prompt to UI: %s. Executing fallback.", exc, exc_info=True)
        _native_fallback()


# =====================================================================
# 2. Meeting End Note Dialog
# =====================================================================

def show_meeting_end_note_dialog_async(
    duration_min: int,
    on_submit: Callable[[str], None],
    lang: str = "en",
) -> None:
    """Prompt for an optional note when ending a meeting."""
    def _show() -> None:
        try:
            master = ensure_ui_thread()
            if not _tk_available or master is None:
                raise RuntimeError("Tkinter unavailable")
            root = tk.Toplevel(master)
            root.title(t("note_dialog_title", lang))
            root.resizable(False, False)
            root.attributes("-topmost", True)

            fit_dialog(root, 420, 200)
            root.configure(bg="#f8f9fa")

            tk.Label(
                root,
                text=t("note_dialog_prompt", lang, duration=f"{duration_min} min"),
                font=("Segoe UI", 10, "bold"),
                bg="#f8f9fa",
                fg="#28a745",
                justify="center",
            ).pack(pady=(15, 5))

            note_entry = tk.Entry(root, font=("Segoe UI", 10), width=36)
            note_entry.pack(pady=8, padx=20)
            note_entry.focus_set()

            btn_frame = tk.Frame(root, bg="#f8f9fa")
            btn_frame.pack(pady=8)

            def on_save() -> None:
                note_text = note_entry.get().strip()
                root.destroy()
                on_submit(note_text)

            def on_skip() -> None:
                root.destroy()
                on_submit("")

            tk.Button(
                btn_frame,
                text=t("note_save", lang),
                font=("Segoe UI", 9, "bold"),
                width=12,
                bg="#007bff",
                fg="#ffffff",
                relief="flat",
                command=on_save,
            ).pack(side="left", padx=5)

            tk.Button(
                btn_frame,
                text=t("note_skip", lang),
                font=("Segoe UI", 9),
                width=10,
                bg="#e9ecef",
                relief="flat",
                command=on_skip,
            ).pack(side="left", padx=5)

            root.bind("<Return>", lambda e: on_save())
            root.protocol("WM_DELETE_WINDOW", on_skip)
            apply_rtl(root, lang)
            root.lift()

        except Exception as exc:
            logger.error("Error showing meeting note dialog: %s", exc)
            try:
                on_submit("")
            except Exception as cb_exc:
                logger.error("Error in meeting note submit callback: %s", cb_exc)

    dispatch_to_ui(_show)


# =====================================================================
# 3. Retroactive Past Meeting Dialog
# =====================================================================

def show_past_meeting_dialog_async(
    on_submit: Callable[[str, str, str, str, bool, str], None],
    lang: str = "en",
) -> None:
    """Dialog to add a missed meeting retroactively.

    ``on_submit(date_str, start_time_str, end_time_str, note, count_as_overtime, reason)``
    The overtime checkbox defaults to checked so a retroactive meeting outside
    working hours is booked as Overtime unless the user says otherwise (D3).
    """
    def _show() -> None:
        try:
            master = ensure_ui_thread()
            if not _tk_available or master is None:
                raise RuntimeError("Tkinter unavailable")
            root = tk.Toplevel(master)
            root.title(t("past_dialog_title", lang))
            root.resizable(False, False)
            root.attributes("-topmost", True)

            fit_dialog(root, 460, 400)
            root.configure(bg="#f8f9fa")

            tk.Label(
                root,
                text=t("past_dialog_title", lang),
                font=("Segoe UI", 11, "bold"),
                bg="#f8f9fa",
                fg="#212529",
            ).pack(pady=(12, 10))

            form = tk.Frame(root, bg="#f8f9fa")
            form.pack(padx=20, pady=5)

            # Date
            tk.Label(form, text=t("past_label_date", lang), font=("Segoe UI", 9), bg="#f8f9fa").grid(row=0, column=0, sticky="e", pady=4, padx=5)
            date_entry = tk.Entry(form, font=("Segoe UI", 9), width=18)
            date_entry.insert(0, date.today().isoformat())
            date_entry.grid(row=0, column=1, sticky="w", pady=4, padx=5)

            # Start time
            tk.Label(form, text=t("past_label_start", lang), font=("Segoe UI", 9), bg="#f8f9fa").grid(row=1, column=0, sticky="e", pady=4, padx=5)
            start_entry = tk.Entry(form, font=("Segoe UI", 9), width=18)
            start_entry.insert(0, "10:00")
            start_entry.grid(row=1, column=1, sticky="w", pady=4, padx=5)

            # End time
            tk.Label(form, text=t("past_label_end", lang), font=("Segoe UI", 9), bg="#f8f9fa").grid(row=2, column=0, sticky="e", pady=4, padx=5)
            end_entry = tk.Entry(form, font=("Segoe UI", 9), width=18)
            end_entry.insert(0, "11:00")
            end_entry.grid(row=2, column=1, sticky="w", pady=4, padx=5)

            # Note
            tk.Label(form, text=t("past_label_note", lang), font=("Segoe UI", 9), bg="#f8f9fa").grid(row=3, column=0, sticky="e", pady=4, padx=5)
            note_entry = tk.Entry(form, font=("Segoe UI", 9), width=24)
            note_entry.grid(row=3, column=1, sticky="w", pady=4, padx=5)

            # D3: retroactive meetings outside paid hours can be booked as overtime.
            as_ot_var = tk.BooleanVar(value=True)
            make_selection_widget(
                form, "Checkbutton",
                text=t("past_label_as_overtime", lang),
                variable=as_ot_var,
                lang=lang,
                font=("Segoe UI", 9),
            ).grid(row=4, column=0, columnspan=2, sticky="we", pady=(8, 2), padx=5)

            tk.Label(
                form, text=t("past_label_reason", lang), font=("Segoe UI", 9), bg="#f8f9fa"
            ).grid(row=5, column=0, sticky="e", pady=4, padx=5)
            if lang == "ar":
                past_reasons = ["إنجاز مشروع", "حالة طارئة", "تعويض ساعات", "عمل عام"]
            else:
                past_reasons = ["Project Work", "Urgent Emergency", "Work Compensation", "General Work"]
            past_reason_var = tk.StringVar(value=past_reasons[0])
            ttk.Combobox(
                form,
                textvariable=past_reason_var,
                values=past_reasons,
                state="readonly",
                width=22,
                font=("Segoe UI", 9),
            ).grid(row=5, column=1, sticky="w", pady=4, padx=5)

            def on_add() -> None:
                d_str = date_entry.get().strip()
                s_str = start_entry.get().strip()
                e_str = end_entry.get().strip()
                n_str = note_entry.get().strip()

                try:
                    dt_s = datetime.strptime(f"{d_str} {s_str}", "%Y-%m-%d %H:%M")
                    dt_e = datetime.strptime(f"{d_str} {e_str}", "%Y-%m-%d %H:%M")
                    # B16: reject dates that would break timestamp arithmetic.
                    if dt_s.date() < date(1970, 1, 2) or dt_s.date() > date.today():
                        messagebox.showerror("Error", t("past_err_date_range", lang), parent=root)
                        return
                    if dt_e <= dt_s:
                        messagebox.showerror("Error", t("past_err_time", lang), parent=root)
                        return
                    root.destroy()
                    on_submit(d_str, s_str, e_str, n_str, bool(as_ot_var.get()), past_reason_var.get())
                except ValueError:
                    messagebox.showerror(
                        "Invalid Format",
                        t("past_err_format", lang),
                        parent=root,
                    )

            btn_frame = tk.Frame(root, bg="#f8f9fa")
            btn_frame.pack(pady=15)

            tk.Button(
                btn_frame,
                text=t("past_btn_add", lang),
                font=("Segoe UI", 9, "bold"),
                width=14,
                bg="#28a745",
                fg="#ffffff",
                relief="flat",
                command=on_add,
            ).pack(side="left", padx=5)

            tk.Button(
                btn_frame,
                text=t("past_btn_cancel", lang),
                font=("Segoe UI", 9),
                width=10,
                bg="#e9ecef",
                relief="flat",
                command=root.destroy,
            ).pack(side="left", padx=5)

            apply_rtl(root, lang)
            root.lift()

        except Exception as exc:
            logger.error("Error in past meeting dialog: %s", exc)

    dispatch_to_ui(
        _show,
        fallback=lambda: (
            _unavailable_notice("Dialog unavailable — the past meeting was not added."),
            on_submit(None, None, None, None, None, None),
        ),
    )


def show_directory_picker_async(
    on_submit: Callable[[Path], None],
    lang: str = "en",
    title: str | None = None,
) -> None:
    """Ask for a folder in the UI thread (used by 'Export CSV to folder...')."""
    def _show() -> None:
        try:
            from tkinter import filedialog

            ensure_ui_thread()
            selected = filedialog.askdirectory(title=title or t("action_export_to_folder", lang))
            if selected:
                on_submit(Path(selected))
        except Exception as exc:
            logger.error("Error in directory picker: %s", exc)

    dispatch_to_ui(
        _show,
        fallback=lambda: (
            _unavailable_notice("Dialog unavailable — export cancelled."),
            on_submit(None),
        ),
    )


# =====================================================================
# 4. Pause Tracking Dialog
# =====================================================================

def show_pause_dialog_async(
    on_pause_selected: Callable[[int], None],
    lang: str = "en",
) -> None:
    """Dialog to temporarily pause tracking for personal computer use.

    on_pause_selected(duration_minutes). 0 means until manually resumed.
    """
    def _show() -> None:
        try:
            master = ensure_ui_thread()
            if not _tk_available or master is None:
                raise RuntimeError("Tkinter unavailable")
            root = tk.Toplevel(master)
            root.title(t("pause_dialog_title", lang))
            root.resizable(False, False)
            root.attributes("-topmost", True)

            fit_dialog(root, 440, 350)
            root.configure(bg="#ffffff")

            header_frame = tk.Frame(root, bg="#fef2f2", padx=20, pady=12)
            header_frame.pack(fill="x")

            tk.Label(
                header_frame,
                text="⏸ " + t("pause_dialog_title", lang),
                font=("Segoe UI", 12, "bold"),
                bg="#fef2f2",
                fg="#b91c1c",
            ).pack(anchor="center")

            tk.Label(
                header_frame,
                text=t("pause_prompt", lang),
                font=("Segoe UI", 8),
                bg="#fef2f2",
                fg="#64748b",
                wraplength=390,
                justify="center",
            ).pack(anchor="center", pady=(4, 0))

            choice_var = tk.IntVar(value=30)
            opt_frame = tk.Frame(root, bg="#ffffff", padx=30, pady=12)
            opt_frame.pack(fill="x")

            options = [
                (t("pause_min_15", lang), 15),
                (t("pause_min_30", lang), 30),
                (t("pause_min_60", lang), 60),
                (t("pause_min_manual", lang), 0),
            ]

            for text, val in options:
                make_selection_widget(
                    opt_frame, "Radiobutton",
                    text=text,
                    variable=choice_var,
                    value=val,
                    lang=lang,
                    font=("Segoe UI", 10),
                ).pack(anchor="w", pady=4)

            def on_confirm() -> None:
                dur = choice_var.get()
                root.destroy()
                on_pause_selected(dur)

            btn_frame = tk.Frame(root, bg="#ffffff", padx=20, pady=10)
            btn_frame.pack(fill="x", side="bottom")

            ok_btn = tk.Button(
                btn_frame,
                text="✓  " + t("pause_btn_confirm", lang),
                font=("Segoe UI", 10, "bold"),
                bg="#dc2626",
                fg="#ffffff",
                activebackground="#b91c1c",
                activeforeground="#ffffff",
                relief="flat",
                cursor="hand2",
                padx=16,
                pady=6,
                command=on_confirm,
            )
            ok_btn.pack(side="left" if lang == "en" else "right", expand=True, fill="x", padx=4)

            cancel_btn = tk.Button(
                btn_frame,
                text=t("pause_btn_cancel", lang),
                font=("Segoe UI", 9),
                bg="#f1f5f9",
                fg="#475569",
                activebackground="#e2e8f0",
                activeforeground="#1e293b",
                relief="flat",
                cursor="hand2",
                padx=12,
                pady=6,
                command=root.destroy,
            )
            cancel_btn.pack(side="right" if lang == "en" else "left", padx=4)

            root.bind("<Return>", lambda event: on_confirm())
            root.bind("<Escape>", lambda event: root.destroy())
            apply_rtl(root, lang)
            root.lift()

        except Exception as exc:
            logger.error("Error in pause dialog: %s", exc)

    dispatch_to_ui(
        _show,
        fallback=lambda: (
            _unavailable_notice("Dialog unavailable — pause was not changed."),
            on_pause_selected(None),
        ),
    )


# =====================================================================
# 5. Full Settings Dialog
# =====================================================================

def show_settings_dialog_async(
    config: Config,
    on_save: Callable[[Config], None],
    db: Any | None = None,
) -> None:
    """Full visual settings window allowing total control over work hours,
    work days, holidays, notifications, language, app tracking, projects,
    retention, updates, and auto backup/export.

    ``db`` is optional and only used by the Projects tab and the CSV export button.
    """
    def _show() -> None:
        try:
            master = ensure_ui_thread()
            if not _tk_available or master is None:
                raise RuntimeError("Tkinter unavailable")
            root = tk.Toplevel(master)
            lang = config.language
            root.title(t("settings_title", lang))
            root.resizable(False, False)
            root.attributes("-topmost", True)

            fit_dialog(root, 600, 720)
            root.configure(bg="#f8f9fa")

            # Header Frame
            header_frame = tk.Frame(root, bg="#0f2b48", padx=20, pady=16)
            header_frame.pack(fill="x")

            title_label = tk.Label(
                header_frame,
                text="⚙ " + t("settings_title", lang),
                font=("Segoe UI", 13, "bold"),
                bg="#0f2b48",
                fg="#ffffff",
            )
            title_label.pack(anchor="w")

            style = ttk.Style()
            style.theme_use("clam")

            notebook = ttk.Notebook(root)
            notebook.pack(fill="both", expand=True, padx=15, pady=10)

            # Tab 1: General & Working Hours
            tab_general, tab_general_outer = make_scrollable(notebook)
            notebook.add(tab_general_outer, text=t("tab_general", lang))

            lang_frame = tk.LabelFrame(tab_general, text=f" {t('sec_language', lang)} ", bg="#f8f9fa", font=("Segoe UI", 9, "bold"), padx=10, pady=5)
            lang_frame.pack(fill="x", padx=15, pady=10)

            lang_var = tk.StringVar(value=config.language)
            lang_change_hooks: list[Callable[[], None]] = []

            def _switch_language(value: str) -> None:
                """Mirror the window live when the language radio changes (item 1)."""
                nonlocal lang
                lang = value
                apply_rtl(root, lang)
                try:
                    root.title(t("settings_title", lang))
                except Exception:
                    pass
                for hook in lang_change_hooks:
                    try:
                        hook()
                    except Exception as exc:
                        logger.error("Language refresh hook failed: %s", exc)

            make_selection_widget(
                lang_frame, "Radiobutton", text="English", variable=lang_var, value="en",
                lang=lang, font=("Segoe UI", 9), command=lambda: _switch_language("en"),
            ).pack(side="left", padx=15)
            make_selection_widget(
                lang_frame, "Radiobutton", text="العربية (Arabic)", variable=lang_var, value="ar",
                lang=lang, font=("Segoe UI", 9), command=lambda: _switch_language("ar"),
            ).pack(side="left", padx=15)

            wh_frame = tk.LabelFrame(tab_general, text=f" {t('sec_work_hours', lang)} ", bg="#f8f9fa", font=("Segoe UI", 9, "bold"), padx=10, pady=5)
            wh_frame.pack(fill="x", padx=15, pady=5)

            tk.Label(wh_frame, text=t("lbl_work_start", lang), font=("Segoe UI", 9), bg="#f8f9fa").grid(row=0, column=0, sticky="e", pady=5, padx=5)
            start_entry = tk.Entry(wh_frame, font=("Segoe UI", 9), width=8)
            start_entry.insert(0, config.work_start)
            start_entry.grid(row=0, column=1, sticky="w", pady=5, padx=5)

            tk.Label(wh_frame, text=t("lbl_work_end", lang), font=("Segoe UI", 9), bg="#f8f9fa").grid(row=0, column=2, sticky="e", pady=5, padx=5)
            end_entry = tk.Entry(wh_frame, font=("Segoe UI", 9), width=8)
            end_entry.insert(0, config.work_end)
            end_entry.grid(row=0, column=3, sticky="w", pady=5, padx=5)

            tk.Label(wh_frame, text=t("lbl_daily_target", lang), font=("Segoe UI", 9), bg="#f8f9fa").grid(row=1, column=0, sticky="e", pady=5, padx=5)
            target_entry = tk.Entry(wh_frame, font=("Segoe UI", 9), width=8)
            target_entry.insert(0, str(config.daily_target_hours))
            target_entry.grid(row=1, column=1, sticky="w", pady=5, padx=5)

            tk.Label(wh_frame, text=t("lbl_weekly_target", lang), font=("Segoe UI", 9), bg="#f8f9fa").grid(row=1, column=2, sticky="e", pady=5, padx=5)
            weekly_entry = tk.Entry(wh_frame, font=("Segoe UI", 9), width=8)
            weekly_entry.insert(0, str(config.weekly_target_hours))
            weekly_entry.grid(row=1, column=3, sticky="w", pady=5, padx=5)

            # Work Days checkboxes
            days_frame = tk.LabelFrame(tab_general, text=f" {t('sec_work_days', lang)} ", bg="#f8f9fa", font=("Segoe UI", 9, "bold"), padx=10, pady=5)
            days_frame.pack(fill="x", padx=15, pady=10)

            all_days = [
                ("Sun", t("day_Sun", lang)),
                ("Mon", t("day_Mon", lang)),
                ("Tue", t("day_Tue", lang)),
                ("Wed", t("day_Wed", lang)),
                ("Thu", t("day_Thu", lang)),
                ("Fri", t("day_Fri", lang)),
                ("Sat", t("day_Sat", lang)),
            ]

            day_vars: dict[str, tk.BooleanVar] = {}
            for i, (day_code, day_label) in enumerate(all_days):
                v = tk.BooleanVar(value=day_code in config.work_days)
                day_vars[day_code] = v
                make_selection_widget(
                    days_frame, "Checkbutton",
                    text=day_label,
                    variable=v,
                    lang=lang,
                    font=("Segoe UI", 8),
                ).grid(row=i // 4, column=i % 4, sticky="w", padx=4, pady=3)

            # Tab 2: Holidays
            tab_holidays, tab_holidays_outer = make_scrollable(notebook)
            notebook.add(tab_holidays_outer, text=t("tab_holidays", lang))

            from time_tracker.localization.egypt_holidays import (
                count_new_holidays,
                get_egyptian_holidays,
                get_holiday_name,
                list_supported_years,
            )

            tk.Label(
                tab_holidays,
                text=t("holidays_desc", lang),
                font=("Segoe UI", 8),
                bg="#f8f9fa",
                fg="#6c757d",
            ).pack(anchor="w", padx=15, pady=(10, 5))

            # Dates are stored separately from the displayed text: the list now
            # shows "2026-01-07 — Coptic Christmas" while config still gets only
            # the YYYY-MM-DD value on Save.
            holiday_dates: list[str] = sorted({str(h) for h in (config.holidays or [])})

            h_list_frame = tk.Frame(tab_holidays, bg="#f8f9fa")
            h_list_frame.pack(fill="x", padx=15, pady=5)

            holidays_listbox = tk.Listbox(h_list_frame, font=("Segoe UI", 9), selectmode="single", height=10)
            holidays_listbox.pack(side="left", fill="both", expand=True)

            h_scroll = tk.Scrollbar(h_list_frame, orient="vertical", command=holidays_listbox.yview)
            h_scroll.pack(side="right", fill="y")
            holidays_listbox.config(yscrollcommand=h_scroll.set)

            def holiday_label(value: str) -> str:
                name = get_holiday_name(value, lang)
                label = f"{value}  —  {name}" if name else value
                return bidi_text(label, lang)

            def refresh_holidays_list(select_date: str | None = None) -> None:
                dates = sorted(set(holiday_dates))
                holidays_listbox.delete(0, tk.END)
                for value in dates:
                    holidays_listbox.insert(tk.END, holiday_label(value))
                if select_date is not None and select_date in dates:
                    index = dates.index(select_date)
                    holidays_listbox.selection_set(index)
                    holidays_listbox.see(index)

            refresh_holidays_list()
            lang_change_hooks.append(lambda: refresh_holidays_list())

            h_add_frame = tk.Frame(tab_holidays, bg="#f8f9fa")
            h_add_frame.pack(fill="x", padx=15, pady=10)

            new_date_entry = tk.Entry(h_add_frame, font=("Segoe UI", 9), width=12)
            new_date_entry.insert(0, date.today().strftime("%Y-%m-%d"))
            new_date_entry.pack(side="left", padx=5)

            def add_holiday() -> None:
                val = new_date_entry.get().strip()
                try:
                    datetime.strptime(val, "%Y-%m-%d")
                except ValueError:
                    messagebox.showerror("Invalid Date", "Please enter date as YYYY-MM-DD", parent=root)
                    return
                if val not in holiday_dates:
                    holiday_dates.append(val)
                    refresh_holidays_list(select_date=val)

            def remove_holiday() -> None:
                sel = holidays_listbox.curselection()
                if not sel:
                    return
                dates = sorted(set(holiday_dates))
                if sel[0] >= len(dates):
                    return
                holiday_dates[:] = [d for d in holiday_dates if d != dates[sel[0]]]
                refresh_holidays_list()

            tk.Button(h_add_frame, text=t("btn_add_date", lang), command=add_holiday, font=("Segoe UI", 8), bg="#e9ecef").pack(side="left", padx=3)
            tk.Button(h_add_frame, text=t("btn_remove_selected", lang), command=remove_holiday, font=("Segoe UI", 8), bg="#e9ecef").pack(side="left", padx=3)

            def import_egypt_holidays() -> None:
                """Merge the bundled Egyptian holidays of the selected year (offline)."""
                try:
                    year = int(year_spin.get().strip())
                except (ValueError, tk.TclError):
                    year = date.today().year

                new_dates = get_egyptian_holidays(year)
                if not new_dates:
                    messagebox.showinfo("Egypt Holidays", t("egypt_no_data", lang), parent=root)
                    return

                existing = list(holiday_dates)
                added = count_new_holidays(existing, new_dates)
                holiday_dates[:] = sorted(set(existing) | set(new_dates))
                refresh_holidays_list()
                messagebox.showinfo(
                    "Egypt Holidays",
                    t("egypt_imported_msg", lang, count=added),
                    parent=root,
                )

            year_frame = tk.Frame(h_add_frame, bg="#f8f9fa")
            year_frame.pack(side="left", padx=(0, 6))

            tk.Label(
                year_frame,
                text=t("holidays_year_label", lang),
                font=("Segoe UI", 8),
                bg="#f8f9fa",
                fg="#4b5563",
            ).pack(side="left", padx=(0, 4))

            supported_years = list_supported_years() or [date.today().year]
            year_spin = tk.Spinbox(
                year_frame,
                from_=min(supported_years),
                to=max(max(supported_years), date.today().year),
                width=6,
                font=("Segoe UI", 8),
            )
            year_spin.delete(0, tk.END)
            default_year = date.today().year if date.today().year in supported_years else supported_years[-1]
            year_spin.insert(0, str(default_year))
            year_spin.pack(side="left")

            tk.Button(
                h_add_frame,
                text="🇪🇬 " + t("btn_import_egypt_holidays", lang),
                command=import_egypt_holidays,
                font=("Segoe UI", 8),
                bg="#e0f2fe",
            ).pack(side="left", padx=5)

            # Tab 3: Reminders & Features
            tab_features, tab_features_outer = make_scrollable(notebook)
            notebook.add(tab_features_outer, text=t("tab_features", lang))

            autostart_var = tk.BooleanVar(value=config.autostart_enabled)
            app_tracking_var = tk.BooleanVar(value=getattr(config, "enable_app_tracking", True))
            eod_var = tk.BooleanVar(value=config.enable_end_of_day_notification)
            meet_remind_var = tk.BooleanVar(value=config.enable_meeting_reminder)
            stretch_remind_var = tk.BooleanVar(value=config.enable_stretch_reminder)
            auto_export_var = tk.BooleanVar(value=config.enable_auto_weekly_export)
            auto_backup_var = tk.BooleanVar(value=config.enable_auto_weekly_backup)
            active_check_var = tk.BooleanVar(value=getattr(config, "active_check_enabled", True))
            csv_excel_safe_var = tk.BooleanVar(value=getattr(config, "csv_excel_safe", True))

            feats = [
                (t("feat_autostart", lang), autostart_var),
                (t("feat_app_tracking", lang), app_tracking_var),
                (t("feat_active_check", lang), active_check_var),
                (t("feat_eod", lang), eod_var),
                (t("feat_meeting_remind", lang), meet_remind_var),
                (t("feat_stretch", lang), stretch_remind_var),
                (t("feat_auto_export", lang), auto_export_var),
                (t("feat_auto_backup", lang), auto_backup_var),
            ]

            for label_text, v in feats:
                make_selection_widget(
                    tab_features, "Checkbutton",
                    text=label_text,
                    variable=v,
                    lang=lang,
                    font=("Segoe UI", 9),
                ).pack(fill="x", padx=15, pady=4)

            interval_frame = tk.Frame(tab_features, bg="#f8f9fa")
            interval_frame.pack(fill="x", padx=35, pady=(2, 6))

            tk.Label(
                interval_frame,
                text=t("lbl_active_check_interval", lang),
                font=("Segoe UI", 8),
                bg="#f8f9fa",
                fg="#4b5563",
            ).pack(side="left", padx=(0, 6))

            interval_spin = tk.Spinbox(
                interval_frame,
                from_=1,
                to=60,
                width=4,
                font=("Segoe UI", 9),
            )
            interval_spin.delete(0, tk.END)
            interval_spin.insert(0, str(getattr(config, "active_check_interval_min", 3)))
            interval_spin.pack(side="left")

            # v1.3.2 (item 4): how long an overtime prompt waits before it resolves to "No".
            ot_timeout_frame = tk.Frame(tab_features, bg="#f8f9fa")
            ot_timeout_frame.pack(fill="x", padx=35, pady=(2, 6))
            tk.Label(
                ot_timeout_frame,
                text=t("lbl_overtime_timeout", lang),
                font=("Segoe UI", 8),
                bg="#f8f9fa",
                fg="#4b5563",
            ).pack(side="left", padx=(0, 6))
            ot_timeout_spin = tk.Spinbox(
                ot_timeout_frame,
                from_=30,
                to=600,
                increment=10,
                width=5,
                font=("Segoe UI", 9),
            )
            ot_timeout_spin.delete(0, tk.END)
            ot_timeout_spin.insert(
                0, str(getattr(config, "overtime_prompt_timeout_seconds", 90))
            )
            ot_timeout_spin.pack(side="left")

            def _ot_timeout_value() -> int:
                try:
                    value = int(ot_timeout_spin.get())
                except (TypeError, ValueError):
                    value = getattr(config, "overtime_prompt_timeout_seconds", 90)
                return max(30, min(600, value))

            tk.Label(
                interval_frame,
                text=t("unit_minutes", lang),
                font=("Segoe UI", 8),
                bg="#f8f9fa",
                fg="#4b5563",
            ).pack(side="left", padx=(4, 0))

            # Retention policy: aggregate into daily_summary before purging.
            retention_frame = tk.Frame(tab_features, bg="#f8f9fa")
            retention_frame.pack(fill="x", padx=15, pady=(10, 2))

            tk.Label(
                retention_frame,
                text=t("lbl_retention_days", lang),
                font=("Segoe UI", 9),
                bg="#f8f9fa",
                anchor="w",
            ).pack(side="left")

            retention_spin = tk.Spinbox(
                retention_frame,
                from_=0,
                to=3650,
                width=6,
                font=("Segoe UI", 9),
            )
            retention_spin.delete(0, tk.END)
            retention_spin.insert(0, str(getattr(config, "retention_days", 90)))
            retention_spin.pack(side="left", padx=6)

            backup_keep_frame = tk.Frame(tab_features, bg="#f8f9fa")
            backup_keep_frame.pack(fill="x", padx=15, pady=(4, 2))
            tk.Label(
                backup_keep_frame,
                text=t("lbl_backup_keep", lang),
                font=("Segoe UI", 9),
                bg="#f8f9fa",
                anchor="w",
            ).pack(side="left")
            backup_keep_spin = tk.Spinbox(
                backup_keep_frame,
                from_=1,
                to=52,
                width=6,
                font=("Segoe UI", 9),
            )
            backup_keep_spin.delete(0, tk.END)
            backup_keep_spin.insert(0, str(getattr(config, "backup_keep_count", 8)))
            backup_keep_spin.pack(side="left", padx=6)

            tk.Label(
                tab_features,
                text=t("retention_hint", lang),
                font=("Segoe UI", 7),
                bg="#f8f9fa",
                fg="#6b7280",
                wraplength=540,
                justify="left",
            ).pack(fill="x", padx=35, pady=(0, 6))

            # Local (offline) update source
            update_frame = tk.LabelFrame(
                tab_features,
                text=f" {t('sec_update', lang)} ",
                bg="#f8f9fa",
                font=("Segoe UI", 9, "bold"),
                padx=10,
                pady=6,
            )
            update_frame.pack(fill="x", padx=15, pady=6)

            tk.Label(
                update_frame,
                text=t("lbl_update_source", lang),
                font=("Segoe UI", 8),
                bg="#f8f9fa",
                anchor="w",
            ).grid(row=0, column=0, sticky="w")

            update_entry = tk.Entry(update_frame, font=("Segoe UI", 9), width=48)
            update_entry.insert(0, getattr(config, "update_source_path", ""))
            update_entry.grid(row=1, column=0, sticky="we", pady=(2, 4))

            tk.Label(
                update_frame,
                text=t("update_source_hint", lang),
                font=("Segoe UI", 7),
                bg="#f8f9fa",
                fg="#6b7280",
                wraplength=520,
                justify="left",
            ).grid(row=2, column=0, sticky="w")

            # CSV export (daily summary + sessions + app usage + projects)
            export_frame = tk.LabelFrame(
                tab_features,
                text=f" {t('sec_export', lang)} ",
                bg="#f8f9fa",
                font=("Segoe UI", 9, "bold"),
                padx=10,
                pady=6,
            )
            export_frame.pack(fill="x", padx=15, pady=6)

            tk.Label(
                export_frame,
                text=t("export_csv_hint", lang),
                font=("Segoe UI", 7),
                bg="#f8f9fa",
                fg="#6b7280",
                wraplength=520,
                justify="left",
            ).pack(anchor="w")

            make_selection_widget(
                export_frame, "Checkbutton",
                text=t("csv_excel_safe", lang),
                variable=csv_excel_safe_var,
                lang=lang,
                font=("Segoe UI", 7),
            ).pack(anchor="w", pady=(4, 0))

            def export_csv_from_settings() -> None:
                if db is None:
                    return

                def on_submit(start_d: date | None, end_d: date | None) -> None:
                    if start_d is None or end_d is None:
                        return  # Tk-unavailable fallback (BUG-05)
                    try:
                        from time_tracker.reporting.reporter import export_csv_bundle

                        files = export_csv_bundle(
                            db, config, start_d, end_d,
                            open_folder=True,
                            excel_safe=csv_excel_safe_var.get(),
                        )
                        messagebox.showinfo(
                            t("sec_export", lang),
                            t("export_csv_success", lang, count=len(files)),
                            parent=root,
                        )
                    except Exception as exc:
                        logger.error("CSV export from settings failed: %s", exc)
                        messagebox.showerror(
                            t("sec_export", lang),
                            t("export_csv_fail", lang, error=str(exc)),
                            parent=root,
                        )

                today = date.today()
                show_date_range_picker_dialog_async(
                    on_submit=on_submit,
                    lang=lang,
                    default_start=today.replace(day=1),
                    default_end=today,
                )

            tk.Button(
                export_frame,
                text=t("export_csv_now", lang),
                font=("Segoe UI", 8),
                bg="#e0f2fe",
                command=export_csv_from_settings,
                state="normal" if db is not None else "disabled",
            ).pack(anchor="w", pady=(6, 2))

            # Tab 4: Projects (optional tags on tracked time)
            tab_projects, tab_projects_outer = make_scrollable(notebook)
            notebook.add(tab_projects_outer, text=t("tab_projects", lang))

            tk.Label(
                tab_projects,
                text=t("projects_desc", lang),
                font=("Segoe UI", 8),
                bg="#f8f9fa",
                fg="#6c757d",
                wraplength=540,
                justify="left",
            ).pack(anchor="w", padx=15, pady=(10, 6))

            projects_state: list[dict[str, Any]] = []
            show_archived_var = tk.BooleanVar(value=True)
            project_listbox = tk.Listbox(tab_projects, font=("Segoe UI", 9), selectmode="single", height=10)
            project_listbox.pack(fill="both", expand=True, padx=15, pady=4)

            def selected_project() -> dict[str, Any] | None:
                selection = project_listbox.curselection()
                if not selection or selection[0] >= len(projects_state):
                    return None
                return projects_state[selection[0]]

            def fill_entry_from_project(item: dict[str, Any] | None) -> None:
                project_entry.delete(0, tk.END)
                if item is None:
                    return
                project_entry.insert(0, str(item["name"]))
                project_entry.icursor(tk.END)

            def update_project_buttons() -> None:
                item = selected_project()
                is_archived = item is not None and int(item.get("archived") or 0) == 1
                rename_btn.config(state="normal" if item is not None else "disabled")
                archive_btn.config(state="normal" if item is not None and not is_archived else "disabled")
                restore_btn.config(state="normal" if is_archived else "disabled")

            def refresh_projects_list(select_id: int | None = None) -> None:
                nonlocal projects_state
                project_listbox.delete(0, tk.END)
                projects_state = []
                if db is None:
                    project_listbox.insert(tk.END, t("projects_empty", lang))
                    update_project_buttons()
                    return
                try:
                    projects_state = db.list_projects(include_archived=bool(show_archived_var.get()))
                except Exception as exc:
                    logger.error("Could not load projects: %s", exc)
                    projects_state = []
                if not projects_state:
                    project_listbox.insert(tk.END, t("projects_empty", lang))
                    update_project_buttons()
                    return
                target_index: int | None = None
                for index, item in enumerate(projects_state):
                    label = str(item["name"])
                    if int(item.get("archived") or 0) == 1:
                        label += " " + t("project_archived_tag", lang)
                    project_listbox.insert(tk.END, label)
                    if int(item.get("archived") or 0) == 1:
                        project_listbox.itemconfig(index, foreground="#9ca3af")
                    if select_id is not None and int(item["id"]) == int(select_id):
                        target_index = index
                if target_index is not None:
                    project_listbox.selection_set(target_index)
                    project_listbox.see(target_index)
                    fill_entry_from_project(projects_state[target_index])
                update_project_buttons()

            def on_project_selected(event: Any = None) -> None:
                fill_entry_from_project(selected_project())
                update_project_buttons()

            project_listbox.bind("<<ListboxSelect>>", on_project_selected)

            proj_form = tk.Frame(tab_projects, bg="#f8f9fa")
            proj_form.pack(fill="x", padx=15, pady=6)

            tk.Label(
                proj_form,
                text=t("project_new_label", lang),
                font=("Segoe UI", 8),
                bg="#f8f9fa",
            ).pack(side="left")

            project_entry = tk.Entry(proj_form, font=("Segoe UI", 9), width=28)
            project_entry.pack(side="left", padx=6)

            def add_project() -> None:
                if db is None:
                    return
                name = project_entry.get().strip()
                if not name:
                    messagebox.showerror("Error", t("project_err_name", lang), parent=root)
                    return
                try:
                    new_id = db.add_project(name)
                except ValueError:
                    messagebox.showerror("Error", t("project_err_exists", lang), parent=root)
                    return
                project_entry.delete(0, tk.END)
                refresh_projects_list(select_id=new_id)

            def rename_project() -> None:
                if db is None:
                    return
                item = selected_project()
                if item is None:
                    return
                new_name = project_entry.get().strip()
                if not new_name:
                    messagebox.showerror("Error", t("project_err_name", lang), parent=root)
                    return
                try:
                    db.rename_project(int(item["id"]), new_name)
                except ValueError:
                    messagebox.showerror("Error", t("project_err_exists", lang), parent=root)
                    return
                refresh_projects_list(select_id=int(item["id"]))

            def archive_project() -> None:
                if db is None:
                    return
                item = selected_project()
                if item is None:
                    return
                db.set_project_archived(int(item["id"]), archived=True)
                current = db.get_setting("current_project_id")
                if current and current == str(item["id"]):
                    db.set_setting("current_project_id", "")
                refresh_projects_list(select_id=int(item["id"]) if show_archived_var.get() else None)
                messagebox.showinfo(
                    t("tab_projects", lang),
                    t("project_archived_msg", lang, name=str(item["name"])),
                    parent=root,
                )

            def restore_project() -> None:
                if db is None:
                    return
                item = selected_project()
                if item is None or int(item.get("archived") or 0) == 0:
                    return
                db.set_project_archived(int(item["id"]), archived=False)
                refresh_projects_list(select_id=int(item["id"]))
                messagebox.showinfo(
                    t("tab_projects", lang),
                    t("project_restored_msg", lang, name=str(item["name"])),
                    parent=root,
                )

            def on_entry_return(event: Any = None) -> str:
                if selected_project() is not None:
                    rename_project()
                else:
                    add_project()
                return "break"

            project_entry.bind("<Return>", on_entry_return)

            tk.Button(proj_form, text=t("btn_add_project", lang), command=add_project, font=("Segoe UI", 8), bg="#d1fae5").pack(side="left", padx=3)
            rename_btn = tk.Button(proj_form, text=t("btn_rename_project", lang), command=rename_project, font=("Segoe UI", 8), bg="#e9ecef")
            rename_btn.pack(side="left", padx=3)

            proj_actions = tk.Frame(tab_projects, bg="#f8f9fa")
            proj_actions.pack(fill="x", padx=15, pady=(0, 6))

            make_selection_widget(
                proj_actions, "Checkbutton",
                text=t("chk_show_archived", lang),
                variable=show_archived_var,
                lang=lang,
                font=("Segoe UI", 8),
                command=lambda: refresh_projects_list(),
            ).pack(side="left")

            archive_btn = tk.Button(proj_actions, text=t("btn_archive_selected", lang), command=archive_project, font=("Segoe UI", 8), bg="#fde68a")
            archive_btn.pack(side="left", padx=3)
            restore_btn = tk.Button(proj_actions, text=t("btn_restore_selected", lang), command=restore_project, font=("Segoe UI", 8), bg="#bfdbfe")
            restore_btn.pack(side="left", padx=3)

            tk.Label(
                tab_projects,
                text=t("project_select_hint", lang),
                font=("Segoe UI", 7),
                bg="#f8f9fa",
                fg="#6b7280",
            ).pack(anchor="w", padx=15, pady=(0, 8))

            refresh_projects_list()

            # Save & Cancel Buttons
            bottom_frame = tk.Frame(root, bg="#f8f9fa")
            bottom_frame.pack(fill="x", pady=15, padx=20)

            def save_all() -> None:
                s_val = start_entry.get().strip()
                e_val = end_entry.get().strip()
                try:
                    t_val = float(target_entry.get().strip())
                    w_val = float(weekly_entry.get().strip())
                    start_dt = datetime.strptime(s_val, "%H:%M")
                    end_dt = datetime.strptime(e_val, "%H:%M")
                except ValueError:
                    messagebox.showerror(
                        "Invalid Input",
                        "Please verify times (HH:MM) and target hours.",
                        parent=root,
                    )
                    return

                # D4: the working window must move forward in time.
                if end_dt <= start_dt:
                    messagebox.showerror(
                        t("settings_title", lang),
                        t("err_work_end_before_start", lang),
                        parent=root,
                    )
                    return

                selected_days = [d for d, v in day_vars.items() if v.get()]
                if not selected_days:
                    messagebox.showerror("Error", "Please select at least one work day.", parent=root)
                    return

                current_holidays = sorted(set(holiday_dates))

                try:
                    act_int = max(1, min(60, int(interval_spin.get().strip())))
                except ValueError:
                    act_int = 3

                try:
                    retention_val = max(0, int(retention_spin.get().strip()))
                except ValueError:
                    retention_val = max(0, int(getattr(config, "retention_days", 90)))

                try:
                    backup_keep_val = max(1, min(52, int(backup_keep_spin.get().strip())))
                except ValueError:
                    backup_keep_val = max(1, int(getattr(config, "backup_keep_count", 8)))

                updated_cfg = Config(
                    work_start=s_val,
                    work_end=e_val,
                    work_days=selected_days,
                    daily_target_hours=t_val,
                    weekly_target_hours=w_val,
                    grace_period_seconds=config.grace_period_seconds,
                    tick_interval_seconds=config.tick_interval_seconds,
                    autostart_enabled=autostart_var.get(),
                    language=lang_var.get(),
                    holidays=current_holidays,
                    enable_end_of_day_notification=eod_var.get(),
                    enable_meeting_reminder=meet_remind_var.get(),
                    meeting_reminder_minutes=config.meeting_reminder_minutes,
                    enable_stretch_reminder=stretch_remind_var.get(),
                    stretch_reminder_minutes=config.stretch_reminder_minutes,
                    enable_auto_weekly_export=auto_export_var.get(),
                    enable_auto_weekly_backup=auto_backup_var.get(),
                    enable_app_tracking=app_tracking_var.get(),
                    enable_retention_cleanup=getattr(config, "enable_retention_cleanup", True),
                    retention_days=retention_val,
                    active_check_enabled=active_check_var.get(),
                    active_check_interval_min=act_int,
                    update_source_path=update_entry.get().strip(),
                    backup_keep_count=backup_keep_val,
                    overtime_prompt_timeout_seconds=_ot_timeout_value(),
                    enable_dashboard_server=getattr(config, "enable_dashboard_server", True),
                    enable_smart_meeting_detection=getattr(
                        config, "enable_smart_meeting_detection", True
                    ),
                    smart_meeting_cooldown_minutes=getattr(
                        config, "smart_meeting_cooldown_minutes", 20
                    ),
                    log_level=getattr(config, "log_level", "INFO"),
                    csv_excel_safe=csv_excel_safe_var.get(),
                )

                updated_cfg.save()
                root.destroy()
                on_save(updated_cfg)

            tk.Button(
                bottom_frame,
                text=t("btn_save_settings", lang),
                font=("Segoe UI", 10, "bold"),
                bg="#28a745",
                fg="#ffffff",
                relief="flat",
                command=save_all,
            ).pack(side="right", padx=5)

            tk.Button(
                bottom_frame,
                text=t("btn_cancel", lang),
                font=("Segoe UI", 10),
                bg="#e9ecef",
                relief="flat",
                command=root.destroy,
            ).pack(side="right", padx=5)

            apply_rtl(root, lang)
            root.lift()

        except Exception as exc:
            logger.error("Error in settings dialog: %s", exc)

    dispatch_to_ui(_show)


# =====================================================================
# 5b. Local Update Available Dialog (offline update source)
# =====================================================================

def show_update_dialog_async(
    version: str,
    notes: str,
    on_install: Callable[[], None],
    on_later: Callable[[], None] | None = None,
    lang: str = "en",
) -> None:
    """Offer to run the newer installer found in the update source."""
    def _show() -> None:
        try:
            master = ensure_ui_thread()
            if not _tk_available or master is None:
                raise RuntimeError("Tkinter unavailable")
            root = tk.Toplevel(master)
            root.title(t("update_dialog_title", lang))
            root.resizable(False, False)
            root.attributes("-topmost", True)

            fit_dialog(root, 480, 300)
            root.configure(bg="#ffffff")

            header = tk.Frame(root, bg="#0f2b48", padx=20, pady=14)
            header.pack(fill="x")
            tk.Label(
                header,
                text="⬆ " + t("update_dialog_title", lang),
                font=("Segoe UI", 12, "bold"),
                bg="#0f2b48",
                fg="#ffffff",
            ).pack(anchor="w")

            card = tk.Frame(root, bg="#ffffff", padx=20, pady=15)
            card.pack(fill="both", expand=True)

            tk.Label(
                card,
                text=t("update_available_msg", lang, version=version),
                font=("Segoe UI", 9, "bold"),
                bg="#ffffff",
                fg="#1f2937",
                wraplength=420,
                justify="left",
            ).pack(anchor="w")

            if notes:
                tk.Label(
                    card,
                    text=t("update_notes_label", lang),
                    font=("Segoe UI", 8, "bold"),
                    bg="#ffffff",
                    fg="#374151",
                ).pack(anchor="w", pady=(10, 2))
                notes_box = tk.Text(card, height=5, wrap="word", font=("Segoe UI", 8), relief="flat", bg="#f8fafc")
                notes_box.insert("1.0", notes)
                notes_box.configure(state="disabled")
                notes_box.pack(fill="both", expand=True)

            btn_frame = tk.Frame(root, bg="#ffffff", padx=20, pady=10)
            btn_frame.pack(fill="x")

            def do_install() -> None:
                root.destroy()
                on_install()

            def do_later() -> None:
                root.destroy()
                if on_later is not None:
                    on_later()

            tk.Button(
                btn_frame,
                text=t("update_btn_install", lang),
                font=("Segoe UI", 9, "bold"),
                bg="#16a34a",
                fg="#ffffff",
                relief="flat",
                padx=14,
                pady=5,
                command=do_install,
            ).pack(side="right", padx=4)

            tk.Button(
                btn_frame,
                text=t("update_btn_later", lang),
                font=("Segoe UI", 9),
                bg="#e2e8f0",
                fg="#334155",
                relief="flat",
                padx=12,
                pady=5,
                command=do_later,
            ).pack(side="right", padx=4)

            root.protocol("WM_DELETE_WINDOW", do_later)

            apply_rtl(root, lang)
            root.lift()

        except Exception as exc:
            logger.error("Error showing update dialog: %s", exc)

    dispatch_to_ui(_show)


def show_download_progress_dialog(
    info: Any,
    on_success: Callable[[Path], None],
    lang: str = "en",
) -> None:
    """Display a smooth modal download progress dialog with progress bar."""
    import tempfile
    from time_tracker.system.updater import download_installer

    def _show() -> None:
        try:
            master = ensure_ui_thread()
            if not _tk_available or master is None:
                raise RuntimeError("Tkinter unavailable")
            root = tk.Toplevel(master)
            root.title(t("update_download_title", lang))
            root.resizable(False, False)
            root.attributes("-topmost", True)

            fit_dialog(root, 480, 240)
            root.configure(bg="#ffffff")

            header = tk.Frame(root, bg="#0f2b48", padx=20, pady=14)
            header.pack(fill="x")
            tk.Label(
                header,
                text="⬇ " + t("update_download_title", lang) + f" — v{info.version}",
                font=("Segoe UI", 12, "bold"),
                bg="#0f2b48",
                fg="#ffffff",
            ).pack(anchor="w")

            card = tk.Frame(root, bg="#ffffff", padx=20, pady=15)
            card.pack(fill="both", expand=True)

            status_lbl = tk.Label(
                card,
                text=t("update_downloading", lang, version=info.version),
                font=("Segoe UI", 9, "bold"),
                bg="#ffffff",
                fg="#1f2937",
                anchor="w",
            )
            status_lbl.pack(fill="x", pady=(0, 10))

            prog_bar = ttk.Progressbar(card, orient="horizontal", mode="determinate", length=440)
            prog_bar.pack(fill="x", pady=(0, 8))

            details_lbl = tk.Label(
                card,
                text="0.0 MB / ... (0%)",
                font=("Segoe UI", 8),
                bg="#ffffff",
                fg="#6b7280",
                anchor="w",
            )
            details_lbl.pack(fill="x")

            btn_frame = tk.Frame(root, bg="#ffffff", padx=20, pady=10)
            btn_frame.pack(fill="x")

            cancel_btn = tk.Button(
                btn_frame,
                text=t("btn_cancel", lang),
                font=("Segoe UI", 9),
                bg="#e2e8f0",
                fg="#334155",
                relief="flat",
                padx=14,
                pady=5,
            )
            cancel_btn.pack(side="right")

            cancelled = False

            def do_cancel() -> None:
                nonlocal cancelled
                cancelled = True
                root.destroy()

            cancel_btn.configure(command=do_cancel)
            root.protocol("WM_DELETE_WINDOW", do_cancel)

            # SEC-03: never build a filesystem path from a raw manifest version.
            dest_path = (
                Path(tempfile.gettempdir())
                / f"TimeTracker_Setup_{safe_version_slug(info.version)}_{uuid.uuid4().hex[:8]}.exe"
            )

            def update_progress(downloaded: int, total: int) -> None:
                if cancelled:
                    return
                try:
                    if not root.winfo_exists():
                        return
                    if total > 0:
                        pct = min(100.0, (downloaded / total) * 100)
                        prog_bar["value"] = pct
                        dl_mb = downloaded / (1024 * 1024)
                        tot_mb = total / (1024 * 1024)
                        details_lbl.configure(text=f"{dl_mb:.1f} MB / {tot_mb:.1f} MB ({int(pct)}%)")
                    else:
                        dl_mb = downloaded / (1024 * 1024)
                        details_lbl.configure(text=f"{dl_mb:.1f} MB")
                except Exception:
                    pass

            def worker() -> None:
                ok = download_installer(
                    download_url=info.download_url,
                    dest_path=dest_path,
                    expected_sha256=info.sha256,
                    progress_callback=lambda d, t: root.after(0, update_progress, d, t),
                    cancel_flag=lambda: cancelled,
                )
                if cancelled:
                    return

                def on_done() -> None:
                    try:
                        if not root.winfo_exists():
                            return
                        if ok:
                            status_lbl.configure(
                                text=t("update_download_complete", lang),
                                fg="#16a34a",
                            )
                            prog_bar["value"] = 100
                            cancel_btn.configure(state="disabled")
                            root.after(800, lambda: [root.destroy(), on_success(dest_path)])
                        else:
                            status_lbl.configure(
                                text=t("update_download_failed", lang),
                                fg="#dc2626",
                            )
                            cancel_btn.configure(text=t("btn_close", lang), command=root.destroy)
                    except Exception:
                        pass

                try:
                    root.after(0, on_done)
                except Exception:
                    pass

            threading.Thread(target=worker, daemon=True).start()

            apply_rtl(root, lang)
            root.lift()

        except Exception as exc:
            logger.error("Error showing download dialog: %s", exc)

    dispatch_to_ui(_show)


def show_info_dialog_async(
    title: str,
    message: str,
    lang: str = "en",
    is_success: bool = True,
) -> None:
    """Show a neat modal information or notification dialog with RTL support."""
    def _show() -> None:
        try:
            master = ensure_ui_thread()
            if not _tk_available or master is None:
                raise RuntimeError("Tkinter unavailable")
            root = tk.Toplevel(master)
            root.title(title)
            root.resizable(False, False)
            root.attributes("-topmost", True)

            fit_dialog(root, 440, 220)
            root.configure(bg="#ffffff")

            header_bg = "#0f2b48" if is_success else "#991b1b"
            header = tk.Frame(root, bg=header_bg, padx=20, pady=12)
            header.pack(fill="x")

            icon = "✓ " if is_success else "⚠ "
            tk.Label(
                header,
                text=icon + title,
                font=("Segoe UI", 11, "bold"),
                bg=header_bg,
                fg="#ffffff",
            ).pack(anchor="w")

            card = tk.Frame(root, bg="#ffffff", padx=20, pady=16)
            card.pack(fill="both", expand=True)

            align = "right" if lang == "ar" else "left"
            anchor = "e" if lang == "ar" else "w"

            tk.Label(
                card,
                text=message,
                font=("Segoe UI", 9),
                bg="#ffffff",
                fg="#1f2937",
                wraplength=390,
                justify=align,
                anchor=anchor,
            ).pack(fill="both", expand=True)

            btn_frame = tk.Frame(root, bg="#f8fafc", padx=16, pady=10)
            btn_frame.pack(fill="x")

            btn = tk.Button(
                btn_frame,
                text=t("btn_ok", lang),
                font=("Segoe UI", 9, "bold"),
                bg="#0f2b48",
                fg="#ffffff",
                relief="flat",
                padx=20,
                pady=4,
                command=root.destroy,
            )
            btn.pack(side="right")

            apply_rtl(root, lang)
            root.lift()

        except Exception as exc:
            logger.error("Error showing info dialog: %s", exc)

    dispatch_to_ui(_show)


# =====================================================================
# 6. About Dialog (BUSSMA_TTS — Bussma)
# =====================================================================

def show_about_dialog_async(version: str = "1.2.0", lang: str = "en") -> None:
    """Show About BUSSMA_TTS dialog asynchronously."""
    def _show() -> None:
        try:
            master = ensure_ui_thread()
            if not _tk_available or master is None:
                raise RuntimeError("Tkinter unavailable")
            root = tk.Toplevel(master)
            root.title(t("about_title", lang))
            root.resizable(False, False)
            root.attributes("-topmost", True)

            fit_dialog(root, 580, 520)
            root.configure(bg="#ffffff")

            header_frame = tk.Frame(root, bg="#0f2b48", padx=20, pady=16)
            header_frame.pack(fill="x")

            title_label = tk.Label(
                header_frame,
                text="BUSSMA_TTS",
                font=("Segoe UI", 16, "bold"),
                bg="#0f2b48",
                fg="#ffffff",
            )
            title_label.pack(anchor="w")

            sub_label = tk.Label(
                header_frame,
                text=t("about_company_sub", lang, version=version),
                font=("Segoe UI", 9),
                bg="#0f2b48",
                fg="#94a3b8",
            )
            sub_label.pack(anchor="w", pady=(2, 0))

            card_frame = tk.Frame(root, bg="#ffffff", padx=20, pady=15)
            card_frame.pack(fill="both", expand=True)

            text_area = tk.Text(
                card_frame,
                wrap="word",
                font=("Segoe UI", 9),
                bg="#f8fafc",
                fg="#334155",
                relief="flat",
                padx=12,
                pady=12,
            )
            text_area.pack(fill="both", expand=True)

            if lang == "ar":
                about_text = (
                    "شركة بصمة (Bussma) — رائدة التحول الرقمي وتطوير الحلول البرمجية المتكاملة.\n\n"
                    "رؤيتنا:\n"
                    "تمكين المؤسسات والشركات من إدارة الوقت والموارد البشرية بأعلى درجات الكفاءة والشفافية.\n\n"
                    "مبادئ نظام BUSSMA_TTS:\n"
                    "• الخصوصية أولاً (Privacy by Design): بياناتك مشفرة ومحفوظة محلياً 100% على جهازك.\n"
                    "• لا يتم تسجيل أي محتوى حساس، ولا صور شاشة، ولا تسجيل ضغطات مفاتيح.\n"
                    "• احتساب ذكي ومحايد لساعات العمل والاجتماعات والاستراحات والعمل الإضافي.\n\n"
                    "الموقع الرسمي: https://www.bussma.com\n"
                    "جميع الحقوق محفوظة © شركة بصمة."
                )
            else:
                about_text = (
                    "Bussma — Pioneering digital transformation and integrated enterprise software.\n\n"
                    "Our Vision:\n"
                    "Empowering modern teams to manage their work hours with absolute transparency, "
                    "accuracy, and mutual trust.\n\n"
                    "Core Principles of BUSSMA_TTS:\n"
                    "• Privacy-First: 100% local database stored strictly on your workstation.\n"
                    "• Zero keystroke recording, zero screenshots, zero URL tracking.\n"
                    "• Trailing grace activity model ensuring fair and effortless time attribution.\n\n"
                    "Official Website: https://www.bussma.com\n"
                    "Copyright © Bussma. All rights reserved."
                )

            text_area.insert("1.0", about_text)
            text_area.configure(state="disabled")

            btn_frame = tk.Frame(root, bg="#ffffff", padx=20, pady=10)
            btn_frame.pack(fill="x")

            close_btn = tk.Button(
                btn_frame,
                text=t("about_btn_close", lang),
                font=("Segoe UI", 9, "bold"),
                bg="#0f2b48",
                fg="#ffffff",
                relief="flat",
                padx=16,
                pady=6,
                command=root.destroy,
            )
            close_btn.pack(side="right")

            apply_rtl(root, lang)
            root.lift()

        except Exception as exc:
            logger.error("Error showing about dialog: %s", exc)

    dispatch_to_ui(_show)


# =====================================================================
# 7. Custom Date Range Picker Dialog
# =====================================================================

def show_date_range_picker_dialog_async(
    on_submit: Callable[[date, date], None],
    lang: str = "en",
    default_start: date | None = None,
    default_end: date | None = None,
    title: str | None = None,
    button_text: str | None = None,
    icon: str = "📊",
) -> None:
    """Non-blocking dialog to select a custom date range for report export."""
    def _show() -> None:
        try:
            master = ensure_ui_thread()
            if not _tk_available or master is None:
                raise RuntimeError("Tkinter unavailable")
            root = tk.Toplevel(master)
            dlg_title = title or t("custom_export_title", lang)
            root.title(dlg_title)
            root.resizable(False, False)
            root.attributes("-topmost", True)

            fit_dialog(root, 500, 390)
            root.configure(bg="#f8fafc")

            header_frame = tk.Frame(root, bg="#0f2b48", padx=20, pady=14)
            header_frame.pack(fill="x")

            title_lbl = tk.Label(
                header_frame,
                text=f"{icon} {dlg_title}",
                font=("Segoe UI", 12, "bold"),
                bg="#0f2b48",
                fg="#ffffff",
            )
            title_lbl.pack(anchor="w")

            card = tk.Frame(root, bg="#ffffff", padx=20, pady=15, relief="groove", bd=1)
            card.pack(fill="both", expand=True, padx=20, pady=15)

            today = date.today()
            def_start = default_start or (today - timedelta(days=6))
            def_end = default_end or today

            tk.Label(card, text=t("custom_export_start", lang), font=("Segoe UI", 9, "bold"), bg="#ffffff").grid(row=0, column=0, sticky="w", pady=6)
            start_entry = tk.Entry(card, font=("Segoe UI", 10), width=18, relief="solid", bd=1)
            start_entry.insert(0, def_start.isoformat())
            start_entry.grid(row=0, column=1, sticky="w", pady=6, padx=10)

            tk.Label(card, text=t("custom_export_end", lang), font=("Segoe UI", 9, "bold"), bg="#ffffff").grid(row=1, column=0, sticky="w", pady=6)
            end_entry = tk.Entry(card, font=("Segoe UI", 10), width=18, relief="solid", bd=1)
            end_entry.insert(0, def_end.isoformat())
            end_entry.grid(row=1, column=1, sticky="w", pady=6, padx=10)

            # Quick presets row
            preset_frame = tk.Frame(card, bg="#ffffff")
            preset_frame.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 4))

            tk.Label(preset_frame, text=t("custom_export_presets", lang), font=("Segoe UI", 8, "bold"), bg="#ffffff", fg="#475569").pack(side="left", padx=(0, 6))

            err_lbl = tk.Label(card, text="", font=("Segoe UI", 8), bg="#ffffff", fg="#dc2626")
            err_lbl.grid(row=3, column=0, columnspan=2, sticky="w", pady=4)

            def _set_range(s: date, e: date) -> None:
                start_entry.delete(0, tk.END)
                start_entry.insert(0, s.isoformat())
                end_entry.delete(0, tk.END)
                end_entry.insert(0, e.isoformat())
                err_lbl.config(text="")

            first_this_month = today.replace(day=1)
            last_day_prev_month = first_this_month - timedelta(days=1)
            first_day_prev_month = last_day_prev_month.replace(day=1)

            presets = [
                (t("export_today", lang), today, today),
                (t("export_7days", lang), today - timedelta(days=6), today),
                (t("export_month", lang), first_this_month, today),
                (t("export_last_month", lang), first_day_prev_month, last_day_prev_month),
            ]

            for p_name, p_start, p_end in presets:
                p_btn = tk.Button(
                    preset_frame,
                    text=p_name,
                    font=("Segoe UI", 8),
                    bg="#f1f5f9",
                    fg="#1e293b",
                    relief="solid",
                    bd=1,
                    cursor="hand2",
                    padx=6,
                    pady=2,
                    command=lambda s=p_start, e=p_end: _set_range(s, e),
                )
                p_btn.pack(side="left", padx=2)

            def on_confirm() -> None:
                s_str = start_entry.get().strip()
                e_str = end_entry.get().strip()
                try:
                    sd = datetime.strptime(s_str, "%Y-%m-%d").date()
                    ed = datetime.strptime(e_str, "%Y-%m-%d").date()
                    if sd > ed:
                        err_lbl.config(text=t("custom_export_err_date", lang))
                        return
                    root.destroy()
                    on_submit(sd, ed)
                except ValueError:
                    err_lbl.config(text=t("custom_export_err_date", lang))

            btn_frame = tk.Frame(root, bg="#f8fafc", padx=20, pady=10)
            btn_frame.pack(fill="x", side="bottom")

            submit_btn = tk.Button(
                btn_frame,
                text=button_text or t("custom_export_btn", lang),
                font=("Segoe UI", 9, "bold"),
                bg="#16a34a",
                fg="#ffffff",
                relief="flat",
                cursor="hand2",
                padx=16,
                pady=5,
                command=on_confirm,
            )
            submit_btn.pack(side="left")

            cancel_btn = tk.Button(
                btn_frame,
                text=t("custom_export_cancel", lang),
                font=("Segoe UI", 9),
                bg="#e2e8f0",
                fg="#334155",
                relief="flat",
                cursor="hand2",
                padx=12,
                pady=5,
                command=root.destroy,
            )
            cancel_btn.pack(side="right")

            apply_rtl(root, lang)
            root.lift()

        except Exception as exc:
            logger.error("Error showing custom export dialog: %s", exc)

    dispatch_to_ui(
        _show,
        fallback=lambda: (
            _unavailable_notice("Dialog unavailable — custom export cancelled."),
            on_submit(None, None),
        ),
    )


# =====================================================================
# 8. First-Run Transparency & Privacy Consent Dialog
# =====================================================================

def show_consent_dialog_async(
    on_accept: Callable[[], None],
    on_decline: Callable[[], None],
    lang: str = "en",
) -> None:
    """Show transparency & privacy consent dialog on first run."""
    def _show() -> None:
        answered = False
        try:
            master = ensure_ui_thread()
            win = tk.Toplevel(master)
            win.title(t("consent_title", lang))
            win.resizable(False, False)
            win.attributes("-topmost", True)

            fit_dialog(win, 580, 540)
            win.configure(bg="#f8f9fa")

            # Top Header Banner
            header_frame = tk.Frame(win, bg="#0f2b48", padx=20, pady=16)
            header_frame.pack(fill="x")

            tk.Label(
                header_frame,
                text=t("consent_header", lang),
                font=("Segoe UI", 12, "bold"),
                bg="#0f2b48",
                fg="#ffffff",
            ).pack(anchor="w" if lang == "en" else "e")

            tk.Label(
                header_frame,
                text=t("consent_sub", lang),
                font=("Segoe UI", 9),
                bg="#0f2b48",
                fg="#94a3b8",
            ).pack(anchor="w" if lang == "en" else "e", pady=(4, 0))

            # Localised Arabic copy is longer than English; keep the privacy
            # text reachable with the wheel/keyboard instead of clipping it.
            content_frame, content_outer = make_scrollable(win, bg="#f8f9fa")
            content_outer.pack(fill="both", expand=True, padx=20, pady=12)

            # Tracked Box (Green accent)
            box_tracked = tk.LabelFrame(
                content_frame,
                text=f" {t('consent_tracked_title', lang)} ",
                font=("Segoe UI", 9, "bold"),
                bg="#ffffff",
                fg="#047857",
                padx=12,
                pady=8,
                relief="groove",
            )
            box_tracked.pack(fill="x", pady=6)

            tk.Label(
                box_tracked,
                text=t("consent_tracked_body", lang),
                font=("Segoe UI", 9),
                bg="#ffffff",
                fg="#1f2937",
                justify="left" if lang == "en" else "right",
                anchor="w" if lang == "en" else "e",
            ).pack(fill="x")

            # Never Tracked Box (Red/Slate accent)
            box_never = tk.LabelFrame(
                content_frame,
                text=f" {t('consent_never_tracked_title', lang)} ",
                font=("Segoe UI", 9, "bold"),
                bg="#ffffff",
                fg="#b91c1c",
                padx=12,
                pady=8,
                relief="groove",
            )
            box_never.pack(fill="x", pady=6)

            tk.Label(
                box_never,
                text=t("consent_never_tracked_body", lang),
                font=("Segoe UI", 9),
                bg="#ffffff",
                fg="#1f2937",
                justify="left" if lang == "en" else "right",
                anchor="w" if lang == "en" else "e",
            ).pack(fill="x")

            # Buttons
            btn_frame = tk.Frame(win, bg="#f8f9fa", padx=20, pady=12)
            btn_frame.pack(fill="x")

            def accept() -> None:
                nonlocal answered
                if answered:
                    return
                answered = True
                win.destroy()
                on_accept()

            def decline() -> None:
                nonlocal answered
                if answered:
                    return
                answered = True
                win.destroy()
                on_decline()

            win.protocol("WM_DELETE_WINDOW", decline)
            # Keyboard a11y: Enter activates the focused "agree" button, Escape
            # is the safe decline (same as closing the window).
            win.bind("<Escape>", lambda _event: decline())

            agree_btn = tk.Button(
                btn_frame,
                text=t("consent_agree_btn", lang),
                font=("Segoe UI", 10, "bold"),
                bg="#059669",
                fg="#ffffff",
                relief="flat",
                padx=16,
                pady=6,
                cursor="hand2",
                command=accept,
            )
            agree_btn.pack(side="left" if lang == "en" else "right", padx=6)

            exit_btn = tk.Button(
                btn_frame,
                text=t("consent_exit_btn", lang),
                font=("Segoe UI", 9),
                bg="#e2e8f0",
                fg="#334155",
                relief="flat",
                padx=12,
                pady=6,
                cursor="hand2",
                command=decline,
            )
            exit_btn.pack(side="left" if lang == "en" else "right", padx=6)

            apply_rtl(win, lang)
            win.lift()
            win.focus_force()
            agree_btn.focus_set()

        except Exception as exc:
            logger.error("Error in consent dialog: %s", exc)
            if not answered:
                on_decline()

    dispatch_to_ui(_show)
