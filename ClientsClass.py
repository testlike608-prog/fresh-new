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
import excel as ex                             # BUG-001: إزالة import مكرر
from thread_logger import LoggedThread, get_logger as _get_thread_logger
from camera_hub import CameraHub
import cv2
from fairino.Robot import RPC
import capture_trigger as ct
from config import config as _cfg_singleton    # BUG-008: استخدام singleton
import camera_barcode
from ai_vision import WaterDetector

# ── مسار ملف الإحصائيات الدائمة (جوه DATA_DIR عشان يتحفظ في الـ Volume) ─────
from config import data_path as _data_path
_SESSION_STATS_FILE = _data_path("session_stats.json")

def _to_bytes(message, is_hex=False):
    """
    تحويل أي قيمة لـ bytes جاهزه للإرسال على السوكيت.
    بيتعامل مع: bytes, str, int, float (وأي رقم).
    لو is_hex=True بيفسر الـ str كـ hex.
    """
    if isinstance(message, bytes):
        return message
    if isinstance(message, bytearray):
        return bytes(message)
    if is_hex and isinstance(message, str):
        return bytes.fromhex(message)
    return str(message).encode('utf-8')


class AppStage:
    """
    المراحل اللي ممكن البرنامج يكون فيها.
    يدعم حتى MAX_VISION_TESTS اختبار ديناميكياً بدون تعديل الكود.
    """
    MAX_VISION_TESTS = 30

    IDLE             = "IDLE"
    BARCODE_RECEIVED = "BARCODE_RECEIVED"
    PROGRAM_LOOKUP   = "PROGRAM_LOOKUP"
    SENDING_PROGRAM  = "SENDING_PROGRAM"
    REPORTING        = "REPORTING"
    DONE             = "DONE"
    ERROR            = "ERROR"

    @classmethod
    def vision_stage(cls, i: int) -> str:
        """يرجع اسم الـ stage للاختبار i (0-indexed). مثال: i=0 → 'VISION_TEST_1'"""
        return f"VISION_TEST_{i + 1}"

    VISION_TEST_COUNT = 6

    @classmethod
    def get_vision_test_count(cls) -> int:
        try:
            count = int(_cfg_singleton.get("vision_test_count", 6))
        except Exception:
            count = 6
        return max(1, min(cls.MAX_VISION_TESTS, count))

    @classmethod
    def get_order(cls) -> list:
        """BUG-052: إزالة ORDER المكرر — استخدم get_order() فقط."""
        vision_stages = [cls.vision_stage(i) for i in range(cls.MAX_VISION_TESTS)]
        return [
            cls.IDLE, cls.BARCODE_RECEIVED, cls.PROGRAM_LOOKUP, cls.SENDING_PROGRAM,
            *vision_stages,
            cls.REPORTING, cls.DONE,
        ]
    # BUG-026: إزالة VISION_TEST_COUNT الميت
    # BUG-052: إزالة ORDER المكرر (كان index مختلف عن get_order)

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


# نضيف الـ attributes ديناميكياً على الـ class عشان الكود القديم يشتغل
for _i in range(1, AppStage.MAX_VISION_TESTS + 1):
    setattr(AppStage, f"VISION_TEST_{_i}", f"VISION_TEST_{_i}")


