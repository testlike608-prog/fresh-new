from cmath import log
import json
import socket
import threading
import time
import queue
try:
    import pyodbc
except ModuleNotFoundError:
    pyodbc = None
import os
import re
import textwrap
from datetime import datetime
import csv
import pandas as pd
from openpyxl import load_workbook
import openpyxl
from openpyxl.styles import Font
import scanner as sc
import excel as ex
from thread_logger import LoggedThread, get_logger as _get_thread_logger
import camera_hub_useeplus as camera_hub
import cv2 
from fairino.Robot import RPC
import capture_trigger as ct
from config import Config
import camera_barcode
import api_test as ai
import excel as ex


def _to_bytes(message, is_hex=False):   
    """
    تحويل أي قيمة لـ bytes جاهزه للإرسال على السوكيت.
    بيتعامل مع: bytes, str, int, float (وأي رقم).
    لو is_hex=True بيفسر الـ str كـ hex.

    قبل التعديل: send_only(1) كان بيرمي 'int' object has no attribute 'encode'.
    """
    if isinstance(message, bytes):
        return message
    if isinstance(message, bytearray):
        return bytes(message)
    if is_hex and isinstance(message, str):
        return bytes.fromhex(message)
    # نحوّل أي رقم لـ str قبل encode
    return str(message).encode('utf-8')


class AppStage:
    """
    المراحل اللي ممكن البرنامج يكون فيها.
    يدعم حتى MAX_VISION_TESTS اختبار ديناميكياً بدون تعديل الكود.
    """
    # ─── الحد الأقصى لعدد الاختبارات ────────────────────────────────
    MAX_VISION_TESTS = 30   # ← غيّر الرقم ده لو عايز أكتر أو أقل

    # ─── الـ stages الثابتة ──────────────────────────────────────────
    IDLE             = "IDLE"
    BARCODE_RECEIVED = "BARCODE_RECEIVED"
    PROGRAM_LOOKUP   = "PROGRAM_LOOKUP"
    SENDING_PROGRAM  = "SENDING_PROGRAM"
    REPORTING        = "REPORTING"
    DONE             = "DONE"
    ERROR            = "ERROR"

    # ─── الـ vision stages ديناميكية (VISION_TEST_1 .. VISION_TEST_30) ──
    # بيتولدوا تلقائياً حسب MAX_VISION_TESTS
    @classmethod
    def vision_stage(cls, i: int) -> str:
        """يرجع اسم الـ stage للاختبار i (0-indexed). مثال: i=0 → 'VISION_TEST_1'"""
        return f"VISION_TEST_{i + 1}"

    # ─── عدد الاختبارات الافتراضي ────────────────────────────────────
    VISION_TEST_COUNT = 6   # القيمة الافتراضية لو مش موجودة في config

    @classmethod
    def get_vision_test_count(cls) -> int:
        """يجيب عدد الاختبارات من config (1 .. MAX_VISION_TESTS)."""
        try:
            from config import config as _cfg
            count = int(_cfg.get("vision_test_count", cls.VISION_TEST_COUNT))
        except Exception:
            count = cls.VISION_TEST_COUNT
        return max(1, min(cls.MAX_VISION_TESTS, count))

    # ─── ORDER: الترتيب للـ progress bar (يتولد ديناميكياً) ─────────
    @classmethod
    def get_order(cls) -> list:
        vision_stages = [cls.vision_stage(i) for i in range(cls.MAX_VISION_TESTS)]
        return [
            cls.IDLE, cls.BARCODE_RECEIVED, cls.PROGRAM_LOOKUP, cls.SENDING_PROGRAM,
            *vision_stages,
            cls.REPORTING, cls.DONE,
        ]

    # ORDER ثابت بيستخدمه الكود القديم اللي بيقرأ AppStage.ORDER مباشرة
    ORDER = (
        ["IDLE", "BARCODE_RECEIVED", "PROGRAM_LOOKUP", "SENDING_PROGRAM"]
        + [f"VISION_TEST_{i}" for i in range(1, MAX_VISION_TESTS + 1)]
        + ["REPORTING", "DONE"]
    )

    # ─── LABELS: النصوص للـ GUI ────────────────────────────────────
    LABELS = {
        "IDLE":             "في الانتظار",
        "BARCODE_RECEIVED": "تم استقبال باركود",
        "PROGRAM_LOOKUP":   "البحث عن البرنامج",
        "SENDING_PROGRAM":  "إرسال البرنامج للكوبوت",
        **{f"VISION_TEST_{i}": f"اختبار الرؤية {i}" for i in range(1, MAX_VISION_TESTS + 1)},
        "REPORTING":        "كتابة التقرير",
        "DONE":             "انتهى",
        "ERROR":            "خطأ",
    }

    # backward-compat: attributes for old code using AppStage.VISION_TEST_1 etc.
    # يتولدوا تلقائياً في آخر الكلاس


