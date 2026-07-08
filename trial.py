# ══════════════════════════════════════════════════════════════════════════════
#  INTERFACE: Bynara (OpenAI-compatible)
# ══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import os
import json
import time
import shutil
import base64
import cv2
from abc import ABC, abstractmethod
from datetime import datetime

# ── FIX: تحميل .env على مستوى الـ module ──────────────────────────────────────
# عشان GENAI_API_KEY / GROQ_API_KEY يتحملوا مهما كانت طريقة التشغيل
# (python ClientsClass.py / web_server.py / ai_vision.py — كلهم بيعدوا من هنا)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass  # dotenv مش متسطب — نعتمد على متغيرات البيئة (Docker)


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

            # FIX: أي خطأ API يرجع "Error" — مش "No" بصمت
            if raw.startswith("Error:"):
                print(f"  ❌ AI error: {raw}")
                final[f"image_{idx}"] = "Error"
                continue

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

            # FIX: أي خطأ API يرجع "Error" — مش "No" بصمت
            if raw.startswith("Error:"):
                print(f"  ❌ AI error: {raw}")
                final[f"image_{idx}"] = "Error"
                continue

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




class _Bynara(WaterDetector):
    def __init__(self, model: str, use_enhancement: bool = False,
                 max_retries: int = 4, retry_delay: float = 5.0,
                 api_key: str = "sk-nry-tTKE33ayTskXZjBnAorU-UOMXQkHpNAOpNLnjae-hcg", memory=None):
        super().__init__(model, use_enhancement, max_retries, retry_delay, memory)
        from openai import OpenAI
        self._client = OpenAI(
            base_url="https://router.bynara.id/v1",
            api_key=api_key
        )

    def _try_check_images(self, image_paths, few_shot_examples=None):
        import base64
        try:
            messages = [{"role": "system", "content": "You are a precise quality control assistant. Analyze these 3 images for water. Return ONLY JSON like: {'image_1': 'Yes', 'image_2': 'No', 'image_3': 'Yes'}"}]
            
            content = []
            for i, path in enumerate(image_paths, 1):
                if not os.path.exists(path): continue
                
                # تحويل الصورة لـ base64
                img = cv2.imread(path)
                if self.use_enhancement: img = self.enhance_image(img)
                _, buffer = cv2.imencode(".jpg", img)
                b64 = base64.b64encode(buffer).decode("utf-8")
                
                content.append({"type": "text", "text": f"Image {i}:"})
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            
            messages.append({"role": "user", "content": content})

            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"}
            )
            return response.choices[0].message.content
        except Exception as e:
            return f"Error: {e}"

# قم بربطه بالكلاس الأب
WaterDetector.Bynara = _Bynara



if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    
    # استخدام الـ Bynara الجديد
    ai = WaterDetector.Bynara(model="mistral-medium-3-5") 
    
    # قائمة بـ 3 صور
    image_list = [
        "D:\\hhhhhhhhhh\\fresh-new\\captures_standalone\\capture_20260708_095408_0002.jpg", 
        "D:\\hhhhhhhhhh\\fresh-new\\captures_standalone\\capture_20260708_095414_0005.jpg",
        "D:\\hhhhhhhhhh\\fresh-new\\captures_standalone\\capture_20260708_095618_0011.jpg"
      
    ]
    
    # فحص الكل في طلب واحد
    res1 = ai.check_multiple_images_for_water(image_list)
    
    # res1 ستكون: '{"image_1": "Yes", "image_2": "No", "image_3": "No"}'
    print(res1)