class App():
    def __init__(self):
        self.robot = None
        self._cfg = _cfg_singleton              # BUG-008: singleton بدل new Config()
        self.robot_ip = self._cfg.get(key="cobot_ip")
        self._robot_lock = threading.RLock()
        self._motion_lock = threading.Lock()
        self._last_images = []
        self.barcode = None
        self._mapping_cache_df    = None
        self._mapping_cache_path  = None
        self._mapping_cache_mtime = None
        self._camera      = self._build_camera()
        self._ai_provider = self._build_ai_provider()

        # ── Web state tracking ─────────────────────────────────────────
        self._running       = False
        self._stage         = AppStage.IDLE
        self._program       = None
        self._step          = 0
        self._stats         = {"total": 0, "pass": 0, "fail": 0, "errors": 0}
        self._last_event_time = None
        self._start_time    = None
        self._main_thread   = None
        self._state_lock    = threading.Lock()
        self._stop_app      = threading.Event()

    # ── Session stats persistence ──────────────────────────────────────

    def _load_session_stats(self) -> dict:
        """تحميل الإحصائيات المحفوظة من الجلسة السابقة (stop/start)."""
        try:
            if os.path.exists(_SESSION_STATS_FILE):
                with open(_SESSION_STATS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # تأكد من صحة الشكل
                    stats   = {k: int(data.get(k, 0)) for k in ("total", "pass", "fail", "errors")}
                    barcode = data.get("last_barcode")
                    return {"stats": stats, "last_barcode": barcode}
        except Exception:
            pass
        return {"stats": {"total": 0, "pass": 0, "fail": 0, "errors": 0}, "last_barcode": None}

    def _save_session_stats(self):
        """حفظ الإحصائيات الحالية على الـ disk عند الإيقاف."""
        try:
            with self._state_lock:
                data = dict(self._stats)
                data["last_barcode"] = self.barcode
            with open(_SESSION_STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    def reset_session_stats(self):
        """إعادة تعيين الإحصائيات المتراكمة والملف."""
        with self._state_lock:
            self._stats  = {"total": 0, "pass": 0, "fail": 0, "errors": 0}
            self.barcode = None
        try:
            if os.path.exists(_SESSION_STATS_FILE):
                os.remove(_SESSION_STATS_FILE)
        except Exception:
            pass

    # ── Camera & AI builder ────────────────────────────────────────────

    def _build_camera(self) -> CameraHub:
        cam_type  = self._cfg.get("camera_type",  "useeplus")
        cam_index = self._cfg.get("camera_index", 0)
        if cam_type == "opencv":
            return CameraHub.OpenCV(camera_index=cam_index)
        return CameraHub.UseePlus(camera_index=cam_index)

    def _build_ai_provider(self) -> WaterDetector:
        agent   = self._cfg.get("AI_Agent")
        model   = self._cfg.get("ai_model")
        enhance = self._cfg.get("ai_enhancement", False)

        if agent == "groq":
            return WaterDetector.Groq(model=model, use_enhancement=enhance)
        elif agent == "local_ollama":
            return WaterDetector.Local(model=model, backend="ollama",    use_enhancement=enhance)
        elif agent == "local_lmstudio":
            return WaterDetector.Local(model=model, backend="lm_studio", use_enhancement=enhance)
        else:
            return WaterDetector.Gemini(model=model, use_enhancement=enhance)

    # ── State helpers ──────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    def _set_stage(self, stage: str, step: int = 0):
        """تحديث المرحلة الحالية بشكل thread-safe."""
        with self._state_lock:
            self._stage = stage
            self._step  = step
            self._last_event_time = time.time()

    def get_state_snapshot(self) -> dict:
        """
        يرجع snapshot من الحالة الحالية للـ web dashboard.
        آمن للنداء من أي thread.
        """
        # BUG-027: استخدام is_listener_running() العامة بدل _listener_started الخاص
        # القراءات دي برّه الـ lock عشان is_running() تأخد الـ lock بتاعها من غير deadlock
        camera_ok  = self._camera.is_running()
        scanner_ok = sc.is_listener_running() or camera_barcode.is_running()

        with self._state_lock:
            uptime     = (time.time() - self._start_time) if (self._start_time and self._running) else 0
            robot_ok   = self.robot is not None
            return {
                "is_running":        self._running,
                "stage":             self._stage,
                "barcode":           self.barcode,
                "program":           self._program,
                "step":              self._step,
                "vision_test_count": AppStage.get_vision_test_count(),
                "stats":             dict(self._stats),
                "queue_sizes": {
                    "vision_queue":  0,
                    "scanner_queue": sc.queue_barcode.qsize(),
                },
                "last_event_time": self._last_event_time,
                "uptime":          uptime,
                "connections": {
                    "robot":   robot_ok,
                    "camera":  camera_ok,
                    "ai":      True,        # WaterDetector جاهز دايماً
                    "scanner": scanner_ok,
                },
            }

    # ── Images / AI ───────────────────────────────────────────────────

    def check_images_status(self, images_data):
        if isinstance(images_data, str) and "Error:" in images_data:
            print(f"[ERROR] check_images_status: AI error: {images_data}")
            return "error"
        if isinstance(images_data, str):
            try:
                images_data = json.loads(images_data)
            except Exception as e:
                print(f"[ERROR] check_images_status: JSON parse failed: {e}")
                return "error"
        if not isinstance(images_data, dict):
            print(f"[ERROR] check_images_status: expected dict, got {type(images_data)}")
            return "error"
        for image_name, value in images_data.items():
            if str(value).strip().lower() == "yes":
                return "fail"
        return "pass"

    # ── Scanner / Barcode ──────────────────────────────────────────────

    def get_barcode_from_scanner(self):
        """
        ينتظر الباركود من scanner.queue_barcode.
        يرجع None لو البرنامج وقف (self._stop_app.is_set()).
        """
        log = _get_thread_logger()
        while not self._stop_app.is_set():
            try:
                barcode = sc.queue_barcode.get(timeout=0.5)
                sc.queue_barcode.task_done()
                return barcode
            except queue.Empty:
                continue
        return None

    # ── Robot helpers ─────────────────────────────────────────────────

    def get_points_from_db(self, point_name: str):
        import sqlite3
        # BUG-003: إصلاح SQL Injection — استخدام parameterized query
        # Docker: web_point.db جوه DATA_DIR (Volume) عشان يتحفظ بين restarts
        db_path = _data_path("web_point.db")
        conn   = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT j1, j2, j3, j4, j5, j6 FROM points WHERE name = ?",
            (point_name,)
        )
        result = cursor.fetchone()
        conn.close()
        if result:
            joint_angles = [float(x) for x in result]
            print("Joint Angles as Floats:", joint_angles)
            return joint_angles
        else:
            print(f"النقطة '{point_name}' مش موجودة في الداتا بيز.")
            return [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    def switch_camera(self):
        """BUG-010/011: كانت async وبتتنادى بـ asyncio.run() → RuntimeError تحت uvicorn."""
        self.robot.SetDO(self._cfg.get(key="Switch_camera"), 1)
        time.sleep(3)
        self.robot.SetDO(self._cfg.get(key="Switch_camera"), 0)

    # ── Programs ──────────────────────────────────────────────────────

    def program_1(self):
        log = _get_thread_logger()
        homing  = self.get_points_from_db("water1")
        _10kg_1 = self.get_points_from_db("10kg_1")
        _10kg_2 = self.get_points_from_db("10kg_2")
        _10kg_3 = self.get_points_from_db("10kg_3")
        ready   = self.get_points_from_db("ready")

        # BUG-010: asyncio.run() أُزيل — switch_camera أصبحت sync
        self.switch_camera()
        self.robot.MoveJ(joint_pos=ready,   tool=0, user=1, vel=100, acc=100)

        # Vision test 1
        self._set_stage(AppStage.vision_stage(0), step=1)
        self.robot.MoveJ(joint_pos=_10kg_1, tool=0, user=1, vel=100, acc=100)
        img0 = ct.trigger(name=self.barcode + "_0")   # BUG-012: نحفظ المسار الفعلي
        time.sleep(1)

        # Vision test 2
        self._set_stage(AppStage.vision_stage(1), step=2)
        self.robot.MoveJ(joint_pos=_10kg_2, tool=0, user=1, vel=100, acc=100)
        img1 = ct.trigger(name=self.barcode + "_1")
        time.sleep(1)

        # Vision test 3
        self.switch_camera()                           # BUG-010: sync
        self._set_stage(AppStage.vision_stage(2), step=3)
        self.robot.MoveJ(joint_pos=ready,   tool=0, user=1, vel=100, acc=100)
        self.robot.MoveJ(joint_pos=_10kg_3, tool=0, user=1, vel=100, acc=100)
        img2 = ct.trigger(name=self.barcode + "_2")
        time.sleep(1)

        # Return home
        self.robot.MoveJ(joint_pos=homing,  tool=0, user=1, vel=100, acc=100)

        # Reporting — BUG-012: مسارات الصور من trigger() بدل hardcoded
        self._set_stage(AppStage.REPORTING)
        image_list = [p for p in (img0, img1, img2) if p is not None]
        result = self.check_images_status(
            self._ai_provider.run(image_paths=image_list)
        )
        ex.result_reporting(ID=self.barcode, result=result)

        # Update stats
        with self._state_lock:
            if result == "pass":
                self._stats["pass"]   += 1
            elif result == "fail":
                self._stats["fail"]   += 1
            else:
                self._stats["errors"] += 1

        # Signal robot — BUG-033: مفاتيح config صُحّحت (period بدل preriod)
        self.robot.SetDO(self._cfg.get(key="test_done"),   1)
        self.robot.SetDO(self._cfg.get(key="yellow_led"),  0)
        if result == "pass":
            self.robot.SetDO(self._cfg.get(key="test_pass"), 1)
            time.sleep(self._cfg.get("signal_pass_period", 0.5))
            self.robot.SetDO(self._cfg.get(key="test_pass"), 0)
        elif result == "fail":
            self.robot.SetDO(self._cfg.get(key="test_fail"), 1)
            time.sleep(self._cfg.get("signal_fail_period", 0.5))
            self.robot.SetDO(self._cfg.get(key="test_fail"), 0)

        self._set_stage(AppStage.DONE)
        log.info(f"[program_1] Done — barcode={self.barcode}  result={result}")

    def program_2(self):
        pass

    def program_3(self):
        pass

    def program_4(self):
        pass

    def program_5(self):
        pass

    # ── Sequence ──────────────────────────────────────────────────────

    def start_sequence(self):
        log = _get_thread_logger()

        # اتحرك لنقطة المسح — BUG-013: "CamScan" → "cam" (اسم موجود فعلاً في DB)
        barcode_point = self.get_points_from_db("cam")
        self.robot.MoveJ(barcode_point, 0, 1, vel=100, acc=100)

        # شغّل وضع القراءة
        scan_mode = self._cfg.get(key="scan_mode")
        if scan_mode == "camera":
            camera_barcode.start(camera=self._camera)
        elif scan_mode == "manual":
            sc.start_listener()

        # انتظر الباركود
        self._set_stage(AppStage.IDLE)
        self.barcode = self.get_barcode_from_scanner()

        if self._stop_app.is_set() or self.barcode is None:
            return   # البرنامج وقف

        # وقّف وضع القراءة
        if scan_mode == "camera":
            camera_barcode.stop()
        elif scan_mode == "manual":
            sc.stop_listener()

        log.info(f"[Sequence] Barcode: {self.barcode}")
        self._set_stage(AppStage.BARCODE_RECEIVED)

        # بحث عن البرنامج — BUG-014: ترجع None بدل string عربي عند الخطأ
        self._set_stage(AppStage.PROGRAM_LOOKUP)
        program = self.determine_program_from_barcode(barcode=self.barcode)

        if program is None:
            log.error(f"[Sequence] Could not determine program for barcode: {self.barcode!r}")
            self._set_stage(AppStage.ERROR)
            return

        with self._state_lock:
            self._program = program
            self._stats["total"] += 1

        # إرسال البرنامج للكوبوت
        self._set_stage(AppStage.SENDING_PROGRAM)
        log.info(f"[Sequence] Program: {program}")

        if program == 1:
            self.program_1()
        elif program == 2:
            self.program_2()
        elif program == 3:
            self.program_3()
        elif program == 4:
            self.program_4()
        elif program == 5:
            self.program_5()
        else:
            # BUG-014: programs 2-5 فارغة → ERROR مع رسالة واضحة
            log.warning(f"[Sequence] Program {program} has no implementation (programs 2-5 are empty stubs)")
            self._set_stage(AppStage.ERROR)

    # ── Program mapping ────────────────────────────────────────────────

    def determine_program_from_barcode(self, barcode, excel_file_path=None):
        """
        BUG-014: كانت بترجع string عربي عند الخطأ → أصبحت ترجع None عند الخطأ
        عشان start_sequence يقدر يتعامل معاها بشكل صحيح.
        """
        log = _get_thread_logger()

        if excel_file_path is None:
            fname = _cfg_singleton.get("program_mapping_file", "program_mapping.xlsx")
            excel_file_path = fname if os.path.isabs(fname) else _data_path(fname)

        if not barcode or len(barcode) < 3:
            log.error(f"[Program] Barcode too short: {barcode!r}")
            return None

        target_char = barcode[-3]

        try:
            try:
                current_mtime = os.path.getmtime(excel_file_path)
            except OSError:
                current_mtime = 0.0

            if (self._mapping_cache_df is None
                    or self._mapping_cache_path != excel_file_path
                    or self._mapping_cache_mtime != current_mtime):
                log.info(f"[Program] Loading mapping file: {excel_file_path}")
                self._mapping_cache_df    = pd.read_excel(excel_file_path)
                self._mapping_cache_path  = excel_file_path
                self._mapping_cache_mtime = current_mtime

            df           = self._mapping_cache_df
            char_column  = df.columns[0]
            value_column = df.columns[1]
            match        = df[df[char_column] == target_char]

            if not match.empty:
                raw = match[value_column].values[0]
                try:
                    return int(raw)
                except (ValueError, TypeError):
                    log.error(f"[Program] Value {raw!r} is not a valid program number")
                    return None
            else:
                log.error(f"[Program] Char '{target_char}' not found in mapping file")
                return None

        except FileNotFoundError:
            log.error(f"[Program] Excel file not found: {excel_file_path}")
            return None
        except Exception as e:
            log.exception(f"[Program] Unexpected error: {e}")
            return None

    # ── Main loop (runs in background thread) ─────────────────────────

    def _run_main(self):
        """اللوب الرئيسي — بيشتغل في ثريد خلفي لما start() يتنادى."""
        log = _get_thread_logger()
        log.info("[App] _run_main started")
        try:
            # تشغيل الكاميرا — BUG-037: تحقق من نجاح التشغيل
            self._camera.start()
            if not self._camera.wait_for_frame(timeout=5.0):
                log.error("[App] Camera failed to produce frames — aborting")
                self._set_stage(AppStage.ERROR)
                return
            ct.start(camera=self._camera)

            # اتصل بالروبوت
            self.robot = RPC(self.robot_ip)
            homing = self.get_points_from_db("water1")
            self.robot.MoveJ(joint_pos=homing, tool=0, user=1, vel=100, acc=100)
            self.robot.SetDO(self._cfg.get(key="test_done"), 1)

            last = 0
            self._set_stage(AppStage.IDLE)
            log.info("[App] Ready — waiting for trigger DI0")

            while not self._stop_app.is_set():
                try:
                    ret = self.robot.GetDI(self._cfg.get(key="input_trigger"), 0)
                    if isinstance(ret, (list, tuple)):
                        DI0 = int(ret[1]) if len(ret) > 1 else int(ret[0])
                    else:
                        DI0 = int(ret) if ret is not None else 0
                except Exception as e:
                    log.warning(f"[App] GetDI error: {e} — retrying...")
                    time.sleep(1.0)
                    continue

                if DI0 == 1 and last == 0:
                    log.info("[App] DI0 HIGH — starting sequence")
                    self.robot.SetDO(self._cfg.get(key="test_done"), 0)
                    self.robot.SetDO(self._cfg.get(key="yellow_led"), 1)
                    self.start_sequence()
                    # رجع لحالة الانتظار بعد انتهاء الـ sequence
                    if not self._stop_app.is_set():
                        self._set_stage(AppStage.IDLE)
                        self.robot.SetDO(self._cfg.get(key="test_done"), 1)

                last = DI0
                time.sleep(0.1)

        except Exception as e:
            log.exception(f"[App] _run_main error: {e}")
            self._set_stage(AppStage.ERROR)
        finally:
            self._running = False
            self._save_session_stats()   # حفظ الإحصائيات عند الخروج
            self.robot    = None
            try:
                self._camera.stop()
            except Exception:
                pass
            try:
                camera_barcode.stop()
            except Exception:
                pass
            log.info("[App] _run_main finished")

    # ── start / stop ──────────────────────────────────────────────────

    def start(self, camera_index=None):
        """
        يشغّل البرنامج في ثريد خلفي ويرجع True/False فوراً (non-blocking).
        الإحصائيات بتتحمل من الجلسة السابقة (stop/start تحافظ على الأرقام).
        """
        if self._running:
            return True

        # ── انتظر الـ thread القديم يخلص تماماً قبل ما نبدأ واحد جديد ──
        # (الـ thread ممكن يكون لسه بيوقف الكاميرا/الروبوت في finally)
        if self._main_thread is not None and self._main_thread.is_alive():
            self._main_thread.join(timeout=8.0)
            if self._main_thread.is_alive():
                # الـ thread عالق — مش آمن نبدأ من جديد
                return False

        if camera_index is not None:
            self._camera._cam_index = camera_index

        # ── تحميل إحصائيات الجلسة السابقة ──────────────────────────
        saved = self._load_session_stats()

        self._stop_app.clear()
        self._start_time = time.time()
        with self._state_lock:
            self._stage   = AppStage.IDLE
            # BUG-050: persistent stats — مش بنصفّر، بنرجع من الجلسة السابقة
            self._stats   = saved["stats"]
            self.barcode  = saved["last_barcode"]
            self._program = None
            self._step    = 0

        # BUG-050: مسح queue الباركودات القديمة
        sc.reset_queue()

        self._main_thread = threading.Thread(
            target=self._run_main,
            name="app-main",
            daemon=True,
        )
        self._main_thread.start()
        # BUG-028: _running = True بعد start() وليس قبلها
        self._running = True
        return True

    def run(self):
        return self.start()

    def stop(self, wait: bool = False, timeout: float = 10.0):
        """
        يوقف البرنامج.
        BUG-049: ضيفنا wait=True لانتظار انتهاء الـ thread (للـ shutdown).
        FIX-START: _running = False فوراً عشان start() تشتغل صح بعد stop().
        """
        self._save_session_stats()   # حفظ الإحصائيات قبل الإيقاف
        self._running = False        # فوراً — مش نستنى الـ thread finally
        self._stop_app.set()
        if wait and self._main_thread is not None:
            self._main_thread.join(timeout=timeout)


if __name__ == "__main__":
    app = App()
    app.start()
    # في وضع CLI نستنى لحد ما يتوقف
    try:
        while app.is_running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        app.stop()
