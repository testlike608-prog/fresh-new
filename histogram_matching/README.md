# Histogram Matching — توحيد الإضاءة على صورة مرجعية

نقل توزيع الإضاءة/الألوان من صورة مرجعية ممتازة (مأخوذة في ظروف مثالية في المصنع)
لأي صورة تانية — فكل الصور تطلع بنفس التعريض والإضاءة قبل الفحص.

## الفكرة في سطرين

بدل ما كل صورة تيجي بإضاءة مختلفة حسب الظروف، بتاخد **صورة مرجعية واحدة** ممتازة،
وأي صورة جديدة بتتظبط رياضياً (CDF matching) عشان توزيع سطوعها يطابق المرجع.

## الاستخدام السريع (ترمينال)

```bash
# صورة واحدة
python histmatch.py --ref good.jpg --src bad.jpg

# فولدر كامل + صور مقارنة قبل/بعد
python histmatch.py --ref good.jpg --src captures/ --out matched/ --compare

# احفظ بروفايل المرجع مرة واحدة (متحتاجش صورة المرجع تاني)
python histmatch.py --ref good.jpg --save-profile factory_ref.npz

# استخدم البروفايل المحفوظ
python histmatch.py --profile factory_ref.npz --src captures/ --strength 0.8
```

## الاستخدام من الكود

```python
from histmatch import match_histogram, HistogramProfile

# مباشر
result = match_histogram(bad_img, good_img, mode="lab", strength=0.8)

# أو بالبروفايل (أسرع للإنتاج — بيتحسب مرة واحدة)
profile = HistogramProfile.load("factory_ref.npz")
result  = profile.apply(frame, strength=0.8)
```

## الأوضاع (mode)

| mode | بيطابق إيه | امتى تستخدمه |
|------|-----------|--------------|
| `lab` *(افتراضي)* | الإضاءة فقط (L) | **الفحص الصناعي** — بيوحّد التعريض من غير أي تغيير ألوان |
| `lab-full` | إضاءة + ألوان (LAB) | لو الألوان نفسها مختلفة بين الكاميرات |
| `bgr` | كل قناة لون | أقوى تأثير — وارد color shift بسيط |
| `gray` | صور رمادية | للصور الـ grayscale |

## strength

- `1.0` مطابقة كاملة
- `0.6 – 0.85` الأنسب صناعياً (المطابقة الكاملة ممكن تبرز noise في المناطق الغامقة)
- `0.0` من غير تغيير

## نصايح للاستخدام مع فحص المايه

1. **الصورة المرجعية** لازم تكون لنفس نوع المنتج، بنفس الكاميرا، ونفس وضع الإضاءة —
   منتج ناشف نضيف بإضاءة مثالية.
2. اعمل بروفايل واحفظه (`--save-profile`) وحطه مع المشروع — أسرع وأثبت.
3. الترتيب المقترح في الـ pipeline: **histogram matching الأول** (توحيد الإضاءة)
   وبعده `enhance_image` (ضغط الـ glare والتفاصيل) — لأن توحيد الإضاءة بيخلي
   عتبة الـ glare ثابتة لكل الصور.
