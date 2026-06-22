import os
import shutil
import json
import cv2
import numpy as np
from PIL import Image
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()
API = " " #os.getenv("GENAI_API_KEY")  # تأكد إنك حطيت الـ API Key في ملف .env بالاسم ده 
client = genai.Client(api_key=API)

# --- 1. دالة تحسين الصورة (كما هي) ---
def enhance_image_for_ai(img_bgr):
    denoised = cv2.fastNlMeansDenoisingColored(img_bgr, None, h=3, hColor=3, templateWindowSize=7, searchWindowSize=21)
    lab = cv2.cvtColor(denoised, cv2.COLOR_BGR2Lab)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
    cl = clahe.apply(l)
    limg = cv2.merge((cl, a, b))
    enhanced_contrast = cv2.cvtColor(limg, cv2.COLOR_Lab2BGR)
    gaussian_blur = cv2.GaussianBlur(enhanced_contrast, (0, 0), 3)
    sharpened = cv2.addWeighted(enhanced_contrast, 1.5, gaussian_blur, -0.5, 0)
    return sharpened

# --- 2. دالة الفحص المعدلة لتقبل الأمثلة السابقة (Few-Shot) ---
def check_multiple_images_for_water(image_paths: list[str], few_shot_examples: list[dict] = None) -> str:
    try:
        contents = []
        
        # إضافة الأمثلة التعليمية لو موجودة (الفكرة السحرية لرفع الدقة)
        if few_shot_examples:
            contents.append("Before analyzing the new images, please learn from these tricky examples:")
            for idx, ex in enumerate(few_shot_examples, start=1):
                if os.path.exists(ex['path']):
                    img_ex = cv2.imread(ex['path'])
                    img_ex = enhance_image_for_ai(img_ex) # تحسين المثال برضه
                    rgb_ex = cv2.cvtColor(img_ex, cv2.COLOR_BGR2RGB)
                    pil_ex = Image.fromarray(rgb_ex)
                    
                    contents.append(f"Training Example {idx}:")
                    contents.append(pil_ex)
                    contents.append(f"Correct Result: {ex['correct_result']}. Reason: {ex['reason']}")
            contents.append("Now, apply this logic to the following numbered new images:")

        # قراءة الصور الجديدة المطلوبة
        for i, path in enumerate(image_paths, start=1):
            if os.path.exists(path):
                img = cv2.imread(path)
                img = enhance_image_for_ai(img)
                rgb_img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                pil_image = Image.fromarray(rgb_img)
                
                contents.append(f"This is Image {i}:")
                contents.append(pil_image)
            else:
                print(f"Warning: Image {path} not found.")

        if not contents:
            return "No valid images provided."

        contents.append("Look at each numbered image provided and determine if water is present. Respond for each image according to the schema.")

        properties_dict = {}
        for i in range(1, len(image_paths) + 1):
            properties_dict[f"image_{i}"] = types.Schema(
                type=types.Type.STRING,
                enum=["Yes", "No"],
                description=f"Result for image number {i}"
            )

        response_schema = types.Schema(
            type=types.Type.OBJECT,
            properties=properties_dict,
            required=[f"image_{i}" for i in range(1, len(image_paths) + 1)]
        )

        # ملحوظة: غيرت الموديل لـ gemini-1.5-flash لأن 3.5 لسه مش موجود
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction="You are a precise quality control assistant. Analyze the images and output JSON strictly mapping each image number to 'Yes' or 'No'.",
                response_mime_type="application/json", 
                response_schema=response_schema,
                temperature=0.0
            )
        )
        
        return response.text.strip()

    except Exception as e:
        return f"Error: {e}"

# --- 3. دالة حفظ الأخطاء أوتوماتيكياً للتدريب المستقبلي ---
def save_failed_prediction(image_path, correct_result, reason=""):
    """
    بتاخد الصورة اللي الموديل غلط فيها وتحفظها في فولدر عشان نستخدمها بعدين كـ Training Example
    """
    folder_name = "failed_cases"
    os.makedirs(folder_name, exist_ok=True)
    
    base_name = os.path.basename(image_path)
    # بنغير اسم الصورة عشان يكون واضح إيه الإجابة الصح بتاعتها
    new_name = f"ShouldBe_{correct_result}_{base_name}"
    new_path = os.path.join(folder_name, new_name)
    
    shutil.copy(image_path, new_path)
    print(f"[Feedback Saved] Copied {base_name} to {new_path} as a future example.")

# --- مثال لتشغيل الكود بنظام الـ Feedback ---
if __name__ == "__main__":
    # 1. لو عندك صور الموديل كان بيغلط فيها قبل كده، بنجهزها هنا (Few-shot)
    # لاحظ: بنكتب مسار الصورة واسمها، الإجابة الصح إيه، والسبب عشان الموديل يتعلم
    my_training_examples = [
         {
             "path": "failed_cases/img1.jpg", 
             "correct_result": "No", 
             "reason": "This is just a light reflection from the metal surface, not a water drop."
         },
         {
             "path": "failed_cases/img2.jpg", 
             "correct_result": "No", 
             "reason": "This is just a light reflection from the metal surface, not a water drop."
         }
    ]

    # 2. الصور الجديدة اللي عايزين نفحصها دلوقتي
    images_to_check = ["img1.jpg", "img2.jpg", "img3.jpg", "img4.jpg", "img5.jpg", "img6.jpg", "img7.jpg", "img8.jpg", "img9.jpg", "img10.jpg", "img11.jpg", "img12.jpg"] # حط مسار صورة حقيقية عندك للتجربة
    
    print("Sending images to Gemini...")
    # بنبعث الصور الجديدة ومعاها الأمثلة السابقة عشان يتعلم منها
    json_result = check_multiple_images_for_water(images_to_check, few_shot_examples=my_training_examples)
    
    print("\n--- Final Result (JSON) ---")
    print(json_result)
    
    # 3. محاكاة لنظام الـ Feedback (في بيئة العمل ممكن تتربط بـ GUI أو زرار في واجهة العامل)
    try:
        parsed_result = json.loads(json_result)
        # هنسأل المستخدم (العامل/المهندس) لو النتيجة صح ولا غلط لكل صورة
        for i, path in enumerate(images_to_check, start=1):
            model_answer = parsed_result.get(f"image_{i}")
            
            user_feedback = input(f"\nModel says Image '{path}' has water? [{model_answer}]. Is this CORRECT? (y/n): ")
            
            if user_feedback.lower() == 'n':
                correct_ans = "Yes" if model_answer == "No" else "No"
                reason = input(f"Why is it {correct_ans}? (Type a brief reason for the AI to learn): ")
                # لو غلط، احفظ الصورة فوراً في فولدر الأخطاء
                save_failed_prediction(path, correct_result=correct_ans, reason=reason)
                print("Great, this will be used to make the model smarter next time!")
    except json.JSONDecodeError:
        print("Couldn't parse JSON to run feedback loop.")