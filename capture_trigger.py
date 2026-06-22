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

import camera_hub_useeplus as _cam

log = logging.getLogger("capture_trigger")

# ── إعدادات افتراضية ──────────────────────────────────────────────────────────
DEFAULT_SAVE_DIR = "./results"
_save_dir        = DEFAULT_SAVE_DIR
_counter         = 0
_counter_lock    = threading.Lock()

# ── تهيئة ─────────────────────────────────────────────────────────────────────
def start(camera_index: int = 0, save_dir: str = DEFAULT_SAVE_DIR,
          wait_timeout: float = 8.0) -> bool:
    """
    يشغّل الكاميرا في الخلفية وينتظر أول فريم.
    يرجع True لو الكاميرا جاهزة، False لو فشل.
    """
    global _save_dir
    _save_dir = save_dir
    os.makedirs(save_dir, exist_ok=True)

    _cam.start(camera_index=camera_index)
    ready = _cam.wait_for_frame(timeout=wait_timeout)
    if ready:
        log.info(f"capture_trigger: ✅ الكاميرا جاهزة — مجلد الحفظ: {save_dir}")
    else:
        log.error("capture_trigger: ❌ الكاميرا لم تستجب")
    return ready


def stop():
    """يوقف الكاميرا."""
    _cam.stop()


# ── الـ trigger ────────────────────────────────────────────────────────────────
def trigger(save_path: str = None, name: str = "capture") -> str | None:
    """
    يلتقط الفريم الحالي ويحفظه فوراً.

    :param save_path: مسار كامل للصورة — لو None يُولَّد تلقائياً باستخدام timestamp
    :param name: اسم الصورة (بدون امتداد)
    :return: المسار اللي اتحفظت فيه الصورة، أو None لو فشل
    """
    global _counter

    frame = _cam.get_frame()
    if frame is None:
        log.warning("capture_trigger: ⚠️ مفيش فريم — الكاميرا شغالة؟")
        return None

    if save_path is None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        with _counter_lock:
            _counter += 1
            n = _counter
        save_path = os.path.join(_save_dir, f"{name}.jpg")

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


