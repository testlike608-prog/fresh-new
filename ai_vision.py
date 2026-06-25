"""
ai_vision.py
============
Module موحد لفحص الصور بالذكاء الاصطناعي.

الاستخدام:
    from ai_vision import WaterDetector

    provider = WaterDetector.Gemini(model="gemini-2.0-flash", use_enhancement=True)
    result   = provider.check_multiple_images_for_water(["img1.jpg", "img2.jpg"])
    # result → '{"image_1": "Yes", "image_2": "No"}'

Providers المتاحة:
    WaterDetector.Gemini   — Google Gemini API  (يدعم Few-Shot)
    WaterDetector.Groq     — Groq API
    WaterDetector.Local    — Ollama أو LM Studio

إضافة Provider جديد (3 خطوات):
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
from dotenv import load_dotenv


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
    ):
        """
        Parameters
        ----------
        model          : اسم الموديل
        use_enhancement: True → الصور بتتحسن تلقائياً قبل الإرسال
        max_retries    : أقصى عدد محاولات عند فشل مؤقت
        retry_delay    : ثواني الانتظار (بتتضاعف كل محاولة)
        """
        self.model = model
        self.use_enhancement = use_enhancement
        self.max_retries = max_retries
        self.retry_delay = retry_delay

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
        few_shot_examples: [{"path": "...", "correct_result": "Yes/No", "reason": "..."}]
                           (مدعوم في Gemini فقط)
        """
        for attempt in range(1, self.max_retries + 1):
            result = self._try_check_images(image_paths, few_shot_examples)

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
                 api_key: str | None = None):
        super().__init__(model, use_enhancement, max_retries, retry_delay)
        from google import genai
        from google.genai import types as _t
        load_dotenv()
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
#  INTERFACE: Groq
# ══════════════════════════════════════════════════════════════════════════════

class _Groq(WaterDetector):
    """
    WaterDetector.Groq — Groq API (OpenAI-compatible format).

    .env:  GROQ_API_KEY, MODEL

    مثال:
        p = WaterDetector.Groq(model="meta-llama/llama-4-scout-17b-16e-instruct")
        p.check_multiple_images_for_water(["img1.jpg", "img2.jpg"])
    """

    def __init__(self, model: str, use_enhancement: bool = False,
                 max_retries: int = 4, retry_delay: float = 5.0,
                 api_key: str | None = None):
        super().__init__(model, use_enhancement, max_retries, retry_delay)
        load_dotenv()
        from groq import Groq
        self._client = Groq(api_key=api_key or os.getenv("GROQ_API_KEY"))

    @staticmethod
    def _to_base64(img_bgr) -> str:
        ok, buf = cv2.imencode(".jpg", img_bgr)
        if not ok:
            raise ValueError("JPEG encode failed.")
        return base64.b64encode(buf).decode("utf-8")

    def _try_check_images(self, image_paths, few_shot_examples=None):
        try:
            content = []
            keys = []

            for i, path in enumerate(image_paths, 1):
                if not os.path.exists(path):
                    print(f"Warning: {path} not found.")
                    continue
                img = cv2.imread(path)
                if self.use_enhancement:
                    img = self.enhance_image(img)
                b64 = self._to_base64(img)
                content += [
                    {"type": "text", "text": f"This is Image {i}:"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ]
                keys.append(f"image_{i}")

            if not content:
                return "Error: No valid images provided."

            structure = "{" + ", ".join(f'"{k}": "Yes or No"' for k in keys) + "}"
            content.append({
                "type": "text",
                "text": (
                    "Determine if water is present in each numbered image. "
                    f"Output valid JSON with exactly: {structure}. No extra text."
                ),
            })

            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system",
                     "content": "Precise quality control assistant. Output strictly valid JSON."},
                    {"role": "user", "content": content},
                ],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            return f"Error: {e}"


# ══════════════════════════════════════════════════════════════════════════════
#  INTERFACE: Local  (Ollama أو LM Studio)
# ══════════════════════════════════════════════════════════════════════════════

