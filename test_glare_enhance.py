# -*- coding: utf-8 -*-
"""
test_glare_enhance.py
=====================
سكريبت اختبار للـ enhancement الجديد (مايه على بلاستيك أبيض عاكس).

بيطلع لكل صورة مقارنة جنب بعض:  الأصلية | البايبلاين القديم | الجديد
وبيحفظ النتيجة في فولدر enhance_compare/

الاستخدام:
    python test_glare_enhance.py                        ← كل الصور من captures_standalone/
    python test_glare_enhance.py path/to/image.jpg      ← صورة معينة
    python test_glare_enhance.py path/to/folder         ← فولدر معين

    باراميترات اختيارية (لضبط الإعدادات وانت بتجرب):
    python test_glare_enhance.py --gamma 1.5 --glare 200 --knee 0.25 --clip 2.5 --sharpen 1.0

بعد ما تلاقي أحسن قيم لمنتجاتك، ثبّتها في كودك قبل الفحص:
    detector = WaterDetector.Gemini(..., use_enhancement=True)
    detector.ENH_GAMMA        = 1.5
    detector.ENH_GLARE_THRESH = 200
"""

import os
import sys
import glob
import argparse

import cv2
import numpy as np

from ai_vision import WaterDetector

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "enhance_compare")
DEFAULT_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures_standalone")
IMG_EXTS = (".jpg", ".jpeg", ".png", ".bmp")


# ── البايبلاين القديم (للمقارنة فقط) ─────────────────────────────────────────
def old_enhance(img_bgr):
    denoised = cv2.fastNlMeansDenoisingColored(
        img_bgr, None, h=3, hColor=3, templateWindowSize=7, searchWindowSize=21)
    lab = cv2.cvtColor(denoised, cv2.COLOR_BGR2Lab)
    l_ch, a_ch, b_ch = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l_ch = clahe.apply(l_ch)
    enhanced = cv2.cvtColor(cv2.merge((l_ch, a_ch, b_ch)), cv2.COLOR_Lab2BGR)
    blurred = cv2.GaussianBlur(enhanced, (0, 0), 3)
    return cv2.addWeighted(enhanced, 1.5, blurred, -0.5, 0)


def label(img, text):
    """يكتب عنوان فوق الصورة."""
    out = img.copy()
    cv2.rectangle(out, (0, 0), (out.shape[1], 34), (0, 0, 0), -1)
    cv2.putText(out, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (255, 255, 255), 2, cv2.LINE_AA)
    return out


def collect_images(src: str) -> list[str]:
    if os.path.isfile(src):
        return [src]
    if os.path.isdir(src):
        files = []
        for ext in IMG_EXTS:
            files += glob.glob(os.path.join(src, f"*{ext}"))
            files += glob.glob(os.path.join(src, f"*{ext.upper()}"))
        return sorted(set(files))
    return []


class _Dummy(WaterDetector):
    """كلاس وهمي عشان نستخدم enhance_image من غير API keys."""
    def _try_check_images(self, image_paths, few_shot_examples=None):
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default=DEFAULT_SRC,
                    help="صورة أو فولدر (افتراضي: captures_standalone)")
    ap.add_argument("--gamma",   type=float, default=None, help="غمقان الفواتح (افتراضي 1.3)")
    ap.add_argument("--glare",   type=int,   default=None, help="عتبة الـ glare (افتراضي 220)")
    ap.add_argument("--knee",    type=float, default=None, help="قوة ضغط الـ glare (افتراضي 0.35، أقل=أقوى)")
    ap.add_argument("--clip",    type=float, default=None, help="قوة CLAHE (افتراضي 2.0)")
    ap.add_argument("--sharpen", type=float, default=None, help="قوة الحدة (افتراضي 1.2، 1.0=مفيش)")
    ap.add_argument("--no-show", action="store_true", help="من غير عرض نوافذ — حفظ بس")
    args = ap.parse_args()

    det = _Dummy(model="dummy")
    if args.gamma   is not None: det.ENH_GAMMA        = args.gamma
    if args.glare   is not None: det.ENH_GLARE_THRESH = args.glare
    if args.knee    is not None: det.ENH_GLARE_KNEE   = args.knee
    if args.clip    is not None: det.ENH_CLAHE_CLIP   = args.clip
    if args.sharpen is not None: det.ENH_SHARPEN      = args.sharpen

    print(f"الإعدادات: gamma={det.ENH_GAMMA} glare={det.ENH_GLARE_THRESH} "
          f"knee={det.ENH_GLARE_KNEE} clip={det.ENH_CLAHE_CLIP} sharpen={det.ENH_SHARPEN}")

    images = collect_images(args.src)
    if not images:
        print(f"❌ مفيش صور في: {args.src}")
        sys.exit(1)

    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"هشتغل على {len(images)} صورة → النتائج في {OUT_DIR}\n")

    for path in images:
        img = cv2.imread(path)
        if img is None:
            print(f"⚠️ متقريتش: {path}")
            continue

        old = old_enhance(img)
        new = det.enhance_image(img)

        combo = np.hstack([
            label(img, "Original"),
            label(old, "Old (CLAHE+Sharp)"),
            label(new, "New (Glare+Gamma+CLAHE)"),
        ])

        name = os.path.splitext(os.path.basename(path))[0]
        out_path = os.path.join(OUT_DIR, f"{name}_compare.png")
        cv2.imwrite(out_path, combo)
        print(f"✅ {os.path.basename(path)} → {out_path}")

        if not args.no_show:
            # عرض بمقاس مناسب للشاشة
            h, w = combo.shape[:2]
            scale = min(1600 / w, 900 / h, 1.0)
            disp = cv2.resize(combo, (int(w * scale), int(h * scale)))
            cv2.imshow("enhance compare  (q=exit, any key=next)", disp)
            key = cv2.waitKey(0) & 0xFF
            if key == ord("q"):
                break

    cv2.destroyAllWindows()
    print("\nخلصنا. عدّل الباراميترات وجرب تاني لحد ما توصل لأحسن نتيجة لمنتجاتك.")


if __name__ == "__main__":
    main()
