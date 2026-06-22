"""
live_image.py
-------------
يحفظ آخر فريم من الكاميرا المشتركة (camera_hub) باستمرار.

لا يفتح الكاميرا بنفسه — يقرأ من camera_hub.get_frame()
عشان الكاميرا مش تتفتح أكتر من مرة.

الصورة بتتحفظ في:
    live_camera_feed/latest_frame.jpg   (overwrite دايمًا)

API:
    start()  → يبدأ الحفظ في ثريد خلفي
    stop()   → يوقف الحفظ
    is_running() → True لو شغالة
"""

import cv2
import os
import time
import threading
import logging

import camera_hub   # المصدر المشترك

log = logging.getLogger("live_image")

# ── إعدادات ──────────────────────────────────────────────────────────
_FOLDER    = "live_camera_feed"
_FILENAME  = "latest_frame.jpg"
_SLEEP_SEC = 0.2     # تأخير بين كل حفظ والتاني (200ms = 5fps حفظ)

# ── state داخلي ──────────────────────────────────────────────────────
_stop_event = threading.Event()
_thread     = None
_lock       = threading.Lock()


def _save_loop(out_path: str):
    """يقرأ من camera_hub ويحفظ الفريم باستمرار."""
    log.info(f"live_image: بيحفظ في {out_path}")
    try:
        while not _stop_event.is_set():
            frame = camera_hub.get_frame()
            if frame is None:
                # الكاميرا لسه مبدأتش — نستنى
                time.sleep(0.1)
                continue
            cv2.imwrite(out_path, frame)
            time.sleep(_SLEEP_SEC)
    except Exception as e:
        log.error(f"live_image: خطأ: {e}")
    log.info("live_image: أوقف.")


def start():
    """يبدأ حفظ الفريمات في ثريد خلفي. لو شغال بالفعل مش بيعمل حاجة."""
    global _thread

    with _lock:
        if _thread is not None and _thread.is_alive():
            log.debug("live_image: شغالة بالفعل")
            return

        os.makedirs(_FOLDER, exist_ok=True)
        out_path = os.path.join(_FOLDER, _FILENAME)

        _stop_event.clear()
        _thread = threading.Thread(
            target=_save_loop,
            args=(out_path,),
            name="live-image",
            daemon=True,
        )
        _thread.start()
        log.info("live_image: بدأت.")


def stop(timeout: float = 3.0):
    """يوقف الحفظ وينتظر الثريد ينتهي."""
    global _thread

    with _lock:
        if _thread is None or not _thread.is_alive():
            log.debug("live_image: مش شغالة")
            return
        _stop_event.set()
        t = _thread

    t.join(timeout=timeout)
    if t.is_alive():
        log.warning("live_image: الثريد لم ينتهِ في الوقت")
    else:
        log.info("live_image: أوقفت بنجاح")

    with _lock:
        _thread = None


def is_running() -> bool:
    with _lock:
        return _thread is not None and _thread.is_alive()


# ── لو شغّلت الملف مباشرة ────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    cam = 0 #int(sys.argv[1]) if len(sys.argv) > 1 else 1
    camera_hub.start(camera_index=cam)
    time.sleep(0.5)
    start()

    print("اضغط Ctrl+C للإيقاف...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop()
        camera_hub.stop()