# نضيف الـ attributes ديناميكياً على الـ class عشان الكود القديم يشتغل
for _i in range(1, AppStage.MAX_VISION_TESTS + 1):
    setattr(AppStage, f"VISION_TEST_{_i}", f"VISION_TEST_{_i}")


class App():
    def __init__(self):

        pass 
   

    def check_images_status(self, images_data):
        # المرور على جميع القيم (Values) داخل الـ JSON
        for image_name, value in images_data.items():
            # التحقق إذا كانت القيمة تساوي 'yes' (مع تجاهل المسافات وحالة الأحرف)
            if str(value).strip().lower() == 'yes':
                return "fail"
                
        # إذا انتهت الحلقة بدون العثور على 'yes'، يتم إرجاع 'pass'
        return "pass"
    
    def capture_image_and_save(self, image_name, image_path):
        """
        بتاخد فريم من camera_hub وتحفظه في المسار المحدد.
        
        :param file_name: اسم الصورة (مثال: image.jpg)
        :param save_dir: مسار المجلد اللي هتتحفظ فيه (الافتراضي ./result)
        :return: True لو تم الحفظ بنجاح، False لو فشل أو مفيش فريم
        """
        # 1. التأكد إن المجلد (Directory) موجود، ولو مش موجود ننشئه
        os.makedirs(image_path, exist_ok=True)
        
        # 2. الحصول على الفريم من الكاميرا
        frame = camera_hub.get_frame()
        
        # 3. التأكد إن الفريم موجود
        if frame is not None:
            # تجهيز المسار الكامل للصورة
            full_path = os.path.join(image_path, image_name)
            
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

    def list_images(self, images_names, images_paths):
        # Implementation for processing images

        pass

    def result_judement(self, images_data):
        # 1. لو الداتا راجعة من الـ API على هيئة نص (JSON String)، هنحولها
        if isinstance(images_data, str):
            try:
                images_data = json.loads(images_data)
            except json.JSONDecodeError:
                return "Error: Invalid JSON format from API"
                
        # 2. حماية إضافية: نتأكد إن الداتا في النهاية بقت Dictionary نقدر نشتغل عليه
        if not isinstance(images_data, dict):
            return "Error: Data is not a dictionary"

        # 3. اللوجيك الأساسي بتاعنا
        for value in images_data.values():
            clean_value = str(value).strip().lower()
            if clean_value == "yes":
                return "fail"
                
        return "pass"
    
    def start(self):
        camera_hub.start()  # تأكد إن الكاميرا بدأت تشتغل في الخلفية
        return True

    def run(self):
        """alias قديم - مازال يشتغل عشان الـ tests و main.py."""
        return self.start()

    def stop(self):
        pass


