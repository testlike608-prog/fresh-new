from cmath import log
import asyncio
import json
import socket
import threading
import time
import queue
import xmlrpc.client
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


def _parse_entry(value) -> tuple[str, float | None]:
    """
    يوحّد شكل نتيجة صورة واحدة ويرجع (answer, confidence).

    بيدعم الشكلين:
      - الجديد: {"answer": "Yes", "confidence": 0.92}
      - القديم: "Yes"  → confidence = None
    """
    if isinstance(value, dict):
        ans = str(value.get("answer", "No"))
        try:
            conf = float(value.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        return ans, max(0.0, min(1.0, conf))
    return str(value), None



def load_session_stats() -> dict:
    """
    قراءة إحصائيات آخر جلسة من الـ disk — بدون الحاجة لـ App instance،
    عشان الداشبورد تعرض الأرقام وإحنا في حالة STOPPED (مفيش App).
    """
    try:
        if os.path.exists(_SESSION_STATS_FILE):
            with open(_SESSION_STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            stats = {k: int(data.get(k, 0)) for k in ("total", "pass", "fail", "errors")}
            return {"stats": stats, "last_barcode": data.get("last_barcode")}
    except Exception:
        pass
    return {"stats": {"total": 0, "pass": 0, "fail": 0, "errors": 0}, "last_barcode": None}


def clear_session_stats():
    """مسح ملف إحصائيات الجلسة من الـ disk — بدون الحاجة لـ App instance."""
    try:
        if os.path.exists(_SESSION_STATS_FILE):
            os.remove(_SESSION_STATS_FILE)
            return True
    except Exception:
        pass
    return False


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


class RobotPoint(list):
    """
    نقطة روبوت = list بالزوايا الستة (j1..j6) **وجواها كمان** الـ pose
    الكارتيزي المعلّم (x,y,z,rx,ry,rz) في `.desc`.

    ليه list؟ عشان تفضل تشتغل مع MoveJ بالظبط زي الأول من غير أي تعديل
    (الـ SDK بيعمل list(map(float, joint_pos)) وخلاص)، وفي نفس الوقت
    MoveL تلاقي الـ desc_pos اللي هي محتاجاه جاهز.
    """
    __slots__ = ("desc", "name")

    def __new__(cls, joints, desc=None, name=None):
        obj = super().__new__(cls, joints)
        return obj

    def __init__(self, joints, desc=None, name=None):
        super().__init__(joints)
        self.desc = desc
        self.name = name

    def has_desc(self) -> bool:
        """True لو فيه pose كارتيزي حقيقي (مش أصفار)."""
        return bool(self.desc) and any(abs(float(v)) > 1e-9 for v in self.desc)


class StopRequested(Exception):
    """
    بيتم رفعه من أي نقطة توقف (checkpoint) جوه السيكوانس لما المستخدم يدوس Stop.
    مش error — ده الطريق الطبيعي للخروج من سيكوانس نصّها، فبيتم التقاطه
    في run_async() من غير ما يتسجل كـ ERROR.
    """
    pass


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
        self._state_lock    = threading.Lock()

        # ── Stop/teardown lifecycle ────────────────────────────────────
        # _stop_app  : Event عشان الكود اللي بيشتغل جوه thread (to_thread)
        #              يقدر يشوف إن فيه stop مطلوب — asyncio مش بيقدر
        #              يقتل thread، فالخروج لازم يكون تعاوني.
        # _task      : الـ asyncio.Task بتاع اللوب الرئيسي (بدل الـ thread)
        # _closed    : teardown اتعمل بالفعل (idempotent)
        self._stop_app      = threading.Event()
        self._task: "asyncio.Task | None" = None
        self._closed        = False
        self._stop_reason   = None

    # ── Session stats persistence ──────────────────────────────────────

    def _load_session_stats(self) -> dict:
        """تحميل الإحصائيات المحفوظة من الجلسة السابقة (stop/start)."""
        return load_session_stats()

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
        clear_session_stats()

    # ── Camera & AI builder ────────────────────────────────────────────

    def _build_camera(self) -> CameraHub:
        cam_type  = self._cfg.get("camera_type",  "useeplus")
        cam_index = self._cfg.get("camera_index", 0)
        if cam_type == "opencv":
            return CameraHub.OpenCV(camera_index=cam_index)
        return CameraHub.UseePlus(camera_index=cam_index, upscale=False)

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
        elif agent == "clip":
            # موديل محلي مدرّب (SigLIP classifier) — مفيش API key ولا انترنت.
            # الأوزان بتتحمل مرة واحدة هنا، مش مع كل صورة.
            path   = (self._cfg.get("clip_model_path") or "siglip_leak_classifier.pt").strip()
            device = (self._cfg.get("clip_device") or "auto").strip().lower()
            # ntpath عشان مسار ويندوز (D:\...) يتعرف كمسار مطلق حتى
            # لو السيرفر شغال في Docker/Linux
            import ntpath as _nt
            if not (os.path.isabs(path) or _nt.isabs(path)):
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
            return WaterDetector.CLIP(
                model_path      = path,
                device          = None if device in ("", "auto") else device,
                use_enhancement = enhance,
            )
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
                "last_images":     list(self._last_images),
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
            has_error = False                                    # ← جديد
            for image_name, value in images_data.items():
                ans, conf = _parse_entry(value)
                val_cleaned = ans.strip().lower()
                print(f"Checking: {image_name} -> Value: '{val_cleaned}' (confidence: {conf})")

                if val_cleaned == "yes":
                    return "fail"     # مايه في أي صورة = فشل فوراً

                if val_cleaned not in ("no",):                   # ← جديد: أي إجابة غريبة
                    has_error = True                             #    (زي "Error") بتتسجل
                    print(f"[ERROR] check_images_status: {image_name} answer='{ans}' — مش إجابة صالحة")

            if has_error:                                        # ← جديد
                return "error"        # مفيش مايه مؤكدة بس فيه صور فشلت = مينفعش pass
            return "pass"


        # ── Scanner / Barcode ──────────────────────────────────────────────

    async def get_barcode_from_scanner(self):
        """
        ينتظر الباركود من scanner.queue_barcode — async وقابل للإلغاء فورًا.

        BUG-FIX (سبب "Stop بيقفل بس Start مش بيبدأ"):
        الإصدار القديم كان بينادي `sc.queue_barcode.get()` **بدون timeout**،
        فالثريد كان يقف ميت جوه الـ get() للأبد. لما المستخدم يدوس Stop،
        الـ _stop_app بتتظبط و stop_listener() بيوقف تغذية الكيو — يعني مفيش
        حاجة هتدخل الكيو تاني ⇒ الثريد عمره ما يموت ⇒ start() بعد كده كان
        بيلاقي الثريد القديم لسه عايش ويرجع False (503) للأبد.

        الحل: polling بـ get_nowait() + await asyncio.sleep() — فالتاسك بتخرج
        فورًا لو اتعمل cancel أو لو _stop_app اتظبطت.
        """
        log = _get_thread_logger()
        log.info("[Sequence] في انتظار الباركود...")
        while not self._stop_app.is_set():
            try:
                barcode = sc.queue_barcode.get_nowait()
                sc.queue_barcode.task_done()
                log.info(f"[Sequence] الباركود: {barcode}")
                return barcode
            except queue.Empty:
                await asyncio.sleep(0.1)   # ← نقطة إلغاء (cancellation point)
        return None

    # ── Robot helpers ─────────────────────────────────────────────────

    def get_points_from_db(self, point_name: str) -> RobotPoint:
        """
        يرجع RobotPoint: الزوايا الستة + الـ pose الكارتيزي المعلّم.

        الجدول فيه العمودين (j1..j6 و x,y,z,rx,ry,rz) لكل نقطة، فبناخد
        الاتنين: MoveJ تستخدم الزوايا زي الأول، و MoveL تستخدم الـ
        desc_pos المعلّم بدل ما ترفع TypeError (كانت ناقصة desc_pos).
        """
        import sqlite3
        # BUG-003: إصلاح SQL Injection — استخدام parameterized query
        # Docker: web_point.db جوه DATA_DIR (Volume) عشان يتحفظ بين restarts
        db_path = _data_path("web_point.db")
        conn   = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT j1, j2, j3, j4, j5, j6, x, y, z, rx, ry, rz "
            "FROM points WHERE name = ?",
            (point_name,)
        )
        result = cursor.fetchone()
        conn.close()

        if not result:
            print(f"النقطة '{point_name}' مش موجودة في الداتا بيز.")
            # من غير desc → MoveL هترفع رسالة واضحة بدل ما تتحرك لأصفار
            return RobotPoint([0.0] * 6, desc=None, name=point_name)

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        joints = [_f(v) for v in result[:6]]
        desc   = [_f(v) for v in result[6:12]]
        print(f"[Point] {point_name}  j={joints}  desc={desc}")
        return RobotPoint(joints, desc=desc, name=point_name)

    def switch_camera(self):
        """نسخة sync — متبقية للاستخدام من سكربتات/CLI برّه الـ event loop."""
        self.robot.SetDO(self._cfg.get(key="Switch_camera"), 1)
        time.sleep(3)
        self.robot.SetDO(self._cfg.get(key="Switch_camera"), 0)

    # ══════════════════════════════════════════════════════════════════
    #  نقط التوقف التعاونية + أغلفة الهاردوير (async)
    # ══════════════════════════════════════════════════════════════════
    # ليه كل نداء هاردوير بيمر من هنا؟
    #   fairino RPC و OpenCV/pyusb و torch كلهم سينكروني بالكامل،
    #   و asyncio **مش بيقدر** يلغي نداء بلوكينج جوه thread. فالخروج من
    #   سيكوانس نصّها لازم يكون "تعاوني": بنتشيّك على _stop_app قبل وبعد
    #   كل نداء، ولو اتظبطت نرفع StopRequested ونخرج من السيكوانس.

    def _raise_if_stopping(self):
        """نقطة توقف — بترفع StopRequested لو Stop اتدوس."""
        if self._stop_app.is_set():
            raise StopRequested(self._stop_reason or "stop requested")

    async def _call(self, fn, *args, **kwargs):
        """
        ينفّذ نداء هاردوير بلوكينج في thread executor، مع نقطة توقف
        قبله وبعده. بيرجع نتيجة النداء زي ما هي.
        """
        self._raise_if_stopping()
        result = await asyncio.to_thread(fn, *args, **kwargs)
        self._raise_if_stopping()
        return result

    async def _sleep(self, seconds: float):
        """
        بديل time.sleep — بيتشيّك على Stop كل 50ms، فالاستجابة شبه فورية
        بدل ما ننتظر الـ sleep كله يخلص. ومابياخدش thread من الـ executor.
        """
        self._raise_if_stopping()
        deadline = time.monotonic() + float(seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.05, remaining))
            self._raise_if_stopping()

    async def _pulse_do(self, port, seconds: float):
        """
        نبضة DO (1 → انتظار → 0) **مضمونة الرجوع لصفر** حتى لو Stop
        اتدوس في نص النبضة.

        قبل كده: لو Stop اتدوس بين SetDO(1) و SetDO(0)، خط الـ
        pass/fail كان بيفضل مرفوع (1) بعد الإيقاف.
        """
        await self._set_do(port, 1)
        try:
            await self._sleep(seconds)
        finally:
            if self.robot is not None:
                try:
                    await asyncio.to_thread(self.robot.SetDO, port, 0)
                except Exception:
                    pass

    # ⚠️ الأغلفة التالية بتمرّر الـ arguments **بالحرف** زي ما البرامج
    #    بتبعتها بالظبط — عمداً. مفيش ولا argument بيتضاف أو يتغير، عشان
    #    أوامر الحركة المرسلة للكوبوت تبقى مطابقة ١٠٠% للكود القديم.
    #    اللي بيتضاف هو نقطة التوقف قبل وبعد النداء فقط.

    async def _move_j(self, *args, **kwargs):
        """MoveJ مع نقطة توقف.
        NOTE: الحركة نفسها مش قابلة للقطع من asyncio (نداء بلوكينج جوه thread) —
        القطع الفوري بيحصل عن طريق StopMotion() في request_stop()."""
        return await self._call(self.robot.MoveJ, *args, **kwargs)

    async def _move_l(self, *args, **kwargs):
        """
        MoveL مع نقطة توقف + حل الـ desc_pos تلقائيًا.

        الخلفية: توقيع الـ SDK هو MoveL(desc_pos, tool, user, joint_pos=...)
        و desc_pos **مطلوب**، لكن كل النداءات في البرامج كانت بتبعت
        joint_pos بس ⇒ TypeError عند أول MoveL في أي برنامج.

        الحل: كل نقطة جاية من get_points_from_db() شايلة معاها الـ pose
        الكارتيزي المعلّم (x,y,z,rx,ry,rz) من نفس صف الـ DB، فبناخده
        كـ desc_pos. مش بنحسب ولا بنخمّن حاجة — ده الـ pose اللي اتعلّم
        من الـ teach pendant بنفسه.
        """
        if "desc_pos" not in kwargs and len(args) == 0:
            pt = kwargs.get("joint_pos")
            if isinstance(pt, RobotPoint) and pt.has_desc():
                kwargs["desc_pos"] = list(pt.desc)
            else:
                name = getattr(pt, "name", None) or "<unknown>"
                raise RuntimeError(
                    f"MoveL للنقطة '{name}': مفيش pose كارتيزي (desc_pos) "
                    f"في الـ DB — اتأكد إن أعمدة x,y,z,rx,ry,rz للنقطة دي "
                    f"متعلّمة ومش أصفار."
                )
        return await self._call(self.robot.MoveL, *args, **kwargs)

    async def _set_do(self, *args, **kwargs):
        """SetDO مع نقطة توقف — بيتخطى بهدوء لو الروبوت اتقفل بالفعل."""
        if self.robot is None:
            return None
        return await self._call(self.robot.SetDO, *args, **kwargs)

    async def _switch_camera(self):
        """تبديل الكاميرا: DO=1 → استنى 3 ثواني (قابلة للإلغاء) → DO=0."""
        port = self._cfg.get(key="Switch_camera")
        await self._set_do(port, 1)
        try:
            await self._sleep(3)
        finally:
            # حتى لو Stop اتدوس في النص، لازم نرجّع الـ DO لـ 0
            if self.robot is not None:
                try:
                    await asyncio.to_thread(self.robot.SetDO, port, 0)
                except Exception:
                    pass

    async def _capture(self, save_path: str = None, name: str = "capture"):
        """
        التقاط صورة في thread (cv2.imwrite بلوكينج) + تسجيل المسار
        في self._last_images عشان يبان في الـ debug snapshot.
        """
        path = await self._call(ct.trigger, save_path, name)
        if path:
            with self._state_lock:
                self._last_images.append(path)
        return path

    async def _ai_run(self, image_list):
        """
        نداء الـ AI في thread — الـ providers كلها سينكروني
        (HTTP مع retries، أو torch inference على الـ GPU).
        ملحوظة: مش بنعمل raise بعد النداء عشان نتيجة الفحص متتوهش لو
        المستخدم دوس Stop وهو بيحلل — بنسيب الـ reporting يكمّل.
        """
        self._raise_if_stopping()
        return await asyncio.to_thread(self._ai_provider.run, image_paths=image_list)


    # ── Programs ──────────────────────────────────────────────────────

    async def program_1(self):
        log = _get_thread_logger()
        homing  = self.get_points_from_db("Homming")
        cam_parq= self.get_points_from_db("cam_parq")
        cam_relese= self.get_points_from_db("cam_relese")
        model1_prepoint1= self.get_points_from_db("model1_prepoint1")
        model1_point1 = self.get_points_from_db("model1_point1")
        model1_point2 = self.get_points_from_db("model1_point2")
        model1_point3 = self.get_points_from_db("model1_point3")
        model1_point4 = self.get_points_from_db("model1_point4")
        model1_point5   = self.get_points_from_db("model1_point5")
        model1_point5_re   = self.get_points_from_db("model1_point5_re")
        model1_point5_re2   = self.get_points_from_db("model1_point5_re2")
        homming2   = self.get_points_from_db("Homming2")
        

        # BUG-010: asyncio.run() أُزيل — switch_camera أصبحت sync
        await self._move_j(joint_pos=homing,   tool=0, user=1, vel=60, acc=100)
        # await self._move_j(joint_pos=cam_parq, tool=0, user=1, vel=100, acc=100)
        # await self._move_j(joint_pos=cam_relese, tool=0, user=1, vel=100, acc=100)
        await self._move_j(joint_pos=model1_prepoint1, tool=0, user=1, vel=100, acc=100)
        # Vision test 1
        
        self._set_stage(AppStage.vision_stage(1), step=1)
        await self._move_l(joint_pos=model1_point1, tool=0, user=1, vel=60, acc=100)
        #await self._sleep(1)
        img0 = await self._capture(name=self.barcode + "_0")   # BUG-012: نحفظ المسار الفعلي
        #await self._sleep(1)
        await self._switch_camera()
        self._set_stage(AppStage.vision_stage(2), step=2)
        await self._move_l(joint_pos=model1_point2, tool=0, user=1, vel=100, acc=100)

        img1 = await self._capture(name=self.barcode + "_1") 
        await self._switch_camera()

        self._set_stage(AppStage.vision_stage(3), step=3)
        await self._move_l(joint_pos= model1_point3, tool=0, user=1, vel=100, acc=100)
        img2 = await self._capture(name=self.barcode + "_2")
        self._set_stage(AppStage.vision_stage(4), step=4)
        await self._move_l(joint_pos=model1_point4, tool=0, user=1, vel=100, acc=100)
        img3 = await self._capture(name=self.barcode + "_3")
   
        await self._switch_camera()                           # BUG-010: sync
        self._set_stage(AppStage.vision_stage(5), step=5)
        await self._move_l(joint_pos=model1_point5,   tool=0, user=1, vel=100, acc=100)
        img4 = await self._capture(name=self.barcode + "_4")
        await self._switch_camera() 
        await self._move_l(joint_pos=model1_point5_re, tool=0, user=1, vel=100, acc=100)
       
        await self._move_l(joint_pos=model1_point5_re2,  tool=0, user=1, vel=100, acc=100)

    #await self._move_l(joint_pos=homming2,  tool=0, user=1, vel=100, acc=100)
        await self._move_l(joint_pos=homing,  tool=0, user=1, vel=100, acc=100)

        # Reporting — FIX: استخدام الصورة الملتقطة فعلياً (img0) بدل مسار hardcoded
        self._set_stage(AppStage.REPORTING)
        image_list = [p for p in [img0] if p]
        if not image_list:
            log.error(f"[program_1] Camera trigger failed — no image captured for barcode={self.barcode}")
            res    = "Error: no captured image"
            result = "error"
        else:
            res    = await self._ai_run(image_list)
            result = self.check_images_status(res)
        log.info(f"[program_1] Done — model answer — barcode={self.barcode}  result={res}")
        await asyncio.to_thread(ex.result_reporting, ID=self.barcode, result=result)

        # Update stats
        with self._state_lock:
            if result == "pass":
                self._stats["pass"]   += 1
            elif result == "fail":
                self._stats["fail"]   += 1
            else:
                self._stats["errors"] += 1

        # Signal robot — BUG-033: مفاتيح config صُحّحت (period بدل preriod)
        await self._set_do(self._cfg.get(key="test_done"),   1)
        await self._set_do(self._cfg.get(key="yellow_led"),  0)
        if result == "pass":
            await self._pulse_do(self._cfg.get(key="test_pass"),
                                 self._cfg.get("signal_pass_period", 0.5))
        elif result == "fail":
            await self._pulse_do(self._cfg.get(key="test_fail"),
                                 self._cfg.get("signal_fail_period", 0.5))

        self._set_stage(AppStage.DONE)
        log.info(f"[program_1] Done — barcode={self.barcode}  result={result}")

    async def program_2(self):
        log = _get_thread_logger()
        homing  = self.get_points_from_db("Homming")
        cam_parq= self.get_points_from_db("cam_parq")
        cam_relese= self.get_points_from_db("cam_relese")
        model2_prepoint1= self.get_points_from_db("model2_prepoint1")
        model2_point1 = self.get_points_from_db("model2_point1")
        model2_point2 = self.get_points_from_db("model2_point2")
        model2_point3 = self.get_points_from_db("model2_point3")
        model2_point4 = self.get_points_from_db("model2_point4")
        model2_point4_res = self.get_points_from_db("model2_point4_res")
        model2_point4_res2 = self.get_points_from_db("model2_point4_res2")
        model2_point5   = self.get_points_from_db("model2_point5")
        model2_point5_re2   = self.get_points_from_db("model2_point5_re2")

        # BUG-010: asyncio.run() أُزيل — switch_camera أصبحت sync
        await self._move_j(joint_pos=homing,   tool=0, user=1, vel=60, acc=100)
        # await self._move_j(joint_pos=cam_parq, tool=0, user=1, vel=100, acc=100)
        # await self._move_j(joint_pos=cam_relese, tool=0, user=1, vel=100, acc=100)
        await self._move_j(joint_pos=model2_prepoint1, tool=0, user=1, vel=100, acc=100)
        # Vision test 1
        
        self._set_stage(AppStage.vision_stage(1), step=1)
        await self._move_l(joint_pos=model2_point1, tool=0, user=1, vel=60, acc=100)
        #await self._sleep(1)
        img0 = await self._capture(name=self.barcode + "_0")   # BUG-012: نحفظ المسار الفعلي
        #await self._sleep(1)
        await self._switch_camera()

        self._set_stage(AppStage.vision_stage(2), step=2)
        await self._move_l(joint_pos=model2_point2, tool=0, user=1, vel=100, acc=100)
        img1 = await self._capture(name=self.barcode + "_1") 
        self._set_stage(AppStage.vision_stage(3), step=3)
        await self._move_l(joint_pos= model2_point3, tool=0, user=1, vel=100, acc=100)
        img2 = await self._capture(name=self.barcode + "_2")
        self._set_stage(AppStage.vision_stage(4), step=4)
        await self._move_l(joint_pos=model2_point4, tool=0, user=1, vel=100, acc=100)
        img3 = await self._capture(name=self.barcode + "_3")
        await self._move_l(joint_pos=model2_point4_res,   tool=0, user=1, vel=100, acc=100)
        await self._move_l(joint_pos=model2_point4_res2, tool=0, user=1, vel=100, acc=100)
        self._set_stage(AppStage.vision_stage(5), step=5)
        await self._move_l(joint_pos=model2_point5,  tool=0, user=1, vel=100, acc=100)
        img3 = await self._capture(name=self.barcode + "_4")
        await self._move_l(joint_pos=model2_point4_res2, tool=0, user=1, vel=100, acc=100)
        await self._move_l(joint_pos=model2_point5_re2, tool=0, user=1, vel=100, acc=100)
        await self._move_j(joint_pos=homing,  tool=0, user=1, vel=100, acc=100)

        # Reporting — FIX: استخدام الصورة الملتقطة فعلياً (img0) بدل مسار hardcoded
        self._set_stage(AppStage.REPORTING)
        image_list = [p for p in [img0] if p]
        if not image_list:
            log.error(f"[program_2] Camera trigger failed — no image captured for barcode={self.barcode}")
            res    = "Error: no captured image"
            result = "error"
        else:
            res    = await self._ai_run(image_list)
            result = self.check_images_status(res)
        log.info(f"[program_2] Done — model answer — barcode={self.barcode}  result={res}")
        await asyncio.to_thread(ex.result_reporting, ID=self.barcode, result=result)

        # Update stats
        with self._state_lock:
            if result == "pass":
                self._stats["pass"]   += 1
            elif result == "fail":
                self._stats["fail"]   += 1
            else:
                self._stats["errors"] += 1

        # Signal robot — BUG-033: مفاتيح config صُحّحت (period بدل preriod)
        await self._set_do(self._cfg.get(key="test_done"),   1)
        await self._set_do(self._cfg.get(key="yellow_led"),  0)
        if result == "pass":
            await self._pulse_do(self._cfg.get(key="test_pass"),
                                 self._cfg.get("signal_pass_period", 0.5))
        elif result == "fail":
            await self._pulse_do(self._cfg.get(key="test_fail"),
                                 self._cfg.get("signal_fail_period", 0.5))

        self._set_stage(AppStage.DONE)
        log.info(f"[program_2] Done — barcode={self.barcode}  result={result}")

                

    async def program_3(self):
        log = _get_thread_logger()
        homing  = self.get_points_from_db("Homming")
        cam_parq= self.get_points_from_db("cam_parq")
        cam_relese= self.get_points_from_db("cam_relese")
        model3_prepoint1= self.get_points_from_db("model3_prepoint1")
        model3_point1 = self.get_points_from_db("model3_point1")
        model3_point2 = self.get_points_from_db("model3_point2")
        model3_point3 = self.get_points_from_db("model3_point3")
        model3_point4 = self.get_points_from_db("model3_point4")
        model3_point4_res = self.get_points_from_db("model3_point4_res")
        model3_point4_res2 = self.get_points_from_db("model3_point4_res2")
        model3_point5   = self.get_points_from_db("model3_point5")
        model3_point5_res   = self.get_points_from_db("model3_point5_res")
        model3_point6   = self.get_points_from_db("model3_point6")

        # BUG-010: asyncio.run() أُزيل — switch_camera أصبحت sync
        await self._move_j(joint_pos=homing,   tool=0, user=1, vel=60, acc=100)
        # await self._move_j(joint_pos=cam_parq, tool=0, user=1, vel=100, acc=100)
        # await self._move_j(joint_pos=cam_relese, tool=0, user=1, vel=100, acc=100)
        await self._move_j(joint_pos=model3_prepoint1, tool=0, user=1, vel=100, acc=100)
        # Vision test 1
        
        self._set_stage(AppStage.vision_stage(1), step=1)
        await self._move_l(joint_pos=model3_point1, tool=0, user=1, vel=60, acc=100)
        await self._sleep(1)
        img0 = await self._capture(name=self.barcode + "_0")   # BUG-012: نحفظ المسار الفعلي
        await self._sleep(1)
        await self._switch_camera()

        self._set_stage(AppStage.vision_stage(2), step=2)
        await self._move_l(joint_pos=model3_point2, tool=0, user=1, vel=100, acc=100)
        img1 = await self._capture(name=self.barcode + "_1") 
        self._set_stage(AppStage.vision_stage(3), step=3)
        await self._move_l(joint_pos= model3_point3, tool=0, user=1, vel=100, acc=100)
        img2 = await self._capture(name=self.barcode + "_2")
        self._set_stage(AppStage.vision_stage(4), step=4)
        await self._move_l(joint_pos=model3_point4, tool=0, user=1, vel=100, acc=100)
        img3 = await self._capture(name=self.barcode + "_3")
        await self._move_l(joint_pos=model3_point4_res,   tool=0, user=1, vel=100, acc=100)
        await self._move_l(joint_pos=model3_point4_res2, tool=0, user=1, vel=100, acc=100)
        self._set_stage(AppStage.vision_stage(5), step=5)
        await self._move_l(joint_pos=model3_point5,  tool=0, user=1, vel=100, acc=100)
        img3 = await self._capture(name=self.barcode + "_4")
        self._set_stage(AppStage.vision_stage(6), step=6)
        await self._move_l(joint_pos=model3_point6, tool=0, user=1, vel=100, acc=100)
        img3 = await self._capture(name=self.barcode + "_5")
        await self._move_l(joint_pos=model3_point5, tool=0, user=1, vel=100, acc=100)
        await self._move_l(joint_pos=model3_point4_res2, tool=0, user=1, vel=100, acc=100)
        await self._move_l(joint_pos=model3_point5_res, tool=0, user=1, vel=100, acc=100)
        await self._move_j(joint_pos=homing,  tool=0, user=1, vel=100, acc=100)

        # Reporting — FIX: استخدام الصورة الملتقطة فعلياً (img0) بدل مسار hardcoded
        self._set_stage(AppStage.REPORTING)
        image_list = [p for p in [img0] if p]
        if not image_list:
            log.error(f"[program_3] Camera trigger failed — no image captured for barcode={self.barcode}")
            res    = "Error: no captured image"
            result = "error"
        else:
            res    = await self._ai_run(image_list)
            result = self.check_images_status(res)
        log.info(f"[program_3] Done — model answer — barcode={self.barcode}  result={res}")
        await asyncio.to_thread(ex.result_reporting, ID=self.barcode, result=result)

        # Update stats
        with self._state_lock:
            if result == "pass":
                self._stats["pass"]   += 1
            elif result == "fail":
                self._stats["fail"]   += 1
            else:
                self._stats["errors"] += 1

        # Signal robot — BUG-033: مفاتيح config صُحّحت (period بدل preriod)
        await self._set_do(self._cfg.get(key="test_done"),   1)
        await self._set_do(self._cfg.get(key="yellow_led"),  0)
        if result == "pass":
            await self._pulse_do(self._cfg.get(key="test_pass"),
                                 self._cfg.get("signal_pass_period", 0.5))
        elif result == "fail":
            await self._pulse_do(self._cfg.get(key="test_fail"),
                                 self._cfg.get("signal_fail_period", 0.5))

        self._set_stage(AppStage.DONE)
        log.info(f"[program_3] Done — barcode={self.barcode}  result={result}")
        

    async def program_4(self):
        log = _get_thread_logger()
        homing  = self.get_points_from_db("Homming")
        cam_parq= self.get_points_from_db("cam_parq")
        cam_relese= self.get_points_from_db("cam_relese")
        model4_prepoint1= self.get_points_from_db("model4_prepoint1")
        model4_point1 = self.get_points_from_db("model4_point1")
        model4_point2 = self.get_points_from_db("model4_point2")
        model4_point3 = self.get_points_from_db("model4_point3")
        model4_point4 = self.get_points_from_db("model4_point4")
        model4_point4_res = self.get_points_from_db("model4_point4_res")
        model4_point4_res2 = self.get_points_from_db("model4_point4_res2")
        model4_point4_res3 = self.get_points_from_db("model4_point4_res3")
        model4_point4_res4 = self.get_points_from_db("model4_point4_res4")
        model4_point5   = self.get_points_from_db("model4_point5")

        # BUG-010: asyncio.run() أُزيل — switch_camera أصبحت sync
        await self._move_j(joint_pos=homing,   tool=0, user=1, vel=60, acc=100)
        # await self._move_j(joint_pos=cam_parq, tool=0, user=1, vel=100, acc=100)
        # await self._move_j(joint_pos=cam_relese, tool=0, user=1, vel=100, acc=100)
        await self._move_j(joint_pos=model4_prepoint1, tool=0, user=1, vel=100, acc=100)
        # Vision test 1
        
        self._set_stage(AppStage.vision_stage(1), step=1)
        await self._move_l(joint_pos=model4_point1, tool=0, user=1, vel=60, acc=100)
        await self._sleep(1)
        img0 = await self._capture(name=self.barcode + "_0")   # BUG-012: نحفظ المسار الفعلي
        self._set_stage(AppStage.vision_stage(2), step=2)
        await self._move_l(joint_pos=model4_point2, tool=0, user=1, vel=100, acc=100)
        img1 = await self._capture(name=self.barcode + "_1") 
        self._set_stage(AppStage.vision_stage(3), step=3)
        await self._move_l(joint_pos= model4_point3, tool=0, user=1, vel=100, acc=100)
        img2 = await self._capture(name=self.barcode + "_2")
        self._set_stage(AppStage.vision_stage(4), step=4)
        await self._move_l(joint_pos=model4_point4, tool=0, user=1, vel=100, acc=100)
        img3 = await self._capture(name=self.barcode + "_3")
        await self._move_l(joint_pos=model4_point4_res,   tool=0, user=1, vel=100, acc=100)
        await self._move_l(joint_pos=model4_point4_res2, tool=0, user=1, vel=100, acc=100)
        await self._move_l(joint_pos=model4_point4_res3, tool=0, user=1, vel=100, acc=100)
        await self._move_l(joint_pos=model4_point4_res4, tool=0, user=1, vel=100, acc=100)
        self._set_stage(AppStage.vision_stage(5), step=5)
        await self._move_l(joint_pos=model4_point5,  tool=0, user=1, vel=100, acc=100)
        img3 = await self._capture(name=self.barcode + "_4")
        await self._move_l(joint_pos=model4_point4_res4, tool=0, user=1, vel=100, acc=100)
        await self._move_l(joint_pos=model4_point4_res3, tool=0, user=1, vel=100, acc=100)
        await self._move_l(joint_pos=model4_point4_res2, tool=0, user=1, vel=100, acc=100)
        await self._move_j(joint_pos=model4_prepoint1, tool=0, user=1, vel=100, acc=100)
        await self._move_j(joint_pos=cam_relese, tool=0, user=1, vel=100, acc=100)
        await self._move_j(joint_pos=homing,  tool=0, user=1, vel=100, acc=100)

        # Reporting — FIX: استخدام الصورة الملتقطة فعلياً (img0) بدل مسار hardcoded
        self._set_stage(AppStage.REPORTING)
        image_list = [p for p in [img0] if p]
        if not image_list:
            log.error(f"[program_4] Camera trigger failed — no image captured for barcode={self.barcode}")
            res    = "Error: no captured image"
            result = "error"
        else:
            res    = await self._ai_run(image_list)
            result = self.check_images_status(res)
        log.info(f"[program_4] Done — model answer — barcode={self.barcode}  result={res}")
        await asyncio.to_thread(ex.result_reporting, ID=self.barcode, result=result)

        # Update stats
        with self._state_lock:
            if result == "pass":
                self._stats["pass"]   += 1
            elif result == "fail":
                self._stats["fail"]   += 1
            else:
                self._stats["errors"] += 1

        # Signal robot — BUG-033: مفاتيح config صُحّحت (period بدل preriod)
        await self._set_do(self._cfg.get(key="test_done"),   1)
        await self._set_do(self._cfg.get(key="yellow_led"),  0)
        if result == "pass":
            await self._pulse_do(self._cfg.get(key="test_pass"),
                                 self._cfg.get("signal_pass_period", 0.5))
        elif result == "fail":
            await self._pulse_do(self._cfg.get(key="test_fail"),
                                 self._cfg.get("signal_fail_period", 0.5))

        self._set_stage(AppStage.DONE)
        log.info(f"[program_4] Done — barcode={self.barcode}  result={result}")

    async def program_5(self):
        log = _get_thread_logger()
        homing  = self.get_points_from_db("Homming")
        cam_parq= self.get_points_from_db("cam_parq")
        cam_relese= self.get_points_from_db("cam_relese")
        model5_prepoint1= self.get_points_from_db("model5_prepoint1")
        model5_point1 = self.get_points_from_db("model5_point1")
        model5_point2 = self.get_points_from_db("model5_point2")
        model5_point2_res = self.get_points_from_db("model5_point2_res")
        model5_point3 = self.get_points_from_db("model5_point3")
        model5_point4 = self.get_points_from_db("model5_point4")
        model5_point4_res = self.get_points_from_db("model5_point4_res")
        model5_point4_res2 = self.get_points_from_db("model5_point4_res2")
        model5_point5 = self.get_points_from_db("model5_point5")
        model5_point6   = self.get_points_from_db("model5_point6")
        model5_point7   = self.get_points_from_db("model5_point7")


        # BUG-010: asyncio.run() أُزيل — switch_camera أصبحت sync
        await self._move_j(joint_pos=homing,   tool=0, user=1, vel=60, acc=100)
        # await self._move_j(joint_pos=cam_parq, tool=0, user=1, vel=100, acc=100)
        # await self._move_j(joint_pos=cam_relese, tool=0, user=1, vel=100, acc=100)
        await self._move_j(joint_pos=model5_prepoint1, tool=0, user=1, vel=100, acc=100)
        # Vision test 1
        
        self._set_stage(AppStage.vision_stage(1), step=1)
        await self._move_l(joint_pos=model5_point1, tool=0, user=1, vel=60, acc=100)
        await self._sleep(1)
        img0 = await self._capture(name=self.barcode + "_0")   # BUG-012: نحفظ المسار الفعلي
        self._set_stage(AppStage.vision_stage(2), step=2)
        await self._move_l(joint_pos=model5_point2, tool=0, user=1, vel=100, acc=100)
        img1 = await self._capture(name=self.barcode + "_1") 
        await self._move_l(joint_pos=model5_point2_res, tool=0, user=1, vel=100, acc=100)
        self._set_stage(AppStage.vision_stage(3), step=3)
        await self._move_l(joint_pos= model5_point3, tool=0, user=1, vel=100, acc=100)
        img2 = await self._capture(name=self.barcode + "_2")
        self._set_stage(AppStage.vision_stage(4), step=4)
        await self._move_l(joint_pos=model5_point4, tool=0, user=1, vel=100, acc=100)
        img3 = await self._capture(name=self.barcode + "_3")
        await self._move_l(joint_pos=model5_point4_res,   tool=0, user=1, vel=100, acc=100)
        await self._move_l(joint_pos=model5_point4_res2, tool=0, user=1, vel=100, acc=100)
        self._set_stage(AppStage.vision_stage(5), step=5)
        await self._move_l(joint_pos=model5_point5,  tool=0, user=1, vel=100, acc=100)
        img3 = await self._capture(name=self.barcode + "_4")
        self._set_stage(AppStage.vision_stage(6), step=6)
        await self._move_l(joint_pos=model5_point6, tool=0, user=1, vel=100, acc=100)
        img3 = await self._capture(name=self.barcode + "_5")
        self._set_stage(AppStage.vision_stage(7), step=6)
        await self._move_l(joint_pos=model5_point7, tool=0, user=1, vel=100, acc=100)
        img3 = await self._capture(name=self.barcode + "_6")
        await self._move_l(joint_pos=model5_point5, tool=0, user=1, vel=100, acc=100)
        await self._move_l(joint_pos=model5_point4_res2, tool=0, user=1, vel=100, acc=100)
        await self._move_j(joint_pos=model5_point4_res, tool=0, user=1, vel=100, acc=100)
        await self._move_j(joint_pos=model5_prepoint1, tool=0, user=1, vel=100, acc=100)
        await self._move_j(joint_pos=homing,  tool=0, user=1, vel=100, acc=100)

        # Reporting — FIX: استخدام الصورة الملتقطة فعلياً (img0) بدل مسار hardcoded
        self._set_stage(AppStage.REPORTING)
        image_list = [p for p in [img0] if p]
        if not image_list:
            log.error(f"[program_5] Camera trigger failed — no image captured for barcode={self.barcode}")
            res    = "Error: no captured image"
            result = "error"
        else:
            res    = await self._ai_run(image_list)
            result = self.check_images_status(res)
        log.info(f"[program_5] Done — model answer — barcode={self.barcode}  result={res}")
        await asyncio.to_thread(ex.result_reporting, ID=self.barcode, result=result)

        # Update stats
        with self._state_lock:
            if result == "pass":
                self._stats["pass"]   += 1
            elif result == "fail":
                self._stats["fail"]   += 1
            else:
                self._stats["errors"] += 1

        # Signal robot — BUG-033: مفاتيح config صُحّحت (period بدل preriod)
        await self._set_do(self._cfg.get(key="test_done"),   1)
        await self._set_do(self._cfg.get(key="yellow_led"),  0)
        if result == "pass":
            await self._pulse_do(self._cfg.get(key="test_pass"),
                                 self._cfg.get("signal_pass_period", 0.5))
        elif result == "fail":
            await self._pulse_do(self._cfg.get(key="test_fail"),
                                 self._cfg.get("signal_fail_period", 0.5))

        self._set_stage(AppStage.DONE)
        log.info(f"[program_5] Done — barcode={self.barcode}  result={result}")


    # ── Sequence ──────────────────────────────────────────────────────

    async def start_sequence(self):
        log = _get_thread_logger()
        log.info("enter the equance ")
        with self._state_lock:
            self._last_images = []   # صور الدورة الحالية
        # اتحرك لنقطة المسح — BUG-013: "CamScan" → "cam" (اسم موجود فعلاً في DB)
        # barcode_point = self.get_points_from_db("CamScan")
        # self.robot.MoveJ(barcode_point, 0, 1, vel=100, acc=100)

        # شغّل وضع القراءة
        # scan_mode = self._cfg.get(key="scan_mode")
        # if scan_mode == "camera":
            # camera_barcode.start(camera=self._camera)
        # elif scan_mode == "manual":
            # sc.start_listener()

        
        log.info(f"scanner is started")
        # انتظر الباركود
        self._set_stage(AppStage.IDLE)
        self.barcode = await self.get_barcode_from_scanner()

        if self._stop_app.is_set() or self.barcode is None:
            return   # البرنامج وقف

        # وقّف وضع القراءة
        # if scan_mode == "camera":
        #     camera_barcode.stop()
        # elif scan_mode == "manual":
        #     sc.stop_listener()

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
            await self.program_1()
        elif program == 3:
            await self.program_2()
        elif program == 2:
            await self.program_3()
        elif program == 4:
            await self.program_4()
        elif program == 5:
            await self.program_5()
        else:
            # BUG-014: programs 2-5 فارغة → ERROR مع رسالة واضحة
            log.warning(f"[Sequence] Program {program} has no implementation (programs 2-5 are empty stubs)")
            self._set_stage(AppStage.ERROR)

        self._save_session_stats()

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

    # ══════════════════════════════════════════════════════════════════
    #  اللوب الرئيسي (asyncio task — مش thread)
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _reset_rpc_class_state():
        """
        يرجّع متغيرات RPC الـ **class-level** لقيمها الأصلية قبل أي اتصال جديد.

        ليه ده ضروري؟
        الـ SDK حاطط الأعلام دي على الـ class مش على الـ instance، وعامل
        decorator على كل دالة بيقول:

            if RPC.is_conect == False: return -4   # مابينفّذش حاجة

        و`is_conect` بتتظبط False لو الاتصال فشل في __init__، و**عمرها ما
        بترجع True** (لا في CloseRPC ولا في أي مكان). فلو Start واحدة بس
        فشلت (الروبوت مقفول / كابل مفصول / IP غلط)، كل Start بعد كده —
        مهما الروبوت رجع — بتعمل RPC جديد بس كل أمر يرجع -4 من غير تنفيذ:
        MoveJ مابتحركش، و GetDI ترجع -4 فـ DI0 عمره ما يساوي 1 فالسيكوانس
        عمرها ما تبدأ. والنتيجة: البرنامج ميت وبيقول إنه شغال، والحل الوحيد
        كان إعادة تشغيل العملية.

        بنصفّرهم هنا عشان Stop/Start يبقى مكافئ لإعادة تشغيل البرنامج فعلاً.
        """
        log = _get_thread_logger()
        before = getattr(RPC, "is_conect", None)
        RPC.is_conect      = True
        RPC.SDK_state      = True
        RPC.closeRPC_state = False
        RPC.reconnect_flag = False
        RPC.reconnect_lock = False
        if before is False:
            log.warning("[App] RPC.is_conect كانت False من محاولة سابقة — اترجعت True")

    async def _connect_hardware(self):
        """تشغيل الكاميرا + الاتصال بالروبوت. بيرفع Exception لو فشل."""
        log = _get_thread_logger()

        # ── الكاميرا ───────────────────────────────────────────────────
        await asyncio.to_thread(self._camera.start)
        ok = await asyncio.to_thread(self._camera.wait_for_frame, 5.0)
        if not ok:
            raise RuntimeError("الكاميرا مش بتبعت فريمات — اتأكد من التوصيل والـ index")
        await asyncio.to_thread(ct.start, 0, ct.DEFAULT_SAVE_DIR, 8.0, self._camera)

        # ── الروبوت ────────────────────────────────────────────────────
        self._reset_rpc_class_state()
        self.robot = await asyncio.to_thread(RPC, self.robot_ip)

        # الـ SDK مابيرفعش exception لو الاتصال فشل — بيظبط is_conect=False
        # ويخلي كل أمر يرجع -4 بصمت. فبنتشيّك هنا ونفشّل الـ Start برسالة
        # واضحة بدل ما البرنامج يقول "شغال" وهو مش بيحرك الكوبوت.
        if getattr(RPC, "is_conect", True) is False:
            raise RuntimeError(
                f"مفيش اتصال بالكوبوت على {self.robot_ip} — اتأكد من الباور "
                f"والشبكة والـ IP في الإعدادات. (الـ SDK ظبط is_conect=False)"
            )
        log.info(f"[App] RPC connected → {self.robot_ip}")

        homing = await asyncio.to_thread(self.get_points_from_db, "Homming")
        await self._move_j(joint_pos=homing, tool=0, user=1, vel=100, acc=100)
        await self._set_do(self._cfg.get(key="test_done"), 1)

    async def _read_di(self, default=0) -> int:
        """قراءة DI الترجر وتطبيع الشكل (int أو list/tuple)."""
        ret = await self._call(self.robot.GetDI, self._cfg.get(key="input_trigger"), 0)
        if isinstance(ret, (list, tuple)):
            return int(ret[1]) if len(ret) > 1 else int(ret[0])
        return int(ret) if ret is not None else default

    async def run_async(self):
        """
        اللوب الرئيسي — بيشتغل كـ asyncio.Task جوه نفس الـ event loop
        بتاع uvicorn. كل نداء هاردوير بلوكينج بيروح thread executor عن
        طريق self._call() اللي بيحط نقطة توقف قبله وبعده.

        الخروج بيحصل بواحد من تلاتة:
          1. _stop_app اتظبطت  → StopRequested من أقرب نقطة توقف
          2. الـ task اتعمله cancel → CancelledError
          3. Exception حقيقي    → stage = ERROR
        التنضيف مش هنا — هو في aclose() عشان يبقى مضمون ومرة واحدة.
        """
        log = _get_thread_logger()
        log.info("[App] run_async started")
        try:
            # الهاردوير اتوصل بالفعل في start() قبل ما التاسك دي تتعمل،
            # فلو وصلنا هنا يبقى الكاميرا والروبوت جاهزين.

            # BUG-FIX: ناخد قراءة أولية حقيقية من DI0 بدل last = 0 ثابت،
            # عشان لو DI0 كانت أصلاً 1 وقت الـ Start ماتتفسرش غلط كأنها
            # positive edge (0→1) وتشغّل السيكوانس فورًا بدون إشارة حقيقية.
            try:
                last = await self._read_di()
            except StopRequested:
                raise
            except Exception as e:
                log.warning(f"[App] Initial GetDI read failed: {e} — defaulting last=0")
                last = 0
            log.info(f"[App] Initial DI0 state = {last} (last synced to avoid false trigger on start)")

            self._set_stage(AppStage.IDLE)
            log.info("[App] Ready — waiting for trigger DI0")

            while not self._stop_app.is_set():
                try:
                    DI0 = await self._read_di()
                except StopRequested:
                    raise
                except Exception as e:
                    log.warning(f"[App] GetDI error: {e} — retrying...")
                    await self._sleep(1.0)
                    continue

                if DI0 == 1 and last == 0:
                    log.info("[App] DI0 HIGH — starting sequence")
                    await self._set_do(self._cfg.get(key="test_done"), 0)
                    await self._set_do(self._cfg.get(key="yellow_led"), 1)
                    await self.start_sequence()
                    if not self._stop_app.is_set():
                        self._set_stage(AppStage.IDLE)
                        await self._set_do(self._cfg.get(key="test_done"), 1)

                last = DI0
                await self._sleep(0.1)

        except StopRequested as e:
            # خروج طبيعي — المستخدم دوس Stop
            log.info(f"[App] run_async stopped by request ({e})")
        except asyncio.CancelledError:
            log.info("[App] run_async cancelled")
            raise
        except Exception as e:
            log.exception(f"[App] run_async error: {e}")
            self._set_stage(AppStage.ERROR)
        finally:
            self._running = False
            self._save_session_stats()
            log.info("[App] run_async finished")

    # ══════════════════════════════════════════════════════════════════
    #  start / stop / teardown
    # ══════════════════════════════════════════════════════════════════

    async def start(self, camera_index=None) -> bool:
        """
        يوصّل الهاردوير (كاميرا + روبوت + homing) **ثم** يشغّل اللوب
        الرئيسي كـ asyncio.Task.

        مهم: التهيئة بتحصل قبل الرجوع، فلو الدالة رجعت True يبقى
        الكاميرا والروبوت شغالين بجد — ولو الكاميرا مش بتبعت فريمات أو
        الروبوت مش راد، بترفع Exception والـ Start بتفشل برسالة واضحة
        بدل ما تنجح شكليًا والبرنامج يروح على ERROR بعدها.
        الإحصائيات بتتحمل من الجلسة السابقة.

        ملحوظة: الـ App ده بيتعمل مرة واحدة ويتستخدم مرة واحدة —
        الـ lifecycle manager (lifecycle.py) بيبني App جديد كل Start،
        فمفيش حالة "ثريد قديم لسه بيوقف" من الأساس.
        """
        if self._running:
            return True
        if self._closed:
            raise RuntimeError("App instance already closed — اعمل instance جديد")

        if camera_index is not None:
            self._camera._cam_index = camera_index

        saved = self._load_session_stats()

        self._stop_app.clear()
        self._stop_reason = None
        self._start_time = time.time()
        with self._state_lock:
            self._stage   = AppStage.IDLE
            # BUG-050: persistent stats — مش بنصفّر، بنرجع من الجلسة السابقة
            self._stats   = saved["stats"]
            self.barcode  = saved["last_barcode"]
            self._program = None
            self._step    = 0
            self._last_images = []

        # BUG-050: مسح queue الباركودات القديمة
        sc.reset_queue()
        sc.start_listener()

        self._running = True
        try:
            # التهيئة هنا (مش جوه التاسك) عشان فشلها يبان كـ فشل Start
            await self._connect_hardware()
        except BaseException:
            self._running = False
            raise

        self._task = asyncio.create_task(self.run_async(), name="app-main")
        return True

    def request_stop(self, reason: str = "user stop"):
        """
        إيقاف فوري وغير بلوكينج (آمن للنداء من أي thread أو من الـ event loop).

        خطوتين مهمتين:
          1. _stop_app.set()  → أقرب نقطة توقف هتخرج من السيكوانس
          2. StopMotion()     → بتقطع الحركة الحالية فورًا. ضروري لأن
             MoveJ/MoveL نداء xmlrpc بلوكينج بيستنى الحركة تخلص، وأسيو
             مش بيقدر يلغي thread — فلازم نقول للكنترولر نفسه "قف".
        """
        self._stop_reason = reason
        self._running = False     # فورًا عشان الـ UI/الـ snapshot يبانوا صح
        self._stop_app.set()
        self._emergency_stop_motion()

    def _emergency_stop_motion(self):
        """
        StopMotion على **ServerProxy منفصل** — مش self.robot.

        ليه منفصل؟ xmlrpc.client.ServerProxy بيكاش connection واحدة
        وهي **مش thread-safe**. لو الـ MoveL لسه في الهوا على نفس الـ
        proxy وبعتنا StopMotion عليه من thread تاني، الـ HTTP connection
        بتتلخبط. proxy جديد = connection جديدة = آمن.
        """
        log = _get_thread_logger()
        if self.robot is None:
            return
        try:
            proxy = xmlrpc.client.ServerProxy(
                f"http://{self.robot_ip}:20003", allow_none=True
            )
            socket.setdefaulttimeout(3)
            try:
                proxy.StopMotion()
                log.info("[App] StopMotion أُرسل — الحركة اتقطعت")
            finally:
                socket.setdefaulttimeout(None)
        except Exception as e:
            log.warning(f"[App] StopMotion فشل: {e}")

    async def aclose(self, timeout: float = 12.0):
        """
        التنضيف الكامل — بعده الـ instance ده مابيتستخدمش تاني.

        بيقفل كل حاجة بالترتيب الصح، وكل خطوة في try خاصة بيها عشان
        فشل خطوة ماتمنعش اللي بعدها:
          1. إيقاف اللوب + StopMotion
          2. انتظار التاسك (بـ timeout) ثم cancel لو عندت
          3. capture_trigger → camera_barcode → الكاميرا نفسها
          4. سكانر الباركود
          5. robot.CloseRPC()  ← ده اللي كان ناقص خالص قبل كده
        """
        log = _get_thread_logger()
        if self._closed:
            return
        self._closed = True

        # 1) اطلب الإيقاف
        self.request_stop("aclose")

        # 2) استنى اللوب يخرج، وبعدين اقطعه بالعافية
        task, self._task = self._task, None
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning(f"[App] اللوب مخلصش في {timeout}s — cancel")
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    log.warning("[App] اللوب لسه معلّق في نداء هاردوير — بنكمّل التنضيف")
                except Exception:
                    pass
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.warning(f"[App] اللوب خرج بـ exception: {e}")

        # 3) الكاميرا وكل اللي معتمد عليها
        for label, fn in (
            ("capture_trigger", ct.stop),
            ("camera_barcode",  camera_barcode.stop),
            ("camera",          self._camera.stop),
        ):
            try:
                await asyncio.to_thread(fn)
                log.info(f"[App] {label} اتقفل")
            except Exception as e:
                log.warning(f"[App] إيقاف {label} فشل: {e}")

        # 4) سكانر الباركود
        try:
            await asyncio.to_thread(sc.stop_listener)
            log.info("[App] scanner listener اتقفل")
        except Exception as e:
            log.warning(f"[App] إيقاف الـ scanner فشل: {e}")

        # 5) الروبوت — CloseRPC بيقفل سوكيت 20004 وبيوقف
        #    robot_state_routine_thread. من غيره كل Start كانت بتسيب
        #    ثريد + سوكيت شغالين للأبد.
        robot, self.robot = self.robot, None
        if robot is not None:
            try:
                await asyncio.to_thread(robot.CloseRPC)
                log.info("[App] robot CloseRPC تم")
            except Exception as e:
                log.warning(f"[App] CloseRPC فشل: {e}")

        # 6) موديل الـ AI + الـ VRAM
        #    كل Start بتحمّل موديل جديد على الـ GPU. من غير تنضيف صريح،
        #    الـ allocator بتاع torch بيمسك البلوكات المحررة في الكاش
        #    والـ VRAM بتزحف مع كل دورة start/stop.
        await asyncio.to_thread(self._release_ai_provider)

        # 7) حالة نهائية نظيفة
        self._running = False
        self._save_session_stats()
        self._set_stage(AppStage.IDLE)
        log.info("[App] aclose — التنضيف خلص، كل الهاردوير اتقفل")

    def _release_ai_provider(self):
        """يفضي موديل الـ AI ويرجّع الـ VRAM. آمن لو مفيش torch خالص."""
        log = _get_thread_logger()
        provider, self._ai_provider = self._ai_provider, None
        if provider is None:
            return
        # امسح أي reference للموديل جوه الـ provider قبل الـ GC
        for attr in ("model", "_model", "processor", "_processor",
                     "tokenizer", "_tokenizer"):
            if hasattr(provider, attr):
                try:
                    setattr(provider, attr, None)
                except Exception:
                    pass
        del provider
        try:
            import gc
            gc.collect()
        except Exception:
            pass
        try:
            import sys
            torch = sys.modules.get("torch")   # مش بنعمل import لو مش محمّل
            if torch is not None and torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
                log.info("[App] CUDA cache اتفضى")
        except Exception as e:
            log.warning(f"[App] تنضيف الـ CUDA فشل: {e}")

    # ── توافق مع الكود القديم ─────────────────────────────────────────

    def stop(self, wait: bool = False, timeout: float = 10.0):
        """
        DEPRECATED — متبقية للتوافق مع أي كود قديم (سكربتات/tests).
        الإيقاف الكامل الصح هو: await app.aclose().
        """
        self.request_stop("legacy stop()")

    def run(self):
        return self.start()


if __name__ == "__main__":
    # وضع CLI — بيشغّل نفس اللوب جوه event loop صغير
    async def _main():
        app = App()
        await app.start()
        try:
            while app.is_running:
                await asyncio.sleep(0.5)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await app.aclose()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
