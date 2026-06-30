"""
ai_vision.py
============
Module موحد لفحص الصور بالذكاء الاصطناعي مع ذاكرة تدريب ذاتية.

── الاستخدام الأساسي ──────────────────────────────────────────────────────────
    from ai_vision import WaterDetector

    provider = WaterDetector.Gemini(model="gemini-2.0-flash", use_enhancement=True)
    result   = provider.check_multiple_images_for_water(["img1.jpg", "img2.jpg"])
    # result → '{"image_1": "Yes", "image_2": "No"}'

── الاستخدام مع TrainingMemory (ذاكرة التصحيح) ───────────────────────────────
    from ai_vision import WaterDetector

    memory   = WaterDetector.Memory(folder="training_memory", max_examples=10)
    provider = WaterDetector.Gemini(model="gemini-2.0-flash", memory=memory)

    result = provider.check_multiple_images_for_water(["img1.jpg"])

    # لو الموديل غلط → أضيف التصحيح والذاكرة بتتحمل تلقائياً المرة الجاية:
    memory.add("img1.jpg", correct="No", reason="light reflection, not water")

    # عرض الذاكرة:
    print(memory.stats())        # {"total": 1, "yes": 0, "no": 1, ...}
    print(memory.list_all())     # كل الأمثلة مع index كل واحدة

    # حذف مثال أو مسح الكل:
    memory.remove(0)
    memory.clear()

── Providers ──────────────────────────────────────────────────────────────────
    WaterDetector.Gemini   — Google Gemini API  (يدعم few-shot كاملاً)
    WaterDetector.Groq     — Groq API
    WaterDetector.Local    — Ollama أو LM Studio

── إضافة Provider جديد (3 خطوات) ─────────────────────────────────────────────
    1. اعمل class يورث من WaterDetector
    2. نفذ _try_check_images() فقط — الباقي جاهز
    3. اربطه:  WaterDetector.MyNew = MyNewProvider
"""

from __future__ import annotations

import os
import json
import time
import shutil
import base64
import cv2
from abc import ABC, abstractmethod
from datetime import datetime


# ══════════════════════════════════════════════════════════════════════════════
#  TRAINING MEMORY
# ══════════════════════════════════════════════════════════════════════════════

class TrainingMemory:
    """
    ذاكرة التدريب — بتحفظ الصور اللي غلط فيها الموديل على الـ disk،
    وبتحملهم تلقائياً كـ few-shot examples مع كل طلب.

    كل ما بتضيف غلطة، الموديل بيتذكرها من غير ما تعمل حاجة يدوي.

    الاستخدام:
        memory   = TrainingMemory(folder="training_memory", max_examples=10)
        provider = WaterDetector.Gemini(model="...", memory=memory)

        result = provider.check_multiple_images_for_water(["img1.jpg"])

        # لو الموديل غلط:
        memory.add("img1.jpg", correct="No", reason="light reflection, not water")
        # المرة الجاية هيتذكر ويصحح نفسه تلقائياً

    Operations:
        memory.add(path, correct, reason)  → يضيف مثال تصحيح
        memory.remove(index)               → يحذف مثال بالرقم
        memory.clear()                     → يمسح كل الذاكرة
        memory.list_all()                  → يعرض كل الأمثلة
        len(memory)                        → عدد الأمثلة الحالية
    """

    _INDEX = "memory_index.json"

    def __init__(self, folder: str = "training_memory", max_examples: int = 10):
        """
        Parameters
        ----------
        folder      : المجلد اللي هيتحفظ فيه الصور والـ index
        max_examples: أقصى عدد أمثلة بتتبعت للـ API في كل طلب
                      (عشان مايتجاوزش حد الـ tokens)
        """
        self.folder       = folder
        self.max_examples = max_examples
        os.makedirs(folder, exist_ok=True)
        self._index_path  = os.path.join(folder, self._INDEX)
        self._examples    = self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> list[dict]:
        if os.path.exists(self._index_path):
            try:
                with open(self._index_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save(self):
        with open(self._index_path, "w", encoding="utf-8") as f:
            json.dump(self._examples, f, indent=2, ensure_ascii=False)

    # ── Write ─────────────────────────────────────────────────────────────────

    def add(self, image_path: str, correct: str, reason: str = "") -> str:
        """
        يضيف صورة فشل فيها الموديل كـ training example.

        Parameters
        ----------
        image_path : مسار الصورة الأصلية
        correct    : الإجابة الصحيحة ("Yes" أو "No")
        reason     : سبب الخطأ — مهم لدقة الـ few-shot
                     مثال: "light reflection from metal, not a water drop"

        Returns: مسار نسخة الصورة المحفوظة في memory folder
        """
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"[Memory] Image not found: {image_path}")
        os.makedirs(self.folder, exist_ok=True)
        base     = os.path.basename(image_path)
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        new_name = f"{ts}_ShouldBe{correct}_{base}"
        new_path = os.path.join(self.folder, new_name)
        shutil.copy2(image_path, new_path)

        self._examples.append({
            "path":           new_path,
            "correct_result": correct,
            "reason":         reason or f"The correct answer is {correct}.",
            "source":         image_path,
            "added_at":       datetime.now().isoformat(),
        })
        self._save()
        print(f"[Memory] +1 → {new_name}  (total: {len(self._examples)})")
        return new_path

    def remove(self, index: int):
        """يحذف example بالرقم (0-indexed) وملفه من الـ disk."""
        if not 0 <= index < len(self._examples):
            raise IndexError(f"index {index} out of range (0..{len(self._examples) - 1})")
        ex = self._examples.pop(index)
        try:
            os.remove(ex["path"])
        except FileNotFoundError:
            pass
        self._save()
        print(f"[Memory] removed: {os.path.basename(ex['path'])}")

    def clear(self):
        """يمسح كل الأمثلة من الذاكرة والـ disk."""
        for ex in self._examples:
            try:
                os.remove(ex["path"])
            except FileNotFoundError:
                pass
        count = len(self._examples)
        self._examples = []
        self._save()
        print(f"[Memory] cleared {count} examples.")

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_examples(self) -> list[dict]:
        """
        يرجع آخر max_examples أمثلة صالحة (ملفاتها موجودة على الـ disk).
        الأحدث بيجي في الآخر — أقرب للـ prompt = أكتر تأثيراً.
        """
        valid = [e for e in self._examples if os.path.exists(e["path"])]
        if len(valid) != len(self._examples):   # ملفات اتحذفت من برّه
            self._examples = valid
            self._save()
        return valid[-self.max_examples:]

    def list_all(self) -> list[dict]:
        """يرجع كل الأمثلة مع رقم index كل واحدة للاستخدام في الـ GUI."""
        return [{"index": i, **ex} for i, ex in enumerate(self.get_examples())]

    def stats(self) -> dict:
        """يرجع إحصائيات سريعة عن الذاكرة."""
        examples = self.get_examples()
        yes_count = sum(1 for e in examples if e["correct_result"] == "Yes")
        no_count  = sum(1 for e in examples if e["correct_result"] == "No")
        return {
            "total":   len(examples),
            "yes":     yes_count,
            "no":      no_count,
            "folder":  self.folder,
            "max":     self.max_examples,
        }

    def __len__(self) -> int:
        return len(self.get_examples())

    def __repr__(self) -> str:
        return (f"TrainingMemory(folder={self.folder!r}, "
                f"examples={len(self)}/{self.max_examples})")


