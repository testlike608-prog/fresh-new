import time
import queue
import threading
import logging

from barcode_utils import normalize_barcode

log = logging.getLogger("scanner")

# --- Keyboard: optional ---
# Works on Linux (root) and Windows (host).
# Inside Docker on Windows without usbipd-win: fails gracefully with warning only.
try:
    import keyboard
    _KEY_DOWN = keyboard.KEY_DOWN
    _KEYBOARD_AVAILABLE = True
except Exception as _kb_err:
    keyboard = None          # type: ignore
    _KEY_DOWN = "down"
    _KEYBOARD_AVAILABLE = False
    log.warning(
        "keyboard library unavailable (%s) -- "
        "HID scanner disabled, use camera scanner or manual input",
        _kb_err
    )

# --- Public API ---
queue_barcode = queue.Queue()
flag_barcode = False

# --- Internal state ---
_recorded_keys = []
_listener_started = False
_listener_lock = threading.Lock()
_hook_ref = None

last_barcode = None


def _on_key_event(e):
    """Called on every key event from the barcode scanner (HID keyboard mode)."""
    global flag_barcode, last_barcode

    if e.event_type == _KEY_DOWN:
        if e.name == 'enter':
            raw = "".join(_recorded_keys)
            _recorded_keys.clear()
            if raw:
                barcode = normalize_barcode(raw)
                if barcode:
                    if raw != barcode:
                        log.info("QR->SN: %r -> %r", raw, barcode)
                    queue_barcode.put(barcode)
                    last_barcode = barcode
                    flag_barcode = True
                    log.info("Barcode scanned: %s", barcode)
        elif len(e.name) == 1:
            _recorded_keys.append(e.name)


def start_listener():
    """Start keyboard hook in background thread (non-blocking)."""
    global _listener_started, _hook_ref
    if not _KEYBOARD_AVAILABLE:
        log.warning("HID scanner disabled -- keyboard not available on this platform")
        return
    with _listener_lock:
        if _listener_started:
            return
        try:
            _hook_ref = keyboard.hook(_on_key_event)
            _listener_started = True
            log.info("Scanner listener started -- waiting for barcode...")
        except Exception as exc:
            log.warning("Scanner listener could not start: %s", exc)


def stop_listener():
    """Stop keyboard hook (only our hook, not all hooks)."""
    global _listener_started, _hook_ref
    if not _KEYBOARD_AVAILABLE:
        return
    with _listener_lock:
        if not _listener_started:
            return
        try:
            if _hook_ref is not None:
                keyboard.unhook(_hook_ref)
                _hook_ref = None
        except Exception:
            pass
        _listener_started = False


def is_listener_running() -> bool:
    with _listener_lock:
        return _listener_started


def reset_queue():
    """Clear barcode queue and flag before a new inspection cycle."""
    global flag_barcode
    flag_barcode = False
    while not queue_barcode.empty():
        try:
            queue_barcode.get_nowait()
        except queue.Empty:
            break


# --- Standalone test ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Waiting for barcode (HID keyboard mode)...")
    print("Press Ctrl+C to stop.")
    start_listener()
    try:
        if _KEYBOARD_AVAILABLE:
            keyboard.wait('esc')
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    stop_listener()
