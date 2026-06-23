import os
from google import genai
from google.genai import types
import cv2
from dotenv import load_dotenv
from PIL import Image

load_dotenv()
API = os.getenv("GENAI_API_KEY") # تأكد إنك حطيت الـ API Key في ملف .env بالاسم ده
api_model = os.getenv("MODEL") #"gemini-3.5-flash"#os.getenv("GENAI_MODEL") # أو أي موديل تاني متاح عندك
# إعداد العميل
client = genai.Client(api_key=API)

# --- دالة مساعدة لتحسين جودة الصورة ---
def enhance_image_for_ai(img_bgr):
    """
    تقوم بتحسين جودة الصورة لإظهار التفاصيل (خاصة المياه) للموديل.
    المراحل: إزالة الشوائب -> تحسين التباين الذكي (CLAHE) -> شحذ الحواف.
    """
    # 1. إزالة الشوائب (Denoising) - بدرجة خفيفة للحفاظ على التفاصيل
    # h=3 هو معامل القوة، لوشوفت الصورة ناعمة زيادة قلله لـ 2
    denoised = cv2.fastNlMeansDenoisingColored(img_bgr, None, h=3, hColor=3, templateWindowSize=7, searchWindowSize=21)

    # 2. تحسين التباين الذكي (CLAHE) في فضاء ألوان Lab (الأفضل للتباين)
    lab = cv2.cvtColor(denoised, cv2.COLOR_BGR2Lab)
    l, a, b = cv2.split(lab)
    
    # إنشاء كائن CLAHE (clipLimit بيتحكم في قوة التباين، 2.0 رقم متوازن)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
    cl = clahe.apply(l) # بنطبق التحسين على قناة الإضاءة (L) فقط
    
    limg = cv2.merge((cl, a, b))
    enhanced_contrast = cv2.cvtColor(limg, cv2.COLOR_Lab2BGR)

    # 3. شحذ الحواف (Sharpening) باستخدام Unsharp Masking
    # بنعمل بلور خفيف وبعدين بنطرحه من الأصل لإظهار الحواف
    gaussian_blur = cv2.GaussianBlur(enhanced_contrast, (0, 0), 3)
    # المعادلة: original * 1.5 - blurred * 0.5
    sharpened = cv2.addWeighted(enhanced_contrast, 1.5, gaussian_blur, -0.5, 0)

    return sharpened

def check_multiple_images_for_water(image_paths: list[str],
                                    max_retries: int = 4,
                                    retry_delay: float = 5.0) -> str:
    """
    بتبعث قائمة بمسارات الصور، والـ API هيرد بـ JSON يوضح كل صورة وفيها ماية ولا لأ.
    لو الـ API رجّع 503 أو أي خطأ مؤقت، بيعيد المحاولة تلقائياً.
    """
    import time as _time

    for attempt in range(1, max_retries + 1):
        result = _try_check_images(image_paths)
        if not result.startswith("Error:"):
            return result
        # لو الخطأ مؤقت (503 / UNAVAILABLE / rate limit) → retry
        is_retryable = any(k in result for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "quota"))
        if is_retryable and attempt < max_retries:
            wait = retry_delay * attempt   # 5s, 10s, 15s ...
            print(f"[AI] محاولة {attempt}/{max_retries} فشلت — انتظار {wait:.0f}s ثم retry...")
            _time.sleep(wait)
            continue
        break   # خطأ دائم أو استنفدنا المحاولات

    print(f"[AI] فشل نهائي بعد {max_retries} محاولة: {result}")
    return result   # الكود الأعلى يتعامل معاه كـ error


def _try_check_images(image_paths: list[str]) -> str:
    """محاولة واحدة — نفس المنطق الأصلي."""
    try:
        # 1. تجهيز الـ Contents: هنمرر الصور والأسئلة بالترتيب
        contents = []
        
        for i, path in enumerate(image_paths, start=1):
            if os.path.exists(path):
                # قراءة الصورة بـ OpenCV
                img = cv2.imread(path)
                #img = enhance_image_for_ai(img)
                rgb_img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                
                # التحويل إلى PIL Image عشان Gemini API يقدر يفهمها
                pil_image = Image.fromarray(rgb_img)
                
                # إضافة الصورة والتعريف للموديل
                contents.append(f"This is Image {i}:")
                contents.append(pil_image)
            else:
                print(f"Warning: Image {path} not found.")

        if not contents:
            return "No valid images provided."

        # بنضيف السؤال العام في الآخر
        contents.append("Look at each numbered image provided and determine if water is present. Respond for each image according to the schema.")

        # 2. تحديد الـ Schema على هيئة Object / Dictionary
        # الرد هيكون حاجة شبه كده: {"image_1": "Yes", "image_2": "No"}
        # بنعرف المفاتيح ديناميكياً بناءً على عدد الصور
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

        # 3. إرسال الطلب
        response = client.models.generate_content(
            model=api_model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction="You are a precise quality control assistant. Analyze the images and output JSON strictly mapping each image number to 'Yes' or 'No'.",
                # هنا غيرنا الـ MIME Type لـ JSON عشان يرجع منظّم
                response_mime_type="application/json", 
                response_schema=response_schema,
                temperature=0.0
            )
        )
        
        return response.text.strip()

    except Exception as e:
        return f"Error: {e}"

# --- مثال لتشغيل الكود بمجموعة صور ---c
if __name__ == "__main__":
    # قائمة الصور اللي عايز تفحصها مع بعض (تقدير تحط 2، 3، 4 أو أكتر في نفس الطلب)
    #images_to_check = ["img17.jpg", "img18.jpg", "img19.jpg", "img20.jpg", "img21.jpg"]  # تأكد إن المسارات دي صحيحة
    images_to_check = ["results\\2511TL005663ISI_0.jpg", "results/2511TL005663ISI_1.jpg", "results\\2511TL005663ISI_2.jpg"]  # تأكد إن المسارات دي صحيحة
    
    
    # للتجربة: تأكد إن الملفات دي موجودة فعلياً في الفولدر
    print("Sending images to Gemini...")
    json_result = check_multiple_images_for_water(images_to_check)
    
    print("\n--- Final Result (JSON) ---")
    print(json_result)