class _Local(WaterDetector):
    """
    WaterDetector.Local — Local models عبر Ollama أو LM Studio.

    بيشتغل صورة صورة ويجمع النتايج في JSON.

    Parameters
    ----------
    model    : اسم الموديل (مثلاً "qwen2-vl")
    backend  : "ollama" أو "lm_studio"
    base_url : رابط الـ server  (الافتراضي: http://127.0.0.1:1234/v1)

    مثال:
        p = WaterDetector.Local(model="qwen2-vl", backend="lm_studio")
        p.check_multiple_images_for_water(["img1.jpg"])
    """

    _MAX_SIZE    = 768
    _JPEG_QUALITY = 85

    def __init__(self, model: str, backend: str = "lm_studio",
                 base_url: str = "http://127.0.0.1:1234/v1",
                 use_enhancement: bool = False,
                 max_retries: int = 4, retry_delay: float = 5.0):
        if backend not in ("ollama", "lm_studio"):
            raise ValueError('backend لازم يكون "ollama" أو "lm_studio"')
        super().__init__(model, use_enhancement, max_retries, retry_delay)
        self.backend = backend
        if backend == "lm_studio":
            from openai import OpenAI
            self._client = OpenAI(base_url=base_url, api_key="lm-studio")

    def _prepare(self, img_bgr):
        """تحسين + resize قبل الإرسال للـ local model."""
        if self.use_enhancement:
            img_bgr = self.enhance_image(img_bgr)
        h, w = img_bgr.shape[:2]
        if max(h, w) > self._MAX_SIZE:
            s = self._MAX_SIZE / max(h, w)
            img_bgr = cv2.resize(img_bgr, (int(w * s), int(h * s)),
                                 interpolation=cv2.INTER_AREA)
        return img_bgr

    def _check_one(self, img_bgr) -> str:
        """يرجع 'Yes' أو 'No' لصورة واحدة."""
        prompt = "Does this image contain water? Answer ONLY with Yes or No."

        if self.backend == "ollama":
            import ollama
            _, buf = cv2.imencode(".jpg", img_bgr)
            resp = ollama.chat(
                model=self.model,
                messages=[{"role": "user", "content": prompt,
                           "images": [buf.tobytes()]}],
            )
            return resp["message"]["content"].strip()

        # lm_studio
        _, buf = cv2.imencode(".jpg", img_bgr,
                              [int(cv2.IMWRITE_JPEG_QUALITY), self._JPEG_QUALITY])
        b64 = base64.b64encode(buf).decode("utf-8")
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}],
        )
        return resp.choices[0].message.content.strip()

    def _try_check_images(self, image_paths, few_shot_examples=None):
        try:
            results = {}
            for i, path in enumerate(image_paths, 1):
                if not os.path.exists(path):
                    print(f"Warning: {path} not found.")
                    results[f"image_{i}"] = "Error"
                    continue
                img = self._prepare(cv2.imread(path))
                raw = self._check_one(img)
                results[f"image_{i}"] = "Yes" if "yes" in raw.lower() else "No"
            return json.dumps(results)
        except Exception as e:
            return f"Error: {e}"


# ══════════════════════════════════════════════════════════════════════════════
#  ربط الـ interfaces بالـ parent class
# ══════════════════════════════════════════════════════════════════════════════

WaterDetector.Gemini = _Gemini   # type: ignore[attr-defined]
WaterDetector.Groq   = _Groq    # type: ignore[attr-defined]
WaterDetector.Local  = _Local   # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════════════════════════════
#  Utilities
# ══════════════════════════════════════════════════════════════════════════════

def save_failed_prediction(image_path: str, correct_result: str,
                           reason: str = "", folder: str = "failed_cases") -> str:
    """
    تحفظ صورة الموديل غلط فيها كـ few-shot example للمستقبل.
    بترجع المسار الجديد.
    """
    os.makedirs(folder, exist_ok=True)
    new_name = f"ShouldBe_{correct_result}_{os.path.basename(image_path)}"
    new_path = os.path.join(folder, new_name)
    shutil.copy(image_path, new_path)
    print(f"[Feedback Saved] → {new_path}")
    return new_path


def run_feedback_loop(image_paths: list[str], json_result: str,
                      folder: str = "failed_cases") -> list[dict]:
    """
    يسأل المستخدم في الـ terminal عن صحة كل نتيجة ويحفظ الأخطاء.
    بيرجع list صالحة مباشرة كـ few_shot_examples في المرة الجاية.
    """
    try:
        parsed = json.loads(json_result)
    except json.JSONDecodeError:
        print("Couldn't parse JSON.")
        return []

    saved = []
    for i, path in enumerate(image_paths, 1):
        answer = parsed.get(f"image_{i}", "Unknown")
        fb = input(f"\nImage '{path}' → model said [{answer}]. Correct? (y/n): ")
        if fb.strip().lower() == "n":
            correct = "Yes" if answer == "No" else "No"
            reason  = input(f"Why is it {correct}? (brief reason): ")
            new_path = save_failed_prediction(path, correct, reason, folder)
            saved.append({"path": new_path, "correct_result": correct, "reason": reason})
    return saved
