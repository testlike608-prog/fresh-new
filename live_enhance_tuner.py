# -*- coding: utf-8 -*-
"""
live_enhance_tuner.py
=====================
أداة ضبط باراميترات الـ enhancement لايف من الكاميرا.

بتعرض قبل | بعد جنب بعض، وسلايدر لكل باراميتر، وانت بتحرك بتشوف النتيجة فوراً.
لما توصل لأحسن إعداد اضغط C (تأكيد) — بيطبعلك الباراميترات النهائية
جاهزة copy/paste في كودك.

التشغيل:
    python live_enhance_tuner.py                ← كاميرا useeplus
    python live_enhance_tuner.py opencv 0       ← ويب كام عادي رقم 0
    python live_enhance_tuner.py image path.jpg ← صورة ثابتة بدل الكاميرا

المفاتيح:
    C أو Enter → تأكيد: طباعة الباراميترات النهائية
    Space      → تجميد/فك الفريم (مفيد تثبّت لقطة فيها مشكلة وتظبط عليها)
    S          → حفظ صورة المقارنة الحالية
    Q أو Esc   → خروج

ملاحظة: سلايدر Denoise تقيل (بيبطئ العرض) — سيبه مقفول وانت بتظبط،
وشغّله في الآخر للتأكد النهائي بس.
"""

import os
import sys
import time
from datetime import datetime

import cv2
import numpy as np

from ai_vision import WaterDetector

WIN = "Enhance Tuner  |  C=Confirm  Space=Freeze  S=Save  Q=Quit"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "enhance_compare")


class _Dummy(WaterDetector):
    """instance وهمي عشان نستخدم enhance_image من غير API keys."""
    def _try_check_images(self, image_paths, few_shot_examples=None):
        return ""


def label(img, text, color=(255, 255, 255)):
    out = img.copy()
    cv2.rectangle(out, (0, 0), (out.shape[1], 34), (0, 0, 0), -1)
    cv2.putText(out, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, color, 2, cv2.LINE_AA)
    return out


def open_source(argv):
    """يرجع دالة get_frame() حسب المصدر المطلوب."""
    mode = argv[1].lower() if len(argv) > 1 else "useeplus"

    if mode == "image":
        if len(argv) < 3:
            sys.exit("❌ حدد مسار الصورة: python live_enhance_tuner.py image path.jpg")
        img = cv2.imread(argv[2])
        if img is None:
            sys.exit(f"❌ متقريتش الصورة: {argv[2]}")
        return (lambda: img.copy()), (lambda: None)

    if mode == "opencv":
        idx = int(argv[2]) if len(argv) > 2 else 0
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(idx)
        if not cap.isOpened():
            sys.exit(f"❌ مقدرتش أفتح كاميرا opencv رقم {idx}")

        def _get():
            ok, f = cap.read()
            return f if ok else None
        return _get, cap.release

    # useeplus (الافتراضي)
    from camera_hub import CameraHub
    cam = CameraHub.UseePlus(camera_index=0, upscale=False)  # خام — التحسين على الأصل
    cam.start()
    if not cam.wait_for_frame(timeout=15.0):
        cam.stop()
        sys.exit("❌ كاميرا UseePlus مش بتبعت فريمات — اتأكد إنها متوصلة ومفيش برنامج تاني فاتحها")
    return cam.get_frame, cam.stop


def main():
    get_frame, close_source = open_source(sys.argv)
    det = _Dummy(model="tuner")

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WIN, 1500, 700)

    # ── السلايدرز (OpenCV بيقبل int بس — بنقسم للوصول للكسور) ────────────────
    nop = lambda v: None
    cv2.createTrackbar("Denoise 0/1",      WIN, 0,   1,   nop)  # مقفول للايف
    cv2.createTrackbar("Glare 0/1",        WIN, 1,   1,   nop)
    cv2.createTrackbar("GlareThresh",      WIN, 220, 255, nop)
    cv2.createTrackbar("GlareKnee x100",   WIN, 35,  100, nop)
    cv2.createTrackbar("Gamma x100",       WIN, 130, 300, nop)
    cv2.createTrackbar("CLAHE x10",        WIN, 20,  80,  nop)
    cv2.createTrackbar("Sharpen x100",     WIN, 120, 300, nop)

    frozen = None
    os.makedirs(OUT_DIR, exist_ok=True)
    print("🎛️ حرك السلايدرز وشوف النتيجة. C=تأكيد | Space=تجميد | S=حفظ | Q=خروج")

    def read_params():
        return dict(
            denoise        = bool(cv2.getTrackbarPos("Denoise 0/1", WIN)),
            glare_compress = bool(cv2.getTrackbarPos("Glare 0/1", WIN)),
            glare_thresh   = max(1, cv2.getTrackbarPos("GlareThresh", WIN)),
            glare_knee     = cv2.getTrackbarPos("GlareKnee x100", WIN) / 100.0,
            gamma          = max(0.3, cv2.getTrackbarPos("Gamma x100", WIN) / 100.0),
            clahe_clip     = max(0.1, cv2.getTrackbarPos("CLAHE x10", WIN) / 10.0),
            sharpen        = max(1.0, cv2.getTrackbarPos("Sharpen x100", WIN) / 100.0),
        )

    try:
        while True:
            frame = frozen if frozen is not None else get_frame()
            if frame is None:
                if cv2.waitKey(30) & 0xFF in (ord("q"), 27):
                    break
                continue

            p = read_params()
            det.ENH_DENOISE        = p["denoise"]
            det.ENH_GLARE_COMPRESS = p["glare_compress"]
            det.ENH_GLARE_THRESH   = p["glare_thresh"]
            det.ENH_GLARE_KNEE     = p["glare_knee"]
            det.ENH_GAMMA          = p["gamma"]
            det.ENH_CLAHE_CLIP     = p["clahe_clip"]
            det.ENH_SHARPEN        = p["sharpen"]

            t0 = time.time()
            enhanced = det.enhance_image(frame)
            ms = (time.time() - t0) * 1000

            state = "FROZEN" if frozen is not None else "LIVE"
            combo = np.hstack([
                label(frame,    f"Before  [{state}]"),
                label(enhanced, f"After   ({ms:.0f} ms)", (0, 255, 0)),
            ])
            cv2.imshow(WIN, combo)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            elif key == ord(" "):
                frozen = None if frozen is not None else frame.copy()
            elif key == ord("s"):
                path = os.path.join(
                    OUT_DIR, f"tuner_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
                cv2.imwrite(path, combo)
                print(f"💾 اتحفظت: {path}")
            elif key in (ord("c"), 13):  # C أو Enter
                print("\n" + "═" * 60)
                print("✅ الباراميترات النهائية — انسخها في كودك:")
                print("═" * 60)
                print(f"""
detector = WaterDetector.Groq(     # أو Gemini / Local
    model="...",
    use_enhancement=True,
    denoise={p['denoise']},
    glare_compress={p['glare_compress']},
    glare_thresh={p['glare_thresh']},
    glare_knee={p['glare_knee']:.2f},
    gamma={p['gamma']:.2f},
    clahe_clip={p['clahe_clip']:.1f},
    sharpen={p['sharpen']:.2f},
)""")
                print("═" * 60 + "\n")
    finally:
        close_source()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
