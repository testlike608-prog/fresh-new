# -*- coding: utf-8 -*-
"""
histmatch.py
============
Histogram Matching احترافي — نقل توزيع الإضاءة/الألوان من صورة مرجعية
(مأخوذة في ظروف مثالية) لأي صورة تانية.

── الاستخدام كموديول ──────────────────────────────────────────────────────────
    from histmatch import match_histogram, HistogramProfile

    # مطابقة مباشرة بين صورتين
    result = match_histogram(bad_img, good_img, mode="lab", strength=0.8)

    # أو: احفظ بروفايل من الصورة المرجعية مرة واحدة واستخدمه دايماً
    profile = HistogramProfile.from_image(good_img, mode="lab")
    profile.save("factory_reference.npz")
    ...
    profile = HistogramProfile.load("factory_reference.npz")
    result  = profile.apply(bad_img, strength=0.8)

── الاستخدام من الترمينال ─────────────────────────────────────────────────────
    python histmatch.py --ref good.jpg --src bad.jpg
    python histmatch.py --ref good.jpg --src folder/ --out matched/ --compare
    python histmatch.py --ref good.jpg --save-profile factory_ref.npz
    python histmatch.py --profile factory_ref.npz --src folder/ --strength 0.7

── الأوضاع (mode) ─────────────────────────────────────────────────────────────
    lab      (الافتراضي) مطابقة الإضاءة فقط (قناة L) — بيوحّد الإضاءة والتعريض
             من غير أي تغيير في الألوان. الأنسب للفحص الصناعي.
    lab-full مطابقة الإضاءة + الألوان في مساحة LAB — أنعم من bgr.
    bgr      مطابقة كل قناة لون على حدة — أقوى تأثير، وارد يحصل color shift بسيط.
    gray     للصور الرمادية.

── strength ───────────────────────────────────────────────────────────────────
    1.0 = مطابقة كاملة | 0.5 = نص المسافة | 0.0 = من غير تغيير.
    قيم 0.6-0.85 غالباً أنضف صناعياً (مطابقة كاملة ممكن تبرز noise).
"""

from __future__ import annotations

import os

import cv2
import numpy as np

VALID_MODES = ("lab", "lab-full", "bgr", "gray")
_BINS = 256


# ══════════════════════════════════════════════════════════════════════════════
#  Core
# ══════════════════════════════════════════════════════════════════════════════

def _channel_hist(ch: np.ndarray) -> np.ndarray:
    """هيستوجرام 256-bin لقناة uint8."""
    return cv2.calcHist([ch], [0], None, [_BINS], [0, _BINS]).ravel()


def _hist_to_cdf(hist: np.ndarray) -> np.ndarray:
    cdf = np.cumsum(hist).astype(np.float64)
    total = cdf[-1]
    if total <= 0:
        return np.linspace(0.0, 1.0, _BINS)
    return cdf / total


def _build_lut(src_hist: np.ndarray, ref_hist: np.ndarray) -> np.ndarray:
    """
    LUT بتنقل قناة من توزيعها الحالي لتوزيع المرجع
    (مطابقة CDF-to-CDF بالـ interpolation).
    """
    src_cdf = _hist_to_cdf(src_hist)
    ref_cdf = _hist_to_cdf(ref_hist)
    lut = np.interp(src_cdf, ref_cdf, np.arange(_BINS))
    return np.clip(lut, 0, 255).astype(np.uint8)


def _match_channel(src_ch: np.ndarray, ref_hist: np.ndarray) -> np.ndarray:
    return cv2.LUT(src_ch, _build_lut(_channel_hist(src_ch), ref_hist))


def _split_channels(img: np.ndarray, mode: str) -> tuple[list[np.ndarray], str]:
    """يرجع القنوات اللي هتتطابق + الـ colorspace للرجوع."""
    if mode == "gray":
        g = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return [g], "gray"
    if mode in ("lab", "lab-full"):
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2Lab)
        return list(cv2.split(lab)), "lab"
    return list(cv2.split(img)), "bgr"      # bgr


def _matched_channel_indices(mode: str) -> list[int]:
    """أرقام القنوات اللي بتتطابق فعلاً حسب الوضع."""
    if mode == "lab":
        return [0]            # L بس — الإضاءة من غير الألوان
    if mode == "lab-full":
        return [0, 1, 2]
    if mode == "gray":
        return [0]
    return [0, 1, 2]          # bgr


# ══════════════════════════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════════════════════════

