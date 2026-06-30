"""
capture_trigger.py
------------------
كاميرا useeplus شغالة في الخلفية — بتبعتلها trigger فتحفظ صورة فوراً.

الاستخدام كـ module:
    import capture_trigger
    capture_trigger.start()
    ...
    path = capture_trigger.trigger()          # يحفظ صورة ويرجع المسار
    path = capture_trigger.trigger("out/x.jpg")  # اختر المسار بنفسك

الاستخدام من command line:
    python capture_trigger.py                # يستنى Space أو Enter لكل صورة
    python capture_trigger.py --save-dir ./shots
"""

import os
import time
import logging
import threading
import cv2

from camera_hub import CameraHub

log = logging.getLogger("capture_trigger")

# ── إعدادات افتراضية ──────────────────────────────────────────────────────────
# في Docker: DATA_DIR=/app/data → الصور بتتحفظ في /app/data/results (Volume)
_DATA_DIR        = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SAVE_DIR = os.path.join(_DATA_DIR, "results")
_save_dir        = DEFAULT_SAVE_DIR
_counter         = 0
_counter_lock    = threading.Lock()
_cam: CameraHub | None = None   # الـ instance الداخلي للكاميرا


def _build_camera(camera_index: int) -> CameraHub:
    """ينشئ CameraHub instance حسب config."""
    try:
        from config import config as _cfg
        cam_type = _cfg.get("camera_type", "useeplus")
    except Exception:
        cam_type = "useeplus"
    if cam_type == "opencv":
        return CameraHub.OpenCV(camera_index=camera_index)
    return CameraHub.UseePlus(camera_index=camera_index)


# ── تهيئة ─────────────────────────────────────────────────────────────────────
def start(camera_index: int = 0, save_dir: str = DEFAULT_SAVE_DIR,
          wait_timeout: float = 8.0, camera: CameraHub | None = None) -> bool:
    """
    يشغّل الكاميرا في الخلفية وينتظر أول فريم.

    :param camera: لو مرّرت CameraHub instance جاهز هيستخدمه مباشرة (مش هيعمل instance جديد)
    :return: True لو الكاميرا جاهزة، False لو فشل.
    """
    global _save_dir, _cam
    _save_dir = save_dir
    os.makedirs(save_dir, exist_ok=True)

    if camera is not None:
        _cam = camera                    # استخدم الـ instance المرسل من App
    elif _cam is None:
        _cam = _build_camera(camera_index)

    if not _cam.is_running():
        # BUG-038: استخدم الـ index الحقيقي للكاميرا وليس القيمة الافتراضية 0
        cam_idx = getattr(_cam, '_cam_index', camera_index)
        _cam.start(camera_index=cam_idx)

    ready = _cam.wait_for_frame(timeout=wait_timeout)
    if ready:
        log.info(f"capture_trigger: ✅ الكاميرا جاهزة — مجلد الحفظ: {save_dir}")
    else:
        log.error("capture_trigger: ❌ الكاميرا لم تستجب")
    return ready


def stop():
    """يوقف الكاميرا."""
    if _cam is not None and _cam.is_running():
        _cam.stop()


# ── الـ trigger ────────────────────────────────────────────────────────────────
def trigger(save_path: str = None, name: str = "capture") -> str | None:
    """
    يلتقط الفريم الحالي ويحفظه فوراً.

    :param save_path: مسار كامل للصورة — لو None يُولَّد تلقائياً
    :param name: اسم الصورة (بدون امتداد)
    :return: المسار اللي اتحفظت فيه الصورة، أو None لو فشل
    """
    global _counter

    frame = _cam.get_frame() if _cam is not None else None
    if frame is None:
        log.warning("capture_trigger: ⚠️ مفيش فريم — الكاميرا شغالة؟")
        return None

    if save_path is None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        with _counter_lock:
            _counter += 1
            n = _counter
        # BUG-016: ts و n كانوا بيتحسبوا لكن مش بيتستخدموا → كل الصور بنفس الاسم
        save_path = os.path.join(_save_dir, f"{name}_{ts}_{n}.jpg")

    os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)

    ok = cv2.imwrite(save_path, frame)
    if ok:
        log.info(f"capture_trigger: 📸 تم الحفظ → {save_path}")
        return save_path
    else:
        log.error(f"capture_trigger: ❌ فشل الحفظ في {save_path}")
        return None


# ── تشغيل مباشر ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="useeplus trigger capture")
    parser.add_argument("--camera", type=int, default=0,  help="رقم الكاميرا (default: 0)")
    parser.add_argument("--save-dir", default="./captures", help="مجلد الحفظ (default: ./captures)")
    parser.add_argument("--auto", type=float, default=0,
                        help="وضع تلقائي: حفظ كل N ثانية (مثال: --auto 2.0)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    print("⏳ جاري تشغيل الكاميرا...")
    if not start(camera_index=args.camera, save_dir=args.save_dir):
        print("❌ فشل تشغيل الكاميرا. تحقق من الاتصال والـ driver.")
        sys.exit(1)

    if args.auto > 0: 
        # ── وضع تلقائي ─────────────────────────────────────────────
        print(f"✅ وضع تلقائي — كل {args.auto}s  |  Ctrl+C للإيقاف")
        try:
            while True:

                path = trigger(name="auto_capture")
                if path:
                    print(f"  📸 {path}")
                time.sleep(args.auto)
        except KeyboardInterrupt:
            print("\nإيقاف...")
    else:
        # ── وضع يدوي (trigger بـ Space/Enter) ──────────────────────
        print("✅ جاهز — اضغط [Space] أو [Enter] للتصوير، [q] للخروج")
        try:
            while True:
                key = input("> ").strip().lower()
                if key in ("q", "quit", "exit"):
                    break
                path = trigger(name="manual_capture")
                if path:
                    print(f"  📸 {path}")
        except (KeyboardInterrupt, EOFError):
            print("\nإيقاف...")

    stop()
    print("👋 انتهى.")