# ══════════════════════════════════════════════════════════════════════════════
#  SHORT-TERM MEMORY  (session-level rolling context)
# ══════════════════════════════════════════════════════════════════════════════

class ShortTermMemory:
    """
    ذاكرة قصيرة للجلسة — بتحتفظ بآخر N تحليل وبتحقنهم في كل prompt
    عشان الموديل يشوف أخطاؤه الأخيرة قبل ما يجاوب.

    على عكس TrainingMemory (disk-based few-shot)،
    الـ ShortTermMemory بتتمسح لما البرنامج بيقفل.

    الاستخدام:
        stm = ShortTermMemory(size=6)
        stm.add("img1.jpg", actual="No", model_said_water=True, was_correct=False)
        print(stm.build_context_block())   # نص يتحط في الـ prompt
    """

    def __init__(self, size: int = 6):
        from collections import deque
        self._buf: "deque[dict]" = __import__("collections").deque(maxlen=size)

    def add(self, image_name: str, actual: str, model_said_water: bool, was_correct: bool):
        """
        Parameters
        ----------
        image_name      : اسم الصورة
        actual          : "Yes" | "No"  (الإجابة الصحيحة)
        model_said_water: True إذا قال الموديل توجد مياه
        was_correct     : True إذا كانت إجابته صحيحة
        """
        self._buf.append({
            "image":   image_name,
            "actual":  actual,
            "model":   "Yes" if model_said_water else "No",
            "correct": was_correct,
        })

    def build_context_block(self) -> str:
        """يبني نص يُحقن كـ prefix في الـ prompt."""
        if not self._buf:
            return ""
        lines = ["[Session memory — learn from your recent decisions:]\n"]
        for e in self._buf:
            verdict = "✓ correct" if e["correct"] else "✗ wrong"
            lines.append(
                f"  • {e['image']}  |  correct={e['actual']}  |  you said={e['model']}  |  {verdict}"
            )
        total   = len(self._buf)
        correct = sum(1 for e in self._buf if e["correct"])
        lines.append(f"\nYour accuracy in the last {total} checks: {correct}/{total}\n")
        return "\n".join(lines)

    def accuracy(self) -> float:
        if not self._buf:
            return 0.0
        return sum(1 for e in self._buf if e["correct"]) / len(self._buf)

    def __len__(self) -> int:
        return len(self._buf)


