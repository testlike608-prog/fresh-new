"""
Barcode scanner listener باستخدام evdev (بدل مكتبة keyboard)
- مش محتاج sudo: بس ضيف اليوزر لجروب input
- بيقرا من جهاز الإسكانر بس، مش من كل الكيبوردات
- نفس الـ Public API القديم بالظبط
"""

import os
import sys
import time
import queue
import select
import threading

from evdev import InputDevice, list_devices, ecodes

from barcode_utils import normalize_barcode

# ─── إعدادات ─────────────────────────────────────────────────────────────────
# جزء من اسم الإسكانر (شغّل: python3 scanner.py --list علشان تعرفه)
SCANNER_NAME = os.environ.get("SCANNER_NAME", "")
SCANNER_VENDOR_ID = 0x05e0
SCANNER_PRODUCT_ID = 0x1200

# True = الإسكانر يبقى حصري للبرنامج (الباركود ما يتكتبش في أي شباك تاني)
GRAB_DEVICE = True

# ─── Public API (للاستخدام من باقي الموديولز) ────────────────────────────────
queue_barcode = queue.Queue()
flag_barcode = False
last_barcode = None

# ─── Internal state ──────────────────────────────────────────────────────────
_recorded_keys = []
_listener_started = False
_listener_lock = threading.Lock()
_stop_event = threading.Event()
_thread = None

# ─── Key maps ────────────────────────────────────────────────────────────────
_NORMAL = {}
_SHIFT = {}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _code = getattr(ecodes, f"KEY_{_c.upper()}")
    _NORMAL[_code] = _c
    _SHIFT[_code] = _c.upper()
for _d, _s in zip("1234567890", "!@#$%^&*()"):
    _code = getattr(ecodes, f"KEY_{_d}")
    _NORMAL[_code] = _d
    _SHIFT[_code] = _s
    _kp = getattr(ecodes, f"KEY_KP{_d}")
    _NORMAL[_kp] = _SHIFT[_kp] = _d
for _name, _n, _s in [
    ("MINUS", "-", "_"), ("EQUAL", "=", "+"), ("SLASH", "/", "?"),
    ("DOT", ".", ">"), ("COMMA", ",", "<"), ("SEMICOLON", ";", ":"),
    ("APOSTROPHE", "'", '"'), ("LEFTBRACE", "[", "{"),
    ("RIGHTBRACE", "]", "}"), ("BACKSLASH", "\\", "|"),
    ("GRAVE", "`", "~"), ("SPACE", " ", " "),
    ("KPMINUS", "-", "-"), ("KPPLUS", "+", "+"),
    ("KPDOT", ".", "."), ("KPSLASH", "/", "/"), ("KPASTERISK", "*", "*"),
]:
    _code = getattr(ecodes, f"KEY_{_name}")
    _NORMAL[_code] = _n
    _SHIFT[_code] = _s

_SHIFT_KEYS = {ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT}
_ENTER_KEYS = {ecodes.KEY_ENTER, ecodes.KEY_KPENTER}


def _find_scanner():
    """يدور على Symbol barcode scanner باستخدام USB VID/PID."""
    for path in list_devices():
        try:
            dev = InputDevice(path)

            # evdev device infoaa
            info = dev.info

            if (
                info.vendor == SCANNER_VENDOR_ID
                and info.product == SCANNER_PRODUCT_ID
            ):
                return dev

            # fallback بالاسم لو احتجناه
            if SCANNER_NAME and SCANNER_NAME.lower() in dev.name.lower():
                return dev

            dev.close()

        except Exception as e:
            try:
                dev.close()
            except Exception:
                pass

    return None



def _handle_barcode():
    global flag_barcode, last_barcode
    raw = "".join(_recorded_keys)
    _recorded_keys.clear()
    if not raw:
        return
    barcode = normalize_barcode(raw)
    if not barcode:
        return
    if raw != barcode:
        print(f"QR→SN: {raw!r}  →  {barcode!r}")
    queue_barcode.put(barcode)
    last_barcode = barcode
    flag_barcode = True
    print(f"تمت قراءة الباركود: {barcode}")


def _read_loop():
    """Thread بيقرا من الإسكانر، وبيعيد الاتصال لو اتشال واتركب."""
    shift = False
    while not _stop_event.is_set():
        dev = _find_scanner()
        if dev is None:
            print("⚠ الإسكانر مش متوصل — هحاول تاني...")
            _stop_event.wait(2)
            continue
        print(f"Scanner listener started on: {dev.name} ({dev.path})")
        try:
            if GRAB_DEVICE:
                dev.grab()
            while not _stop_event.is_set():
                r, _, _ = select.select([dev.fd], [], [], 0.5)
                if not r:
                    continue
                for ev in dev.read():
                    if ev.type != ecodes.EV_KEY:
                        continue
                    if ev.code in _SHIFT_KEYS:
                        shift = ev.value != 0      # 1 down, 2 hold, 0 up
                        continue
                    if ev.value != 1:              # key down بس
                        continue
                    if ev.code in _ENTER_KEYS:
                        _handle_barcode()
                    else:
                        ch = (_SHIFT if shift else _NORMAL).get(ev.code)
                        if ch:
                            _recorded_keys.append(ch)
        except OSError as e:
            print(f"⚠ الإسكانر اتفصل: {e}")
            _recorded_keys.clear()
            shift = False
        finally:
            try:
                if GRAB_DEVICE:
                    dev.ungrab()
            except Exception:
                pass
            dev.close()


def start_listener():
    """تشغيل الـ listener في الباك جراوند (مش blocking)."""
    global _listener_started, _thread
    with _listener_lock:
        if _listener_started:
            return
        try:
            _stop_event.clear()
            _thread = threading.Thread(target=_read_loop, daemon=True)
            _thread.start()
            _listener_started = True
        except Exception as e:
            print(f"⚠ Scanner listener could not start: {e}")


def stop_listener():
    """إيقاف الـ listener."""
    global _listener_started, _thread
    with _listener_lock:
        if not _listener_started:
            return
        _stop_event.set()
        if _thread is not None:
            _thread.join(timeout=2)
            _thread = None
        _listener_started = False


def is_listener_running() -> bool:
    with _listener_lock:
        return _listener_started


def reset_queue():
    """تصفير الكيو والعلم قبل عملية فحص جديدة."""
    global flag_barcode
    flag_barcode = False
    while not queue_barcode.empty():
        try:
            queue_barcode.get_nowait()
        except queue.Empty:
            break


# ─── Standalone mode ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    if "--list" in sys.argv:
        for p in list_devices():
            d = InputDevice(p)
            print(f"{d.path}\t{d.name}")
        sys.exit(0)

    print("في انتظار قراءة الباركود... (Ctrl+C للإيقاف)")
    start_listener()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_listener()

