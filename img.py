import os
import time
import cv2
import camera_hub_useeplus as camera_hub
import logging

logging.basicConfig(level=logging.INFO)

def capture_image_and_save( image_name, image_path, frame=None):
        """
        Captures an image using the CameraHub model and saves it to the disk.
        
        :param image_name: String, name of the file (e.g., 'frame_01.jpg')
        :param image_path: String, directory path to save the image (e.g., '/var/data/images')
        :return: Boolean, True if saved successfully, False otherwise
        """
        # 1. Validate inputs
        if not image_name or not image_path:
            print("[Error] Invalid image name or path provided.")
            return False

        try:
            # 2. Ensure the destination directory exists safely
            os.makedirs(image_path, exist_ok=True)
            
            # 3. Create an absolute or normalized cross-platform file path
            full_output_path = os.path.normpath(os.path.join(image_path, image_name))

            # 4. Request the frame data from your CameraHub model
            # (Adjust 'capture_frame()' to match your model's actual method name)
            frame = camera_hub.get_frame()  # Example method to get the current frame from CameraHub

            if frame is None:
                print("[Error] CameraHub returned an empty frame.")
                return False

            # 5. Save the frame to disk
            # This example assumes CameraHub returns a standard NumPy array (OpenCV format)
            success = cv2.imwrite(full_output_path, frame)
            
            # Note: If CameraHub returns a PIL Image instead, use:
            # frame.save(full_output_path)
            # success = True

            if success:
                print(f"[Success] Image successfully saved to: {full_output_path}")
                return True
            else:
                print(f"[Error] Failed to write image file to disk at: {full_output_path}")
                return False

        except IOError as io_err:
            print(f"[Error] Disk I/O issue while saving image: {io_err}")
            return False
        except Exception as e:
            print(f"[Error] An unexpected error occurred: {e}")
            return False
        
def capture_image_and_save2(file_name: str, save_dir: str = "./result") -> bool:
    """
    بتاخد فريم من camera_hub وتحفظه في المسار المحدد.
    
    :param file_name: اسم الصورة (مثال: image.jpg)
    :param save_dir: مسار المجلد اللي هتتحفظ فيه (الافتراضي ./result)
    :return: True لو تم الحفظ بنجاح، False لو فشل أو مفيش فريم
    """
    # 1. التأكد إن المجلد (Directory) موجود، ولو مش موجود ننشئه
    os.makedirs(save_dir, exist_ok=True)
    
    # 2. الحصول على الفريم من الكاميرا
    frame = camera_hub.get_frame()
    
    # 3. التأكد إن الفريم موجود
    if frame is not None:
        # تجهيز المسار الكامل للصورة
        full_path = os.path.join(save_dir, file_name)
        
        # حفظ الصورة باستخدام OpenCV
        success = cv2.imwrite(full_path, frame)
        
        if success:
            print(f"✅ تم حفظ الصورة بنجاح: {full_path}")
            return True
        else:
            print(f"❌ فشل في حفظ الصورة في المسار: {full_path}")
            return False
    else:
        print("⚠️ مفيش فريم متاح حالياً! تأكد إن الكاميرا شغالة وعملت wait_for_frame.")
        return False

if __name__ == "__main__":
    import sys
    import time
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    
    cam = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    camera_hub.start(camera_index=cam)
    print("اضغط Ctrl+C للإيقاف...")
    
    #try:
        # نستنى أول فريم عشان نتأكد إن الكاميرا لقطت
    if camera_hub.wait_for_frame(timeout=5.0):
            while True:
                # نعمل اسم مميز لكل صورة باستخدام الوقت
                #img_name = f"frame_{int(time.time())}.jpg"
                img_name = "test.jpg"
                # نستدعي الفانكشن بتاعتنا
                capture_image_and_save2(file_name=img_name, save_dir="./result")
                
                # استنى ثانية عشان متصورش 100 صورة في الثانية وتملى الهارد!
                time.sleep(0.05)
    #except KeyboardInterrupt:
    #    print("\nتم طلب الإيقاف...")
    #    camera_hub.stop()
    #finally:
        #camera_hub.stop()