# ══════════════════════════════════════════════════════════════════════════════
#  PARENT CLASS
# ══════════════════════════════════════════════════════════════════════════════

class WaterDetector(ABC):
    """
    الكلاس الأب المشترك لكل providers.

    بيوفر:
      - enhance_image()                   : تحسين الصورة (Denoising → CLAHE → Sharpening)
      - check_multiple_images_for_water() : الـ public API مع retry تلقائي
      - _try_check_images()               : abstract — كل provider بينفذه بنفسه

    Providers:
      WaterDetector.Gemini
      WaterDetector.Groq
      WaterDetector.Local
    """

    _RETRYABLE = ("503", "unavailable", "429", "rate limit",
                  "timeout", "resource_exhausted", "quota")

    def __init__(
        self,
        model: str,
        use_enhancement: bool = False,
        max_retries: int = 4,
        retry_delay: float = 5.0,
        memory: TrainingMemory | None = None,
        short_term_memory: ShortTermMemory | None = None,
    ):
        """
        Parameters
        ----------
        model             : اسم الموديل
        use_enhancement   : True → الصور بتتحسن تلقائياً قبل الإرسال
        max_retries       : أقصى عدد محاولات عند فشل مؤقت
        retry_delay       : ثواني الانتظار (بتتضاعف كل محاولة)
        memory            : TrainingMemory — few-shot examples محفوظة على الـ disk
        short_term_memory : ShortTermMemory — context الجلسة الحالية (session-level)
        """
        self.model             = model
        self.use_enhancement   = use_enhancement
        self.max_retries       = max_retries
        self.retry_delay       = retry_delay
        self.memory            = memory
        self.short_term_memory = short_term_memory or ShortTermMemory()

    # ── Shared: Image Enhancement ─────────────────────────────────────────────

    def enhance_image(self, img_bgr):
        """
        تحسين جودة الصورة لإظهار تفاصيل المياه للموديل.
        المراحل: Denoising → CLAHE على قناة L → Unsharp Masking
        """
        if img_bgr is None:
            return None

        denoised = cv2.fastNlMeansDenoisingColored(
            img_bgr, None, h=3, hColor=3,
            templateWindowSize=7, searchWindowSize=21,
        )
        lab = cv2.cvtColor(denoised, cv2.COLOR_BGR2Lab)
        l_ch, a_ch, b_ch = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l_ch = clahe.apply(l_ch)
        enhanced = cv2.cvtColor(cv2.merge((l_ch, a_ch, b_ch)), cv2.COLOR_Lab2BGR)
        blurred = cv2.GaussianBlur(enhanced, (0, 0), 3)
        return cv2.addWeighted(enhanced, 1.5, blurred, -0.5, 0)

    # ── Shared: Public API with Retry ─────────────────────────────────────────

    def check_multiple_images_for_water(
        self,
        image_paths: list[str],
        few_shot_examples: list[dict] | None = None,
    ) -> str:
        """
        يفحص قائمة صور ويرجع JSON: '{"image_1": "Yes", "image_2": "No"}'

        Parameters
        ----------
        image_paths      : قائمة مسارات الصور
        few_shot_examples: أمثلة إضافية يدوية (بتتضاف بعد أمثلة الـ memory)
                           [{"path": "...", "correct_result": "Yes/No", "reason": "..."}]

        لو الـ provider عنده memory مربوطة، أمثلة الذاكرة بتتحمل تلقائياً
        وبتيجي قبل أي few_shot_examples يدوية.
        """
        # ── merge: memory examples أولاً ثم أي examples يدوية ────────────────
        examples: list[dict] = []
        if self.memory is not None:
            examples = list(self.memory.get_examples())
        if few_shot_examples:
            # أضيف اليدوية بس لو مش موجودة بالفعل في الـ memory
            mem_paths = {e["path"] for e in examples}
            examples += [e for e in few_shot_examples if e.get("path") not in mem_paths]

        final_examples = examples or None

        for attempt in range(1, self.max_retries + 1):
            result = self._try_check_images(image_paths, final_examples)

            if not result.startswith("Error:"):
                return result

            is_retryable = any(k in result.lower() for k in self._RETRYABLE)
            if is_retryable and attempt < self.max_retries:
                wait = self.retry_delay * attempt
                print(f"[AI] محاولة {attempt}/{self.max_retries} فشلت — انتظار {wait:.0f}s...")
                time.sleep(wait)
                continue
            break

        print(f"[AI] فشل نهائي بعد {self.max_retries} محاولة: {result}")
        return result

    # ── Failed-folders memory loader ──────────────────────────────────────────

    def _load_failed_as_fewshot(self,
                                 has_water_dir: str = "failed_has_water",
                                 no_water_dir:  str = "failed_no_water",
                                 max_samples:   int = 10) -> list[dict]:
        """
        يقرأ من failed_has_water و failed_no_water ويبنيهم كـ few-shot examples
        تلقائياً — ده اللي بيخلي الموديل يتذكر أخطاؤه السابقة.

        max_samples: أقصى عدد صور يتبعت (نص من كل فولدر).
        """
        import random
        exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
        half = max(1, max_samples // 2)
        examples = []

        for folder, label, reason in [
            (has_water_dir, "Yes", "This image has water — the model previously missed it."),
            (no_water_dir,  "No",  "No water here — the model was fooled before, likely by reflections."),
        ]:
            if not os.path.exists(folder):
                continue
            files = [
                os.path.join(folder, f)
                for f in os.listdir(folder)
                if os.path.splitext(f)[1].lower() in exts
            ]
            random.shuffle(files)
            for path in files[:half]:
                examples.append({
                    "path":           path,
                    "correct_result": label,
                    "reason":         reason,
                })

        return examples

    # ── MODE: run ─────────────────────────────────────────────────────────────

    def run(self, image_paths: list[str],
            failed_has_water: str = "failed_has_water",
            failed_no_water:  str = "failed_no_water",
            output_json:      str = "result.json") -> dict:
        """
        --run : بتباصيه قايمة صور محددة، يحللهم تلقائياً بدون أسئلة.

        الـ failed folders بتتحمل تلقائياً كـ few-shot memory
        عشان الموديل يتذكر أخطاؤه السابقة ويحسن الدقة.

        Parameters
        ----------
        image_paths      : قايمة مسارات الصور اللي عايز تفحصها
        failed_has_water : فولدر الصور اللي فيها مياه وغلط فيها الموديل قبل كده
        failed_no_water  : فولدر الصور اللي مفيهاش مياه وانخدع بيها الموديل قبل كده
        output_json      : مسار ملف JSON اللي هتتحفظ فيه النتيجة (افتراضي: result.json)
        """
        import json as _j

        if not image_paths:
            print("❌ مفيش صور — باصي قايمة مسارات.")
            return {}

        # تحميل الذاكرة من الفولدرات الفاشلة
        fewshot = self._load_failed_as_fewshot(failed_has_water, failed_no_water)
        loaded  = len([e for e in fewshot if e["correct_result"] == "Yes"])
        loaded2 = len([e for e in fewshot if e["correct_result"] == "No"])

        print("\n" + "═" * 54)
        print("  🔍  مود التشغيل  —  تلقائي بالكامل")
        print(f"  🧠  ذاكرة محملة: {loaded} صورة بمياه، {loaded2} بدون مياه")
        print("═" * 54)

        # النتيجة النهائية — نفس format القديم {"image_1": "Yes", ...}
        final: dict = {}

        for idx, path in enumerate(image_paths, 1):
            if not os.path.exists(path):
                print(f"  ⚠️  مش موجودة: {path}")
                final[f"image_{idx}"] = "Error"
                continue

            name = os.path.basename(path)
            print(f"\n  📸  {name}")

            raw = self.check_multiple_images_for_water([path], few_shot_examples=fewshot or None)
            try:
                parsed    = _j.loads(raw)
                has_water = parsed.get("image_1", "No") == "Yes"
            except Exception:
                has_water = "yes" in raw.lower()

            verdict = "🟢 توجد مياه" if has_water else "⚪ لا توجد مياه"
            print(f"  {verdict}")
            print("─" * 54)

            # BUG-023: في --run mode مفيش تقييم بشري → لا تضيف لـ short_term_memory
            # (was_correct=True كان غلط لأننا مش عارفين الإجابة الصحيحة)
            final[f"image_{idx}"] = "Yes" if has_water else "No"

        # حفظ النتيجة كـ JSON file
        with open(output_json, "w", encoding="utf-8") as f:
            _j.dump(final, f, indent=2, ensure_ascii=False)

        water = sum(1 for v in final.values() if v == "Yes")
        print(f"\n📊 ملخص: {len(final)} صورة  |  بها مياه: {water}  |  بدون: {len(final)-water}")
        print(f"💾 النتيجة اتحفظت في: {output_json}")
        return final

    # ── MODE: train ───────────────────────────────────────────────────────────

    def train(self, image_paths: list[str],
              failed_has_water: str = "failed_has_water",
              failed_no_water:  str = "failed_no_water"):
        """
        --train : بتباصيه قايمة صور، هو يحللها وأنت تقوله صح ولا غلط.

        - اللي غلط فيها → بتتحفظ في failed folder تلقائياً
        - المرة الجاية هيتذكرها كـ few-shot memory ويحسن الدقة
        - الـ failed folders بتتحمل تلقائياً كـ context في كل فحص

        Parameters
        ----------
        image_paths      : قايمة مسارات الصور اللي عايز تدرب عليها
        failed_has_water : فولدر الأخطاء (الصور اللي فيها مياه)
        failed_no_water  : فولدر الأخطاء (الصور اللي مفيهاش مياه)
        """
        import json as _j

        os.makedirs(failed_has_water, exist_ok=True)
        os.makedirs(failed_no_water,  exist_ok=True)

        if not image_paths:
            print("❌ مفيش صور — باصي قايمة مسارات.")
            return

        # تحميل الذاكرة من الفولدرات الفاشلة
        fewshot = self._load_failed_as_fewshot(failed_has_water, failed_no_water)
        loaded  = len([e for e in fewshot if e["correct_result"] == "Yes"])
        loaded2 = len([e for e in fewshot if e["correct_result"] == "No"])

        print("\n" + "═" * 54)
        print("  🎓  مود التدريب  —  أنت تقيّم كل إجابة")
        print(f"  🧠  ذاكرة محملة: {loaded} صورة بمياه، {loaded2} بدون مياه")
        print("═" * 54)

        correct_count = total = 0

        for path in image_paths:
            if not os.path.exists(path):
                print(f"  ⚠️  مش موجودة: {path}")
                continue

            name = os.path.basename(path)
            print(f"\n  📸  {name}")

            raw = self.check_multiple_images_for_water([path], few_shot_examples=fewshot or None)
            try:
                parsed    = _j.loads(raw)
                has_water = parsed.get("image_1", "No") == "Yes"
            except Exception:
                has_water = "yes" in raw.lower()

            verdict = "🟢 توجد مياه" if has_water else "⚪ لا توجد مياه"
            print(f"  🤖  {verdict}\n")

            # تقييم المستخدم
            while True:
                fb = input("  إجابة صحيحة؟ (y / n / q للخروج): ").strip().lower()
                if fb in {"y", "n", "q"}:
                    break
            if fb == "q":
                break

            while True:
                actual_raw = input("  الصورة فيها مياه فعلاً؟ (y / n): ").strip().lower()
                if actual_raw in {"y", "n"}:
                    break

            actual_str = "Yes" if actual_raw == "y" else "No"
            is_correct = (fb == "y")
            total     += 1
            correct_count += int(is_correct)

            self.short_term_memory.add(name, actual_str, has_water, is_correct)

            if not is_correct:
                # حفظ في الفولدر المناسب
                dst_dir = failed_has_water if actual_str == "Yes" else failed_no_water
                dst = os.path.join(dst_dir, name)
                if not os.path.exists(dst):
                    shutil.copy(path, dst)
                print(f"  💾 اتحفظت في '{dst_dir}' — هيتذكرها المرة الجاية.")
                # تحديث الـ fewshot في نفس الجلسة
                fewshot.append({
                    "path":           dst,
                    "correct_result": actual_str,
                    "reason":         f"Model said {'Yes' if has_water else 'No'} but correct is {actual_str}.",
                })

            pct = correct_count / total * 100
            print(f"  {'✅' if is_correct else '❌'}  دقة الجلسة: {correct_count}/{total} ({pct:.0f}%)")
            print("─" * 54)

        print(f"\n📊 انتهت الجلسة — النتيجة: {correct_count}/{total}")

    # ── Abstract: كل provider لازم ينفذه ────────────────────────────────────

    @abstractmethod
    def _try_check_images(
        self,
        image_paths: list[str],
        few_shot_examples: list[dict] | None = None,
    ) -> str:
        """
        محاولة واحدة. لازم ترجع:
          - JSON string عند النجاح  →  '{"image_1": "Yes", ...}'
          - "Error: ..."            عند الفشل
        """
        ...


# ══════════════════════════════════════════════════════════════════════════════
#  INTERFACE: Gemini
# ══════════════════════════════════════════════════════════════════════════════

class _Gemini(WaterDetector):
    """
    WaterDetector.Gemini — Google Gemini API.

    .env:  GENAI_API_KEY, MODEL

    مثال:
        p = WaterDetector.Gemini(model="gemini-2.0-flash", use_enhancement=True)
        p.check_multiple_images_for_water(["img1.jpg"], few_shot_examples=[...])
    """

    def __init__(self, model: str, use_enhancement: bool = False,
                 max_retries: int = 4, retry_delay: float = 5.0,
                 api_key: str | None = None,
                 memory: TrainingMemory | None = None):
        super().__init__(model, use_enhancement, max_retries, retry_delay, memory)
        from google import genai
        from google.genai import types as _t
        self._client = genai.Client(api_key=api_key or os.getenv("GENAI_API_KEY", ""))
        self._t = _t

    def _try_check_images(self, image_paths, few_shot_examples=None):
        from PIL import Image as _Image
        try:
            contents = []

            # Few-Shot examples
            if few_shot_examples:
                contents.append("Before analyzing, learn from these examples:")
                for idx, ex in enumerate(few_shot_examples, 1):
                    if os.path.exists(ex["path"]):
                        img = cv2.imread(ex["path"])
                        if self.use_enhancement:
                            img = self.enhance_image(img)
                        pil = _Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
                        contents += [f"Example {idx}:", pil,
                                     f"Answer: {ex['correct_result']}. Reason: {ex.get('reason','')}"]
                contents.append("Now analyze the following images:")

            # الصور الجديدة
            valid = 0
            for i, path in enumerate(image_paths, 1):
                if not os.path.exists(path):
                    print(f"Warning: {path} not found.")
                    continue
                img = cv2.imread(path)
                if self.use_enhancement:
                    img = self.enhance_image(img)
                pil = _Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
                contents += [f"This is Image {i}:", pil]
                valid += 1

            if valid == 0:
                return "Error: No valid images provided."

            contents.append(
                "Determine if water is present in each numbered image. "
                "Respond according to the schema."
            )

            t = self._t
            props = {
                f"image_{i}": t.Schema(type=t.Type.STRING, enum=["Yes", "No"],
                                       description=f"Result for image {i}")
                for i in range(1, len(image_paths) + 1)
            }
            schema = t.Schema(
                type=t.Type.OBJECT,
                properties=props,
                required=list(props.keys()),
            )

            resp = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=t.GenerateContentConfig(
                    system_instruction=(
                        "You are a precise quality control assistant. "
                        "Output JSON mapping each image number to 'Yes' or 'No'."
                    ),
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0.0,
                ),
            )
            return resp.text.strip()
        except Exception as e:
            return f"Error: {e}"


