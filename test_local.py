import os
from google import genai
from google.genai import types
import cv2
from dotenv import load_dotenv
from PIL import Image
import ollama
import io
import base64
from openai import OpenAI

load_dotenv()
API = " " #os.getenv("GENAI_API_KEY") 
api_model = "gemini-3.5-flash"
client = genai.Client(api_key=API)

# --- دالة مساعدة لتحسين جودة الصورة ---
def enhance_image_for_ai(img_bgr):
    """
    تقوم بتحسين جودة الصورة لإظهار التفاصيل (خاصة المياه) للموديل.
    المراحل: إزالة الشوائب -> تحسين التباين الذكي (CLAHE) -> شحذ الحواف.
    """
    if img_bgr is None:
        return None
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

def check_multiple_images_for_water(image_paths: list[str]) -> str:
    try:
        contents = []
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


def check_with_qwen(image_path):
    if not os.path.exists(image_path):
        return f"Error: {image_path} not found."
        
    img = cv2.imread(image_path)
    enhanced_image = enhance_image_for_ai(img)
    
    # تحويل مصفوفة OpenCV الحالية إلى بايتات مضغوطة (.jpg) في الذاكرة مباشرة لـ Ollama
    _, buffer = cv2.imencode('.jpg', enhanced_image)
    image_bytes = buffer.tobytes()

    response = ollama.chat(
        model='qwen2-vl',
        messages=[{
            'role': 'user',
            'content': 'Does this image contain water? Answer ONLY with "Yes" or "No".',
            'images': [image_bytes]
        }]
    )
    return response['message']['content'].strip()


# الاتصال بالسيرفر الخاص بـ LM Studio (تأكد من إضافة /v1 في نهاية الرابط)
client_local = OpenAI(base_url="http://127.0.0.1:1234/v1", api_key="lm-studio")

def check_with_local_server(image_path):
    if not os.path.exists(image_path):
        return f"Error: {image_path} not found."

    img = cv2.imread(image_path)
    enhanced_image = enhance_image_for_ai(img)
    
    # --- التعديل هنا: تصغير حجم الصورة لتجنب انهيار الموديل (Channel Error) ---
    max_size = 768 # يمكنك تقليل الرقم لـ 512 لو استمرت المشكلة
    height, width = enhanced_image.shape[:2]
    
    if max(height, width) > max_size:
        scale = max_size / max(height, width)
        new_width = int(width * scale)
        new_height = int(height * scale)
        enhanced_image = cv2.resize(enhanced_image, (new_width, new_height), interpolation=cv2.INTER_AREA)

    # ضغط الصورة وتقليل الجودة لـ 85% لتخفيف حجم الـ Payload
    _, buffer = cv2.imencode('.jpg', enhanced_image, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    encoded_image = base64.b64encode(buffer).decode('utf-8')

    try:
        response = client_local.chat.completions.create(
            model="qwen2-vl", # تأكد إن هذا نفس الاسم المكتوب في LM Studio
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Does this image contain water? Answer ONLY with Yes or No."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded_image}"}}
                    ]
                }
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Server Error: {e}"

# --- تشغيل الكود وتجربته ---
if __name__ == "__main__":
    target_image = "img2.jpg"  # تأكد أن الصورة في نفس الفولدر أو اكتب المسار الكامل
    
    if os.path.exists(target_image):
        print(f"Sending '{target_image}' to Local LM Studio Server...")
        json_result_h = check_with_local_server(target_image)
        print("\n--- Final Result ---")
        print(json_result_h)
    else:
        print(f"Error: The file '{target_image}' does not exist in the current directory.")