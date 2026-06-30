"""
camera_barcode.py
-----------------
سكانر الباركود عن طريق الكاميرا المشتركة (camera_hub).

لا يفتح الكاميرا بنفسه — يقرأ الفريمات من camera_hub.get_frame()
عشان الكاميرا مش تتفتح أكتر من مرة.

بيضع الباركودات في نفس queue بتاعة scanner.py.

استخدام:
    import camera_barcode
    camera_barcode.start()    # تشغيل (camera_hub لازم يكون شغال)
    camera_barcode.stop()     # إيقاف
    camera_barcode.is_running()
"""

import cv2
import os
import zxingcpp
import threading
import time
import logging

import scanner
from barcode_utils import normalize_barcode
from camera_hub import CameraHub

log = logging.getLogger("camera_barcode")

# ─── Internal state ───────────────────────────────────────────────────────────
_thread     = None
_stop_event = threading.Event()
_lock       = threading.Lock()
_hub: CameraHub | None = None   # الـ instance المشترك مع App

# كم decode في الثانية (ما نضغطش على المعالج)
DECODE_FPS  = 10


# ─── Decode loop ──────────────────────────────────────────────────────────────
_DATA_DIR           = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DEBUG_FRAME_PATH    = os.path.join(_DATA_DIR, "results", "test.jpg")  # BUG-017 + Docker
DEBUG_SAVE_INTERVAL = 2.0                   # احفظ كل 2 ثانية


def _decode_loop(stop_event: threading.Event, camera: CameraHub):
    """
    يقرأ الفريمات من CameraHub instance ويحاول يكشف الباركود فيها.
    """
    frame_interval       = 1.0 / DECODE_FPS
    last_decode_at       = 0.0
    last_debug_save_at   = 0.0
    _last_queued_barcode = None
    _none_warn_at        = 0.0

    import os
    os.makedirs(os.path.dirname(DEBUG_FRAME_PATH), exist_ok=True)

    log.info("[CameraScanner] في انتظار باركود... (يقرأ من CameraHub)")

    while not stop_event.is_set():
        frame = camera.get_frame()
        if frame is None:
            now = time.time()
            if now - _none_warn_at > 5.0:
                log.warning("[CameraScanner] camera_hub مش بابعت فريمات — استنى...")
                _none_warn_at = now
            time.sleep(0.05)
            continue

        now = time.time()

        # ─── حفظ debug frame كل 2 ثانية عشان نشوف الكاميرا بتشوف إيه ───
        if now - last_debug_save_at >= DEBUG_SAVE_INTERVAL:
            cv2.imwrite(DEBUG_FRAME_PATH, frame)
            last_debug_save_at = now

        if now - last_decode_at < frame_interval:
            time.sleep(0.01)
            continue
        last_decode_at = now

        # ─── محاولة 1: صورة رمادية مباشرة ───
        gray    = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        results = zxingcpp.read_barcodes(gray)

        if not results:
            # ─── محاولة 2: تحسين التباين ───
            enhanced = cv2.equalizeHist(gray)
            results  = zxingcpp.read_barcodes(enhanced)

        if not results:
            # ─── محاولة 3: Threshold ───
            _, thresh = cv2.threshold(gray, 0, 255,
                                      cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            results   = zxingcpp.read_barcodes(thresh)

        for result in results:
            raw = result.text.strip()
            if not raw:
                continue

            barcode = normalize_barcode(raw)
            if not barcode:
                continue

            # ── منع التكرار المتتالي ────────────────────────────────────
            if barcode == _last_queued_barcode:
                continue
            _last_queued_barcode = barcode

            if raw != barcode:
                log.info(f"[CameraScanner] QR→SN: {raw!r}  →  {barcode!r}")

            scanner.queue_barcode.put(barcode)
            scanner.last_barcode = barcode
            scanner.flag_barcode = True
            log.info(f"[CameraScanner] ✅ باركود: {barcode!r}  ({result.format}) → queue")
            time.sleep(3)
            _last_queued_barcode = None  # بعد 3 ثواني نسمح لنفس الباركود يتكرر لو ظهر تاني 
           
            break   # نأخد أول باركود بس في الفريم

    log.info("[CameraScanner] أوقف.")


# ─── Public API ───────────────────────────────────────────────────────────────
def start(camera_index: int = None, camera: CameraHub | None = None):
    """
    تشغيل الـ decode loop في ثريد خلفي.

    :param camera: CameraHub instance شغال — لو None هيبني واحد من config.
                   الكاميرا لازم تكون شغالة (start()) قبل الاستدعاء.
    :param camera_index: محتجز للـ backward compatibility — مش بيُستخدم.
    """
    global _thread, _stop_event, _hub

    if camera is not None:
        _hub = camera
    elif _hub is None:
        # fallback: بيبني instance جديد من config (لو مش متمررلوش instance)
        try:
            from config import config as _cfg
            cam_type = _cfg.get("camera_type", "useeplus")
            cam_idx  = camera_index if camera_index is not None else int(_cfg.get("camera_index", 0))
        except Exception:
            cam_type, cam_idx = "useeplus", 0
        _hub = CameraHub.UseePlus(camera_index=cam_idx) if cam_type != "opencv" \
               else CameraHub.OpenCV(camera_index=cam_idx)
        if not _hub.is_running():
            _hub.start()

    with _lock:
        if _thread is not None and _thread.is_alive():
            log.debug("[CameraScanner] شغالة بالفعل.")
            return

        _stop_event = threading.Event()
        _thread = threading.Thread(
            target=_decode_loop,
            args=(_stop_event, _hub),
            name="camera-barcode-scanner",
            daemon=True,
        )
        _thread.start()
        log.info("[CameraScanner] بدأت.")


def stop():
    """إيقاف الـ decode loop."""
    global _thread

    # BUG-039: _thread = None كان يتعمل برّه الـ lock → race مع is_running()
    _stop_event.set()

    # اجيب reference للـ thread برّه الـ lock حتى أعمل join
    with _lock:
        t = _thread

    if t is not None:
        t.join(timeout=3.0)

    # امسح _thread جوّا الـ lock بعد الـ join
    with _lock:
        if _thread is t:   # تأكيد: مش اتعمل restart في نفس اللحظة
            _thread = None

    log.info("[CameraScanner] أوقف.")


def is_running() -> bool:
    with _lock:
        return _thread is not None and _thread.is_alive()


def get_available_cameras(max_check: int = 6):
    """
    يكتشف الكاميرات المتاحة — مفيد لاختيار رقم الكاميرا في الـ GUI.
    """
    found = []
    for i in range(max_check):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        if cap.isOpened():
            found.append(i)
            cap.release()
    return found


# ─── Standalone test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    '''
    cams = get_available_cameras()
    if not cams:
        print("❌ مفيش كاميرات.")
        sys.exit(1)
    '''
    #print(f"✅ الكاميرات المتاحة: {cams}")
    cam_idx = 0

    # نشغّل الكاميرا ونمررها لـ start()
    hub = CameraHub.UseePlus(camera_index=cam_idx)
    hub.start()
    hub.wait_for_frame(timeout=5.0)

    start(camera=hub)
    print("اضغط Ctrl+C للإيقاف...")
    try:
        while True:
            time.sleep(0.5)
            if not scanner.queue_barcode.empty():
                bc = scanner.queue_barcode.get_nowait()
                print(f">>> باركود: {bc}")
    except KeyboardInterrupt:
        stop()
        hub.stop()