# Working App implementation used by the FastAPI WebSocket integration.
# It intentionally keeps the same class name so old imports receive this version.
class App():
    def __init__(self):
        self.robot = None
        self._cfg = Config()
        self.robot_ip = self._cfg.get(key="cobot_ip")
        self._robot_lock = threading.RLock()
        self._motion_lock = threading.Lock()
        self._last_images = []
        #self.vision_queue = queue.Queue()

    def check_images_status(self, images_data):
        for image_name, value in images_data.items():
            if str(value).strip().lower() == "yes":
                return "fail"
        return "pass"

    def get_barcode_from_scanner(self):
            """
            ياخد الباركود من scanner.queue_barcode (اللي بيتعمل put فيه من thread الـ keyboard hook)
            ويحطه في vision_queue + report_queue.

            ملحوظة: شيلنا الـ flag و race condition عليه — الكيو نفسه thread-safe.
            """
            log = _get_thread_logger()
            while not self._stop_app.is_set():
                try:
                    # هينتظر لحد ما يجي باركود أو نص ثانية (عشان نقدر نتحقق من _stop_app)
                    barcode = sc.queue_barcode.get(timeout=0.5)
                    sc.queue_barcode.task_done()
                    return barcode
                except queue.Empty:
                    log.info(f"Barcode not received within timeout period")
                    continue

                #try:
                    #self.vision_queue.put(barcode)
                    #log.info(f"Barcode received and put in vision_queue: {barcode}")
                #finally:
                    #sc.queue_barcode.task_done()

    def get_points_from_db(self, point_name :str):
        import sqlite3

        # 1. الاتصال بملف قاعدة البيانات
        conn = sqlite3.connect('web_point.db')
        cursor = conn.cursor()

        # 2. الاستعلام
        query = f"SELECT j1, j2, j3, j4, j5, j6 FROM points WHERE name = '{point_name}'"
        cursor.execute(query)

        # 3. سحب النتيجة
        result = cursor.fetchone()

        # 4. التأكد من وجود النتيجة وتحويلها لأرقام float داخل List
        if result:
            # السطر ده بيلف على كل رقم في النتيجة، يحوله لـ float، ويحطه في الـ List
            joint_angles = [float(x) for x in result]
            
            print("Joint Angles as Floats:", joint_angles)
            
            # للتأكد من نوع أول عنصر مثلاً:
            # print(type(joint_angles[0])) 
        else:
            print("النقطة دي مش موجودة في الداتا بيز.")

        # 5. إغلاق الاتصال
        conn.close()
        return joint_angles if result else "Error: Point not found in database"

    def program_1(self):
        homing =self.get_points_from_db("water1")
        _10kg_1 = self.get_points_from_db("10kg_1")
        _10kg_2 = self.get_points_from_db("10kg_2")
        _10kg_3 = self.get_points_from_db("10kg_3")
        _10kg_4 = self.get_points_from_db("10kg_4")
        _10kg_5 = self.get_points_from_db("10kg_5")


        self.robot.MoveJ(joint_pos= _10kg_1, tool=0, user=0, vel=100, acc=100)
        ct.trigger(name=self.barcode+"_0")
        self.robot.MoveJ(joint_pos= _10kg_2, tool=0, user=0, vel=100, acc=100)
        ct.trigger(name=self.barcode+"_1")
        self.robot.MoveJ(joint_pos= _10kg_3, tool=0, user=0, vel=100, acc=100)
        ct.trigger(name=self.barcode+"_2")
        self.robot.MoveJ(joint_pos= _10kg_4, tool=0, user=0, vel=100, acc=100)
        ct.trigger(name=self.barcode+"_3")
        self.robot.MoveJ(joint_pos= _10kg_5, tool=0, user=0, vel=100, acc=100)
        ct.trigger(name=self.barcode+"_4")
        self.robot.MoveJ(joint_pos= homing, tool=0, user=0, vel=100, acc=100)

        image_list = ["./results/"+self.barcode+"_0", "./results/"+self.barcode+"_1", "./results/"+self.barcode+"_2", "./results/"+self.barcode+"_3", "results/"+self.barcode+"_4"]

        result = self.check_images_status(ai.check_multiple_images_for_water(image_paths=image_list))
        ex.result_reporting(ID =self.barcode, result= result )
        self.robot.SetDO(0,1)
        self.robot.SetDO(3,0)
        if result == "pass":   
            self.robot.SetDO(1,1)
            time.sleep(self._cfg.get(key="signal_pass_preriod"))
            self.robot.SetDO(1,0)
        elif result == "fail":
            self.robot.SetDO(2,1)
            time.sleep(self._cfg.get(key="signal_fail_preriod"))
            self.robot.SetDO(2,0)

    def program_2(self):
        pass

    def program_3(self):
        pass

    def program_4(self):
        pass

    def program_5(self):
        pass

    def start_sequence(self):
        barcode_point = self.get_points_from_db("cam")
        self.robot.MoveJ(barcode_point, 0, 0 , vel=100, acc=100, time=0, radius=0)
        try:
            self.barcode = self.get_barcode_from_scanner
        except:
            pass
        program = self.determine_program_from_barcode(barcode=self.barcode)

        if program ==  "1":
            self.program_1()
        elif program ==  "2":
            self.program_2()
        elif program ==  "3":
            self.program_3()    
        elif program ==  "4":
            self.program_4()
        elif program ==  "5":
            self.program_5()


    def determine_program_from_barcode(self, barcode, excel_file_path=None):
        # نقرأ الـ default من config لو ماتمررش
        if excel_file_path is None:
            from config import config as _cfg
            excel_file_path = _cfg.get("program_mapping_file", "program_mapping.xlsx")

        # LOGIC-1 FIX: تحقق من طول الباركود قبل الوصول لـ [-3]
        if not barcode or len(barcode) < 3:
            return "خطأ: الباركود قصير جداً"

        target_char = barcode[-3]

        try:
            # PERF-1 FIX: نقرأ الملف بس لو اتغير (cache by mtime)
            import os as _os
            try:
                current_mtime = _os.path.getmtime(excel_file_path)
            except OSError:
                current_mtime = 0.0

            if (self._mapping_cache_df is None
                    or self._mapping_cache_path != excel_file_path
                    or self._mapping_cache_mtime != current_mtime):
                self.cobotClient._log_add("INFO", f"Loading mapping file: {excel_file_path}")
                self._mapping_cache_df    = pd.read_excel(excel_file_path)
                self._mapping_cache_path  = excel_file_path
                self._mapping_cache_mtime = current_mtime
            else:
                self.cobotClient._log_add("INFO", "Using cached mapping (file unchanged)")

            df = self._mapping_cache_df
            char_column  = df.columns[0]
            value_column = df.columns[1]

            # 2. البحث المباشر بدون loops
            match = df[df[char_column] == target_char]

            if not match.empty:
                # جلب القيمة المقابلة للحرف
                excel_value = match[value_column].values[0]
                return excel_value
            else:
                return "الحرف غير موجود في ملف الإكسل."

        except FileNotFoundError:
            self.cobotClient._log_add("INFO", f"Excel file not found at path: {excel_file_path}")
            return "خطأ: ملف الإكسل غير موجود في المسار المحدد."
        except Exception as e:
            self.cobotClient._log_add("INFO", f"Unexpected error occurred with pandas: {e}")
            return f"حدث خطأ غير متوقع: {e}"
    
    def start(self, camera_index=None):
        self._stop_app = threading.Event()
        camera_hub.start(camera_index=camera_index)

        camera_hub.wait_for_frame(timeout=5.0)
        ct.start()
        self.ensure_robot()
        self.robot = RPC(self.robot_ip)  
        self.robot.SetRobotMode(1)  # وضع التشغيل العادي
        if self._cfg.get (key="scan_mode") == "camera":
            camera_barcode.start()
        elif self._cfg.get (key="scan_mode") == "manual":
            sc.start_listener()

        self.robot.SetDO(0, 1)  # تفعيل مخرج رقمي
        last = 0
        while self.robot.connect_to_robot():
                DI0= self.robot.GetDI(0, 0)  # قراءة حالة مدخل رقمي للتأكد من الاتصال    
                if DI0==1 and last == 0:  # لو DI0 عالي وكان منخفض قبل كده، يبقى الاتصال اتعمل دلوقتي
                    
                    
                    self.robot.SetDO(0, 0)  # تفعيل مخرج رقمي
                    self.robot.SetDO(3, 1)  # تفعيل مخرج رقمي
                    self.start_sequence()  # بدء تسلسل البرامج
                   

                    print("Robot DI0 is HIGH - Connection seems good.")
                
                else:             
                    print("Robot DI0 is LOW - Check robot connection or configuration.")
                last = DI0  # تحديث الحالة الأخيرة
    def run(self):
        return self.start()

    def stop(self):
        self._stop_app.set()
        camera_hub.stop()
        camera_barcode.stop()
