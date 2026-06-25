import os
import cv2
import base64
import time as _time
from dotenv import load_dotenv
from groq import Groq

# 1. تحميل متغيرات البيئة
load_dotenv()
API = os.getenv("GROQ_API_KEY") # تأكد إنك حطيت المفتاح باسم GROQ_API_KEY في ملف .env
# استخدم موديل رؤية متاح على Groq، مثل:
api_model = os.getenv("MODEL") 

# 2. إعداد عميل Groq
client = Groq(api_key=API)

# --- دالة مساعدة لتحسين جودة الصورة (كما هي بدون تغيير) ---
def enhance_image_for_ai(img_bgr):
    """
    تقوم بتحسين جودة الصورة لإظهار التفاصيل (خاصة المياه) للموديل.
    المراحل: إزالة الشوائب -> تحسين التباين الذكي (CLAHE) -> شحذ الحواف.
    """
    # 1. إزالة الشوائب (Denoising)
    denoised = cv2.fastNlMeansDenoisingColored(img_bgr, None, h=3, hColor=3, templateWindowSize=7, searchWindowSize=21)

    # 2. تحسين التباين الذكي (CLAHE)
    lab = cv2.cvtColor(denoised, cv2.COLOR_BGR2Lab)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
    cl = clahe.apply(l) 
    limg = cv2.merge((cl, a, b))
    enhanced_contrast = cv2.cvtColor(limg, cv2.COLOR_Lab2BGR)

    # 3. شحذ الحواف (Sharpening)
    gaussian_blur = cv2.GaussianBlur(enhanced_contrast, (0, 0), 3)
    sharpened = cv2.addWeighted(enhanced_contrast, 1.5, gaussian_blur, -0.5, 0)

    return sharpened

# --- دالة مساعدة لتحويل صورة OpenCV إلى Base64 ---
def encode_cv2_image_to_base64(cv2_img):
    """
    تحويل مصفوفة الصورة (OpenCV) إلى سلسلة نصية Base64 لسهولة إرسالها للـ API
    """
    # تحويل الصورة إلى امتداد jpg في الذاكرة
    success, buffer = cv2.imencode('.jpg', cv2_img)
    if not success:
        raise ValueError("Failed to encode image to JPEG format.")
    # تشفيرها إلى Base64
    return base64.b64encode(buffer).decode('utf-8')

# --- دالة المحاولات المتعددة (نفس المنطق) ---
def check_multiple_images_for_water(image_paths: list[str],
                                    max_retries: int = 4,
                                    retry_delay: float = 5.0) -> str:
    """
    بتبعث قائمة بمسارات الصور، والـ API هيرد بـ JSON يوضح كل صورة وفيها ماية ولا لأ.
    """
    for attempt in range(1, max_retries + 1):
        result = _try_check_images(image_paths)
        if not result.startswith("Error:"):
            return result
        
        # كلمات مفتاحية للأخطاء اللي ممكن يتعملها Retry في Groq
        is_retryable = any(k in result.lower() for k in ("503", "unavailable", "429", "rate limit", "timeout"))
        if is_retryable and attempt < max_retries:
            wait = retry_delay * attempt
            print(f"[AI] محاولة {attempt}/{max_retries} فشلت — انتظار {wait:.0f}s ثم retry...")
            _time.sleep(wait)
            continue
        break 

    print(f"[AI] فشل نهائي بعد {max_retries} محاولة: {result}")
    return result


def _try_check_images(image_paths: list[str]) -> str:
    """محاولة واحدة للاتصال بـ Groq API وإرسال الصور."""
    try:
        # 1. إعداد هيكل المحتوى (Content) الخاص بـ Groq (OpenAI format)
        user_content = []
        expected_keys = []
        
        for i, path in enumerate(image_paths, start=1):
            if os.path.exists(path):
                # قراءة الصورة بـ OpenCV
                img = cv2.imread(path)
                
                # يمكنك تفعيل التحسين هنا إذا أردت
                # img = enhance_image_for_ai(img)
                
                # تحويل الصورة لـ Base64
                base64_image = encode_cv2_image_to_base64(img)
                
                # إضافة النص التعريفي للصورة
                user_content.append({"type": "text", "text": f"This is Image {i}:"})
                
                # إضافة الصورة مشفرة
                user_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}"
                    }
                })
                
                expected_keys.append(f"image_{i}")
            else:
                print(f"Warning: Image {path} not found.")

        if not user_content:
            return "No valid images provided."

        # 2. تحديد التعليمات وصيغة الـ JSON المطلوبة
        # نحدد للموديل الـ Keys اللي لازم يرجعها بالظبط عشان نعوض غياب الـ response_schema الدقيق
        json_structure = "{" + ", ".join([f'"{key}": "Yes" or "No"' for key in expected_keys]) + "}"
        prompt_text = (
            f"Look at each numbered image provided and determine if water is present. "
            f"You MUST output a valid JSON object using exactly this structure: {json_structure}."
        )
        user_content.append({"type": "text", "text": prompt_text})

        messages = [
            {
                "role": "system",
                "content": "You are a precise quality control assistant. Analyze the images and output strictly in JSON format. Do not include any extra text or markdown formatting."
            },
            {
                "role": "user",
                "content": user_content
            }
        ]

        # 3. إرسال الطلب لـ Groq
        response = client.chat.completions.create(
            model=api_model,
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_object"} # إجبار الموديل على إرجاع JSON
        )
        
        return response.choices[0].message.content.strip()

    except Exception as e:
        return f"Error: {str(e)}"

# --- تشغيل الكود ---
if __name__ == "__main__":
    # تأكد إن المسارات دي صحيحة وموجودة عندك
    images_to_check = [
        "results\\2511TL005663ISI_0.jpg", 
        "results\\2511TL005663ISI_1.jpg", 
        "results\\2511TL005663ISI_2.jpg"
    ] 
    
    print("Sending images to Groq API...")
    json_result = check_multiple_images_for_water(images_to_check)
    
    print("\n--- Final Result (JSON) ---")
    print(json_result)