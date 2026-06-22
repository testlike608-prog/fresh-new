import os
import json
import shutil
import base64
import cv2
from openai import OpenAI

# --- الاتصال بسيرفر LM Studio المحلي (OpenAI-compatible) ---
# تأكد إن الموديل (مثلاً qwen2-vl) شغال ومحمّل في LM Studio قبل التشغيل
LMSTUDIO_BASE_URL = "http://127.0.0.1:1234/v1"
LMSTUDIO_MODEL_NAME = "qwen2-vl"  # لازم يطابق اسم الموديل المحمّل في LM Studio بالظبط

client_local = OpenAI(base_url=LMSTUDIO_BASE_URL, api_key="lm-studio")


# --- 1. دالة تحسين الصورة (نفس الفكرة بالظبط) ---
def enhance_image_for_ai(img_bgr):
    if img_bgr is None:
        return None
    denoised = cv2.fastNlMeansDenoisingColored(img_bgr, None, h=3, hColor=3, templateWindowSize=7, searchWindowSize=21)
    lab = cv2.cvtColor(denoised, cv2.COLOR_BGR2Lab)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    cl = clahe.apply(l)
    limg = cv2.merge((cl, a, b))
    enhanced_contrast = cv2.cvtColor(limg, cv2.COLOR_Lab2BGR)
    gaussian_blur = cv2.GaussianBlur(enhanced_contrast, (0, 0), 3)
    sharpened = cv2.addWeighted(enhanced_contrast, 1.5, gaussian_blur, -0.5, 0)
    return sharpened


# --- 2. تحويل الصورة لـ base64 بعد تصغيرها (تجنب انهيار السيرفر المحلي) ---
def encode_image_b64(image_path, max_size=768, jpeg_quality=85):
    img = cv2.imread(image_path)
    img = enhance_image_for_ai(img)
    if img is None:
        return None

    height, width = img.shape[:2]
    if max(height, width) > max_size:
        scale = max_size / max(height, width)
        img = cv2.resize(img, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)

    _, buffer = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
    return base64.b64encode(buffer).decode("utf-8")


def image_content_block(b64_str):
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_str}"}}


# --- 3. بناء الـ JSON schema بنفس فكرة الكود الأول (image_1..image_n -> Yes/No) ---
def build_json_schema(n_images):
    properties = {
        f"image_{i}": {"type": "string", "enum": ["Yes", "No"], "description": f"Result for image number {i}"}
        for i in range(1, n_images + 1)
    }
    return {
        "name": "water_detection_result",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": properties,
            "required": [f"image_{i}" for i in range(1, n_images + 1)],
            "additionalProperties": False,
        },
    }


SYSTEM_INSTRUCTION = (
    "You are a precise quality control assistant. Analyze the images and output JSON strictly "
    "mapping each image number to 'Yes' or 'No'. Output JSON only, no extra text."
)


def _build_user_content(image_paths, few_shot_examples=None):
    """يبني content[] الواحد اللي فيه أمثلة few-shot + الصور الجديدة المرقّمة"""
    content = []

    if few_shot_examples:
        content.append({"type": "text", "text": "Before analyzing the new images, learn from these tricky examples:"})
        for idx, ex in enumerate(few_shot_examples, start=1):
            if os.path.exists(ex["path"]):
                b64 = encode_image_b64(ex["path"])
                if b64:
                    content.append({"type": "text", "text": f"Training Example {idx}:"})
                    content.append(image_content_block(b64))
                    content.append({
                        "type": "text",
                        "text": f"Correct Result: {ex['correct_result']}. Reason: {ex.get('reason', '')}",
                    })
        content.append({"type": "text", "text": "Now apply this logic to the following numbered new images:"})

    for i, path in enumerate(image_paths, start=1):
        if os.path.exists(path):
            b64 = encode_image_b64(path)
            if b64:
                content.append({"type": "text", "text": f"This is Image {i}:"})
                content.append(image_content_block(b64))
        else:
            print(f"Warning: Image {path} not found.")

    content.append({
        "type": "text",
        "text": "Look at each numbered image and determine if water is present. Respond for each image according to the schema.",
    })
    return content


def _try_parse_json(raw_text, n_images):
    """يحاول يستخرج JSON صالح من رد الموديل حتى لو فيه نص زيادة أو ```json fences"""
    cleaned = raw_text.strip()
    cleaned = cleaned.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(cleaned)
        if all(f"image_{i}" in parsed for i in range(1, n_images + 1)):
            return parsed
    except json.JSONDecodeError:
        pass
    return None