class HistogramProfile:
    """
    بروفايل توزيع الصورة المرجعية — بيتحسب مرة واحدة ويتحفظ على الـ disk،
    فمتحتاجش تفضل شايل صورة المرجع نفسها مع المشروع.
    """

    def __init__(self, mode: str, hists: np.ndarray):
        if mode not in VALID_MODES:
            raise ValueError(f"mode لازم يكون واحد من {VALID_MODES}")
        self.mode  = mode
        self.hists = np.asarray(hists, dtype=np.float64)  # (n_channels, 256)

    # ── إنشاء ────────────────────────────────────────────────────────────────
    @classmethod
    def from_image(cls, ref_img: np.ndarray, mode: str = "lab") -> "HistogramProfile":
        if ref_img is None:
            raise ValueError("الصورة المرجعية فاضية (None)")
        chans, _ = _split_channels(ref_img, mode)
        idxs = _matched_channel_indices(mode)
        hists = np.stack([_channel_hist(chans[i]) for i in idxs])
        return cls(mode, hists)

    @classmethod
    def from_file(cls, path: str, mode: str = "lab") -> "HistogramProfile":
        img = cv2.imread(path)
        if img is None:
            raise FileNotFoundError(f"متقريتش الصورة المرجعية: {path}")
        return cls.from_image(img, mode)

    # ── حفظ / تحميل ──────────────────────────────────────────────────────────
    def save(self, path: str):
        np.savez_compressed(path, mode=self.mode, hists=self.hists)

    @classmethod
    def load(cls, path: str) -> "HistogramProfile":
        data = np.load(path, allow_pickle=False)
        return cls(str(data["mode"]), data["hists"])

    # ── تطبيق ────────────────────────────────────────────────────────────────
    def apply(self, img: np.ndarray, strength: float = 1.0) -> np.ndarray:
        """
        يطبق المطابقة على صورة.

        strength: 0.0 → 1.0 (قد إيه الصورة تتحرك ناحية توزيع المرجع)
        """
        if img is None:
            raise ValueError("الصورة فاضية (None)")
        strength = float(np.clip(strength, 0.0, 1.0))
        if strength == 0.0:
            return img.copy()

        chans, space = _split_channels(img, self.mode)
        idxs = _matched_channel_indices(self.mode)

        for hist_row, ci in zip(self.hists, idxs):
            chans[ci] = _match_channel(chans[ci], hist_row)

        if space == "gray":
            matched = chans[0]
            original = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        elif space == "lab":
            matched = cv2.cvtColor(cv2.merge(chans), cv2.COLOR_Lab2BGR)
            original = img
        else:
            matched = cv2.merge(chans)
            original = img

        if strength >= 1.0:
            return matched
        return cv2.addWeighted(matched, strength, original, 1.0 - strength, 0)


def match_histogram(
    source: np.ndarray,
    reference: np.ndarray,
    mode: str = "lab",
    strength: float = 1.0,
) -> np.ndarray:
    """
    الدالة المختصرة: تطابق صورة source على توزيع صورة reference.

    Parameters
    ----------
    source    : الصورة المطلوب تحسينها (BGR أو gray)
    reference : الصورة المرجعية الممتازة
    mode      : "lab" | "lab-full" | "bgr" | "gray"   (شرح فوق في الـ docstring)
    strength  : 0.0-1.0 قوة المطابقة
    """
    return HistogramProfile.from_image(reference, mode).apply(source, strength)


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════

_IMG_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")


def _collect(src: str) -> list[str]:
    if os.path.isfile(src):
        return [src]
    if os.path.isdir(src):
        return sorted(
            os.path.join(src, f) for f in os.listdir(src)
            if f.lower().endswith(_IMG_EXTS)
        )
    return []


def _label(img, text):
    out = img.copy()
    cv2.rectangle(out, (0, 0), (out.shape[1], 34), (0, 0, 0), -1)
    cv2.putText(out, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (255, 255, 255), 2, cv2.LINE_AA)
    return out


def main():
    import argparse

    ap = argparse.ArgumentParser(
        prog="histmatch",
        description="Histogram Matching — توحيد الإضاءة/الألوان على صورة مرجعية",
    )
    ref_group = ap.add_mutually_exclusive_group(required=True)
    ref_group.add_argument("--ref", metavar="IMG",
                           help="الصورة المرجعية الممتازة")
    ref_group.add_argument("--profile", metavar="NPZ",
                           help="بروفايل محفوظ بدل الصورة المرجعية")

    ap.add_argument("--src", metavar="PATH",
                    help="صورة أو فولدر الصور المطلوب تحسينها")
    ap.add_argument("--out", default="matched", metavar="DIR",
                    help="فولدر النتائج (افتراضي: matched/)")
    ap.add_argument("--mode", default="lab", choices=VALID_MODES,
                    help="وضع المطابقة (افتراضي: lab = الإضاءة بس)")
    ap.add_argument("--strength", type=float, default=1.0, metavar="0-1",
                    help="قوة المطابقة (افتراضي: 1.0)")
    ap.add_argument("--compare", action="store_true",
                    help="حفظ صورة مقارنة قبل|بعد جنب النتيجة")
    ap.add_argument("--save-profile", metavar="NPZ",
                    help="حفظ بروفايل الصورة المرجعية للاستخدام لاحقاً")
    args = ap.parse_args()

    # ── البروفايل ────────────────────────────────────────────────────────────
    if args.profile:
        profile = HistogramProfile.load(args.profile)
        print(f"✅ اتحمل البروفايل: {args.profile} (mode={profile.mode})")
    else:
        profile = HistogramProfile.from_file(args.ref, args.mode)
        print(f"✅ اتحسب البروفايل من: {args.ref} (mode={args.mode})")

    if args.save_profile:
        profile.save(args.save_profile)
        print(f"💾 البروفايل اتحفظ: {args.save_profile}")
        if not args.src:
            return

    if not args.src:
        raise SystemExit("❌ حدد --src صورة أو فولدر (أو --save-profile بس)")

    images = _collect(args.src)
    if not images:
        raise SystemExit(f"❌ مفيش صور في: {args.src}")

    os.makedirs(args.out, exist_ok=True)
    print(f"هشتغل على {len(images)} صورة → {args.out}/  (strength={args.strength})\n")

    for path in images:
        img = cv2.imread(path)
        if img is None:
            print(f"⚠️ متقريتش: {path}")
            continue

        result = profile.apply(img, strength=args.strength)
        name, ext = os.path.splitext(os.path.basename(path))
        out_path = os.path.join(args.out, f"{name}_matched{ext}")
        cv2.imwrite(out_path, result)

        if args.compare:
            combo = np.hstack([_label(img, "Before"), _label(result, "After")])
            cv2.imwrite(os.path.join(args.out, f"{name}_compare{ext}"), combo)

        print(f"✅ {os.path.basename(path)} → {out_path}")

    print("\nخلصنا ✅")


if __name__ == "__main__":
    main()