# ══════════════════════════════════════════════════════════════════════════════
#  INTERFACE:
# ══════════════════════════════════════════════════════════════════════════════
#  INTERFACE: Groq
# ══════════════════════════════════════════════════════════════════════════════

class _Groq(WaterDetector):
    """
    WaterDetector.Groq — Groq API (vision-capable models).

    .env:  GROQ_API_KEY

    مثال:
        p = WaterDetector.Groq(model="meta-llama/llama-4-scout-17b-16e-instruct")
        p.check_multiple_images_for_water(["img1.jpg"])
    """

    def __init__(self, model: str, use_enhancement: bool = False,
                 max_retries: int = 4, retry_delay: float = 5.0,
                 api_key: str | None = None,
                 memory: "TrainingMemory | None" = None):
        super().__init__(model, use_enhancement, max_retries, retry_delay, memory)
        from groq import Groq as _GroqClient
        self._client = _GroqClient(api_key=api_key or os.getenv("GROQ_API_KEY", ""))

    @staticmethod
    def _img_to_b64(path: str, enhance_fn=None) -> str | None:
        img = cv2.imread(path)
        if img is None:
            return None
        if enhance_fn:
            img = enhance_fn(img)
        ok, buf = cv2.imencode(".jpg", img)
        return base64.b64encode(buf).decode() if ok else None

    def _try_check_images(self, image_paths, few_shot_examples=None):
        import json as _json
        try:
            messages = []

            # System prompt
            messages.append({
                "role": "system",
                "content": (
                    "You are a precise quality control assistant. "
                    "Analyze images for water presence. "
                    "Respond ONLY with valid JSON: "
                    "{\"image_1\": \"Yes\"|\"No\", \"image_2\": ...}"
                )
            })

            # Few-shot examples
            if few_shot_examples:
                for idx, ex in enumerate(few_shot_examples, 1):
                    b64 = self._img_to_b64(
                        ex["path"],
                        self.enhance_image if self.use_enhancement else None
                    )
                    if b64 is None:
                        continue
                    messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text",
                             "text": f"Example {idx}: Is water present?"},
                            {"type": "image_url",
                             "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        ]
                    })
                    messages.append({
                        "role": "assistant",
                        "content": (
                            f"Answer: {ex['correct_result']}. "
                            f"Reason: {ex.get('reason', '')}"
                        )
                    })

            # New images
            user_content = [{"type": "text",
                             "text": "Analyze the following images for water presence:"}]
            valid = 0
            for i, path in enumerate(image_paths, 1):
                if not os.path.exists(path):
                    print(f"Warning: {path} not found.")
                    continue
                b64 = self._img_to_b64(
                    path,
                    self.enhance_image if self.use_enhancement else None
                )
                if b64 is None:
                    continue
                user_content += [
                    {"type": "text", "text": f"Image {i}:"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ]
                valid += 1

            if valid == 0:
                return "Error: No valid images provided."

            user_content.append({
                "type": "text",
                "text": (
                    f"Return JSON with keys image_1 through image_{len(image_paths)}, "
                    "each 'Yes' or 'No'."
                )
            })
            messages.append({"role": "user", "content": user_content})

            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=512,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            return f"Error: {e}"


# ══════════════════════════════════════════════════════════════════════════════
#  INTERFACE: Local  (Ollama / LM Studio)
# ══════════════════════════════════════════════════════════════════════════════

class _Local(WaterDetector):
    """
    WaterDetector.Local — Ollama أو LM Studio (local inference).

    مثال:
        p = WaterDetector.Local(model="llava", backend="ollama")
        p.check_multiple_images_for_water(["img1.jpg"])
    """

    _BACKENDS = {
        "ollama":    "http://localhost:11434/api/chat",
        "lm_studio": "http://localhost:1234/v1/chat/completions",
    }

    def __init__(self, model: str, backend: str = "ollama",
                 use_enhancement: bool = False,
                 max_retries: int = 4, retry_delay: float = 5.0,
                 memory: "TrainingMemory | None" = None):
        super().__init__(model, use_enhancement, max_retries, retry_delay, memory)
        if backend not in self._BACKENDS:
            raise ValueError(f"backend must be one of {list(self._BACKENDS)}")
        self.backend  = backend
        self._api_url = self._BACKENDS[backend]

    @staticmethod
    def _img_to_b64(path: str, enhance_fn=None) -> str | None:
        img = cv2.imread(path)
        if img is None:
            return None
        if enhance_fn:
            img = enhance_fn(img)
        ok, buf = cv2.imencode(".jpg", img)
        return base64.b64encode(buf).decode() if ok else None

    def _try_check_images(self, image_paths, few_shot_examples=None):
        import json as _json
        import urllib.request as _req
        try:
            results = {}
            enhance_fn = self.enhance_image if self.use_enhancement else None

            for i, path in enumerate(image_paths, 1):
                if not os.path.exists(path):
                    print(f"Warning: {path} not found.")
                    results[f"image_{i}"] = "Error"
                    continue

                b64 = self._img_to_b64(path, enhance_fn)
                if b64 is None:
                    results[f"image_{i}"] = "Error"
                    continue

                messages = [
                    {"role": "system",
                     "content": (
                         "You are a precise quality control assistant. "
                         "Answer only 'Yes' or 'No' about water presence."
                     )}
                ]

                # Few-shot
                if few_shot_examples:
                    for ex in few_shot_examples:
                        ex_b64 = self._img_to_b64(ex["path"], enhance_fn)
                        if ex_b64 is None:
                            continue
                        messages.append({
                            "role": "user",
                            "content": [
                                {"type": "text",  "text": "Is water present in this image?"},
                                {"type": "image_url",
                                 "image_url": {"url": f"data:image/jpeg;base64,{ex_b64}"}},
                            ]
                        })
                        messages.append({
                            "role": "assistant",
                            "content": (
                                f"{ex['correct_result']}. "
                                f"{ex.get('reason', '')}"
                            )
                        })

                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text",  "text": "Is water present in this image? Answer Yes or No only."},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ]
                })

                payload = _json.dumps({
                    "model":    self.model,
                    "messages": messages,
                    "stream":   False,
                    "temperature": 0.0,
                }).encode()

                req = _req.Request(
                    self._api_url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with _req.urlopen(req, timeout=60) as r:
                    data = _json.loads(r.read())

                # Ollama: data["message"]["content"]
                # LM Studio: data["choices"][0]["message"]["content"]
                if "message" in data:
                    answer = data["message"]["content"].strip()
                else:
                    answer = data["choices"][0]["message"]["content"].strip()

                results[f"image_{i}"] = "Yes" if "yes" in answer.lower() else "No"

            return _json.dumps(results)
        except Exception as e:
            return f"Error: {e}"


# ══════════════════════════════════════════════════════════════════════════════
#  Attach interfaces + memory as class attributes
# ══════════════════════════════════════════════════════════════════════════════

WaterDetector.Gemini       = _Gemini
WaterDetector.Groq         = _Groq
WaterDetector.Local        = _Local
WaterDetector.Memory       = TrainingMemory
WaterDetector.SessionMemory = ShortTermMemory


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers (backward-compat / standalone use)
# ══════════════════════════════════════════════════════════════════════════════

def save_failed_prediction(image_paths: list[str], ai_response: str,
                           save_dir: str = "failed_predictions") -> str:
    """يحفظ الصور اللي فشل فيها الـ AI في مجلد لمراجعتها لاحقاً."""
    import shutil
    os.makedirs(save_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = os.path.join(save_dir, ts)
    os.makedirs(folder, exist_ok=True)
    for p in image_paths:
        if os.path.exists(p):
            shutil.copy(p, folder)
    meta = {"ai_response": ai_response, "images": image_paths, "saved_at": ts}
    with open(os.path.join(folder, "meta.json"), "w") as f:
        import json as _j; _j.dump(meta, f, indent=2)
    return folder


def run_feedback_loop(provider: WaterDetector, memory: TrainingMemory,
                      image_paths: list[str], correct_result: str,
                      reason: str = "") -> str:
    """
    بعد ما الـ AI يغلط، خزّن الصورة في الـ memory وأعد التشغيل.

    :return: النتيجة الجديدة بعد التدريب
    """
    for p in image_paths:
        memory.add(p, correct_result, reason)
    return provider.check_multiple_images_for_water(image_paths)


# ══════════════════════════════════════════════════════════════════════════════
#  CLI  —  python ai_vision.py --run | --train
# ══════════════════════════════════════════════════════════════════════════════

def _build_provider(args) -> WaterDetector:
    """يبني الـ provider المناسب بناءً على args."""
    stm = ShortTermMemory(size=args.stm_size)

    kwargs = dict(
        model             = args.model,
        use_enhancement   = args.enhance,
        short_term_memory = stm,
    )

    p = args.provider.lower()
    if p == "groq":
        api_key = args.api_key or os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise SystemExit("❌ مطلوب GROQ_API_KEY — مرره عبر --api-key أو متغير البيئة.")
        return _Groq(api_key=api_key, **kwargs)

    elif p == "gemini":
        api_key = args.api_key or os.environ.get("GENAI_API_KEY")
        if not api_key:
            raise SystemExit("❌ مطلوب GENAI_API_KEY — مرره عبر --api-key أو متغير البيئة.")
        return _Gemini(api_key=api_key, **kwargs)

    elif p == "local":
        backend = getattr(args, "backend", "ollama")
        return _Local(backend=backend, **kwargs)

    raise SystemExit(f"❌ provider غير معروف: {args.provider}  (groq | gemini | local)")


def main():
    import argparse, sys

    parser = argparse.ArgumentParser(
        prog="ai_vision",
        description="🔬 WaterDetector AI Agent — كاشف التسريب",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "أمثلة:\n"
            "  python ai_vision.py --run   --images img1.jpg img2.jpg img3.jpg\n"
            "  python ai_vision.py --train --images img1.jpg img2.jpg\n"
            "  python ai_vision.py --run   --provider gemini --model gemini-2.0-flash --images img1.jpg\n"
        ),
    )

    # ── Mode (required, mutually exclusive) ──────────────────────────────────
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run",   action="store_true", help="مود التشغيل: تحليل تلقائي بدون أسئلة.")
    mode.add_argument("--train", action="store_true", help="مود التدريب: أنت تقيّم كل إجابة.")

    # ── Provider ──────────────────────────────────────────────────────────────
    parser.add_argument("--provider", default="groq",
                        choices=["groq", "gemini", "local"],
                        help="الـ AI provider (افتراضي: groq).")
    parser.add_argument("--model", default="llama-3.2-11b-vision-preview",
                        help="اسم الموديل.")
    parser.add_argument("--api-key", metavar="KEY", default=None,
                        help="API key (أو استخدم GROQ_API_KEY / GENAI_API_KEY كمتغير بيئة).")
    parser.add_argument("--backend", default="ollama", choices=["ollama", "lm_studio"],
                        help="Local backend (افتراضي: ollama).")

    # ── Images ────────────────────────────────────────────────────────────────
    parser.add_argument("--images", nargs="+", metavar="IMG",
                        help="مسارات الصور المراد فحصها (مثال: img1.jpg img2.jpg).")
    parser.add_argument("--enhance", action="store_true",
                        help="تفعيل تحسين الصور قبل الإرسال.")

    # ── Failed folders (memory) ───────────────────────────────────────────────
    parser.add_argument("--failed-has-water", default="failed_has_water", metavar="PATH",
                        help="فولدر الصور اللي فيها مياه وغلط فيها الموديل (افتراضي: failed_has_water/).")
    parser.add_argument("--failed-no-water", default="failed_no_water", metavar="PATH",
                        help="فولدر الصور اللي مفيهاش مياه وانخدع بيها الموديل (افتراضي: failed_no_water/).")
    parser.add_argument("--output", default="result.json", metavar="FILE",
                        help="مسار ملف JSON للنتيجة في --run (افتراضي: result.json).")
    parser.add_argument("--stm-size", type=int, default=6, metavar="N",
                        help="حجم الذاكرة القصيرة للجلسة (افتراضي: 6).")

    args = parser.parse_args()

    print("\n╔══════════════════════════════════════════════════╗")
    print("║   🔬  WaterDetector AI Agent — كاشف التسريب      ║")
    print("╚══════════════════════════════════════════════════╝")
    if not args.images:
        print("❌ لازم تحدد صور عبر --images img1.jpg img2.jpg ...")
        sys.exit(1)

    print(f"   Provider       : {args.provider.upper()}")
    print(f"   Model          : {args.model}")
    print(f"   Images         : {len(args.images)} صورة")
    print(f"   Failed (water) : {args.failed_has_water}")
    print(f"   Failed (dry)   : {args.failed_no_water}\n")

    provider = _build_provider(args)

    if args.run:
        provider.run(
            image_paths      = args.images,
            failed_has_water = args.failed_has_water,
            failed_no_water  = args.failed_no_water,
            output_json      = args.output,
        )
    elif args.train:
        provider.train(
            image_paths      = args.images,
            failed_has_water = args.failed_has_water,
            failed_no_water  = args.failed_no_water,
        )


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    main()