# --- 4. الدالة الأساسية: باتش (كل الصور في طلب واحد) ---
def _check_batch(image_paths, few_shot_examples=None):
    n = len(image_paths)
    content = _build_user_content(image_paths, few_shot_examples)
    messages = [
        {"role": "system", "content": SYSTEM_INSTRUCTION},
        {"role": "user", "content": content},
    ]
    schema = build_json_schema(n)

    # المحاولة الأولى: structured output صارم (لو الـ backend بيدعمه)
    try:
        response = client_local.chat.completions.create(
            model=LMSTUDIO_MODEL_NAME,
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_schema", "json_schema": schema},
        )
        raw = response.choices[0].message.content
        parsed = _try_parse_json(raw, n)
        if parsed:
            return parsed
    except Exception as e:
        print(f"[Info] structured output failed ({e}), falling back to plain JSON prompt...")

    # Fallback: نطلب JSON عادي في الـ prompt من غير response_format
    messages[-1]["content"] = messages[-1]["content"] + [{
        "type": "text",
        "text": f"Respond with ONLY a valid JSON object with keys image_1..image_{n}, each value 'Yes' or 'No'. No explanation.",
    }]
    try:
        response = client_local.chat.completions.create(
            model=LMSTUDIO_MODEL_NAME,
            messages=messages,
            temperature=0.0,
        )
        raw = response.choices[0].message.content
        parsed = _try_parse_json(raw, n)
        if parsed:
            return parsed
        return {"error": f"Could not parse JSON. Raw response: {raw}"}
    except Exception as e:
        return {"error": str(e)}


# --- 5. وضع بديل أضمن: صورة واحدة لكل طلب (أفضل لو الموديل بيلخبط مع باتش الصور) ---
def _check_loop(image_paths, few_shot_examples=None):
    result = {}
    for i, path in enumerate(image_paths, start=1):
        single_result = _check_batch([path], few_shot_examples=few_shot_examples)
        # single_result هيكون فيه key اسمها image_1 دايمًا لأنها صورة واحدة بس
        result[f"image_{i}"] = single_result.get("image_1", single_result.get("error", "Unknown"))
    return result


def check_multiple_images_for_water_local(image_paths, few_shot_examples=None, mode="batch"):
    """
    mode="batch": كل الصور في طلب واحد (أسرع، بس بعض الموديلات المحلية بتلخبط مع كذا صورة)
    mode="loop" : كل صورة في طلب لوحدها، النتايج بترجع بنفس شكل JSON الموحّد (أضمن لكنه أبطأ)
    """
    if mode == "loop":
        return json.dumps(_check_loop(image_paths, few_shot_examples), ensure_ascii=False)
    return json.dumps(_check_batch(image_paths, few_shot_examples), ensure_ascii=False)


# --- 6. حفظ الأخطاء أوتوماتيكيًا للتدريب المستقبلي (نفس الفكرة بالظبط) ---
def save_failed_prediction(image_path, correct_result, reason=""):
    folder_name = "failed_cases"
    os.makedirs(folder_name, exist_ok=True)
    base_name = os.path.basename(image_path)
    new_name = f"ShouldBe_{correct_result}_{base_name}"
    new_path = os.path.join(folder_name, new_name)
    shutil.copy(image_path, new_path)
    print(f"[Feedback Saved] Copied {base_name} to {new_path} as a future example.")


# --- تشغيل تجريبي ---
if __name__ == "__main__":
    my_training_examples = [
        {"path": "failed_cases/img1.jpg", "correct_result": "No", "reason": "Light reflection on metal, not water."},
        {"path": "failed_cases/img2.jpg", "correct_result": "No", "reason": "Light reflection on metal, not water."},
    ]

    #images_to_check = [f"img{i}.jpg" for i in range(1, 13)]
    images_to_check = ["img1.jpg"]

    print("Sending images to local LM Studio model...")
    json_result = check_multiple_images_for_water_local(
        images_to_check, few_shot_examples=my_training_examples, mode="batch"
    )

    print("\n--- Final Result (JSON) ---")
    print(json_result)

    try:
        parsed_result = json.loads(json_result)
        for i, path in enumerate(images_to_check, start=1):
            model_answer = parsed_result.get(f"image_{i}")
            user_feedback = input(f"\nModel says '{path}' has water? [{model_answer}]. Correct? (y/n): ")
            if user_feedback.lower() == "n":
                correct_ans = "Yes" if model_answer == "No" else "No"
                reason = input(f"Why is it {correct_ans}? ")
                save_failed_prediction(path, correct_result=correct_ans, reason=reason)
                print("Saved as a future training example.")
    except json.JSONDecodeError:
        print("Couldn't parse JSON to run feedback loop.")
