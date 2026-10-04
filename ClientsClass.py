"""
ClientsClass.py
---------------
الـ controller بتاع محطة الفحص — نسخة asyncio.

التصميم:
    App      → عايش طول عمر السيرفر. شايل الإحصائيات وحالة التشغيل بس.
    Session  → بتتعمل جديدة مع كل Start وبتتقفل بالكامل مع كل Stop
               (كاميرا + كوبوت + AI + سكانر). كأنك قفلت البرنامج وفتحته تاني.

    Start ─► Session(config من الأول) ─► open() ─► run()   [asyncio Task واحد]
    Stop  ─► StopMotion ─► task.cancel() ─► finally: session.close() ─► STOPPED

كل الـ I/O الـ blocking (Fairino SDK, pyusb, AI, Excel, cv2) بيتنفذ في
asyncio.to_thread عشان الـ event loop (والـ dashboard) ما يقفش أبداً.
"""

import asyncio
import json
import os
import queue
import sqlite3
import threading
import time

import pandas as pd

import scanner as sc
import excel as ex
import capture_trigger as ct
import camera_barcode
from camera_hub import CameraHub
from robot_link import RobotLink
from ai_vision import WaterDetector
from thread_logger import get_logger as _get_thread_logger
from config import config as _cfg_singleton    # BUG-008: استخدام singleton
from config import data_path as _data_path

# ── مسار ملف الإحصائيات الدائمة (جوه DATA_DIR عشان يتحفظ في الـ Volume) ─────
_SESSION_STATS_FILE = _data_path("session_stats.json")


class RunState:
    """حالة دورة حياة البرنامج (غير الـ stage بتاع الـ sequence)."""
    STOPPED  = "STOPPED"
    STARTING = "STARTING"
    RUNNING  = "RUNNING"
    STOPPING = "STOPPING"


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




# ════════════════════════════════════════════════════════════════════════════
#  Session — كل الهاردوير والـ tasks بتوع تشغيل واحد (Start → Stop)
# ════════════════════════════════════════════════════════════════════════════

class Session:
    """
    بتتعمل مع كل Start. بتقرأ الـ config من الأول، وبتفتح الكاميرا والكوبوت
    والـ AI. close() بتقفل كل حاجة فتحتها — حتى لو open() وقف في النص.
    """

    def __init__(self, app: "App"):
        self.app = app
        self.log = _get_thread_logger()
        self.cfg = _cfg_singleton
        self.cfg.load()                         # اقرأ config.json من الأول

        self.camera: CameraHub | None = None
        self.robot:  RobotLink  | None = None
        self.ai:     WaterDetector | None = None
        self._barcode_task: asyncio.Task | None = None
        self._kb_listener = False
        self.barcode: str | None = None

    # ── Builders ──────────────────────────────────────────────────────────

    def _build_camera(self) -> CameraHub:
        cam_type  = self.cfg.get("camera_type",  "useeplus")
        cam_index = int(self.cfg.get("camera_index", 0))
        if cam_type == "opencv":
            return CameraHub.OpenCV(camera_index=cam_index)
        return CameraHub.UseePlus(camera_index=cam_index)

    def _build_ai_provider(self) -> WaterDetector:
        agent   = self.cfg.get("AI_Agent")
        model   = self.cfg.get("ai_model")
        enhance = self.cfg.get("ai_enhancement", False)
        if agent == "groq":
            return WaterDetector.Groq(model=model, use_enhancement=enhance)
        elif agent == "local_ollama":
            return WaterDetector.Local(model=model, backend="ollama",    use_enhancement=enhance)
        elif agent == "local_lmstudio":
            return WaterDetector.Local(model=model, backend="lm_studio", use_enhancement=enhance)
        return WaterDetector.Gemini(model=model, use_enhancement=enhance)

    # ── Open / Close ──────────────────────────────────────────────────────

    async def open(self):
        log = self.log

        # 1) الكاميرا — BUG-037: لازم يجي فريم وإلا نوقف
        self.camera = self._build_camera()
        if not await self.camera.astart(timeout=5.0):
            raise RuntimeError("Camera failed to produce frames")

        # 2) الـ AI provider (ممكن يعمل import تقيل → thread)
        self.ai = await asyncio.to_thread(self._build_ai_provider)

        # 3) الكوبوت
        self.robot = RobotLink(self.cfg.get("cobot_ip"))
        await self.robot.connect()

        log.info("[Session] opened — camera + robot + AI ready")

    def abort_motion(self):
        """sync — بيتنده من stop() قبل الـ cancel عشان MoveJ يرجع فوراً."""
        if self.robot is not None and self.robot.connected:
            self.robot.stop_motion_now()

    async def close(self):
        """
        بتقفل كل حاجة بالترتيب العكسي. كل خطوة لوحدها في try
        عشان فشل خطوة ما يمنعش اللي بعدها.
        """
        log = self.log
        log.info("[Session] closing ...")

        await self._stop_barcode_reader()

        if self.robot is not None:
            try:
                await self.robot.close()
            except Exception as e:
                log.error(f"[Session] robot close error: {e}")

        if self.camera is not None:
            try:
                ok = await self.camera.astop()
                if not ok:
                    log.error("[Session] camera thread did not exit — USB may still be held")
            except Exception as e:
                log.error(f"[Session] camera close error: {e}")

        ct.release()
        sc.reset_queue()
        self.robot = None
        self.camera = None
        self.ai = None
        log.info("[Session] closed")

    # ── Barcode reader ────────────────────────────────────────────────────

    def _start_barcode_reader(self):
        scan_mode = self.cfg.get("scan_mode")
        if scan_mode == "camera":
            self._barcode_task = asyncio.create_task(
                camera_barcode.run(self.camera), name="camera-barcode"
            )
        elif scan_mode == "manual":
            sc.start_listener()
            self._kb_listener = True

    async def _stop_barcode_reader(self):
        if self._kb_listener:
            try:
                sc.stop_listener()
            except Exception:
                pass
            self._kb_listener = False
        t, self._barcode_task = self._barcode_task, None
        if t is not None and not t.done():
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)

    @property
    def scanner_active(self) -> bool:
        return (self._barcode_task is not None and not self._barcode_task.done()) \
               or self._kb_listener

    async def wait_barcode(self) -> str:
        """ينتظر باركود من scanner.queue_barcode — بيتلغي بالـ cancel."""
        while True:
            try:
                barcode = sc.queue_barcode.get_nowait()
                sc.queue_barcode.task_done()
                return barcode
            except queue.Empty:
                await asyncio.sleep(0.05)

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _get_points_blocking(names) -> dict:
        # BUG-003: parameterized query — Docker: web_point.db جوه DATA_DIR
        conn = sqlite3.connect(_data_path("web_point.db"))
        try:
            cur = conn.cursor()
            out = {}
            for name in names:
                cur.execute("SELECT j1, j2, j3, j4, j5, j6 FROM points WHERE name = ?", (name,))
                row = cur.fetchone()
                if row:
                    out[name] = [float(x) for x in row]
                else:
                    print(f"النقطة '{name}' مش موجودة في الداتا بيز.")
                    out[name] = [0.0] * 6
            return out
        finally:
            conn.close()

    async def get_points(self, *names) -> dict:
        return await asyncio.to_thread(self._get_points_blocking, names)

    async def switch_camera(self):
        do = self.cfg.get(key="Switch_camera")
        await self.robot.set_do(do, 1)
        await asyncio.sleep(3)
        await self.robot.set_do(do, 0)

    async def capture(self, name: str) -> str | None:
        return await asyncio.to_thread(ct.capture, self.camera, name)

    @staticmethod
    def check_images_status(images_data):
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
        for _name, value in images_data.items():
            if str(value).strip().lower() == "yes":
                return "fail"
        return "pass"

    # ── Programs ──────────────────────────────────────────────────────────

    async def program_1(self):
        app, robot, log = self.app, self.robot, self.log
        pts = await self.get_points("water1", "10kg_1", "10kg_2", "10kg_3", "ready")
        homing, ready = pts["water1"], pts["ready"]

        await self.switch_camera()
        await robot.move_j(ready)

        # Vision test 1
        app._set_stage(AppStage.vision_stage(0), step=1)
        await robot.move_j(pts["10kg_1"])
        img0 = await self.capture(self.barcode + "_0")   # BUG-012: المسار الفعلي
        await asyncio.sleep(1)

        # Vision test 2
        app._set_stage(AppStage.vision_stage(1), step=2)
        await robot.move_j(pts["10kg_2"])
        img1 = await self.capture(self.barcode + "_1")
        await asyncio.sleep(1)

        # Vision test 3
        await self.switch_camera()
        app._set_stage(AppStage.vision_stage(2), step=3)
        await robot.move_j(ready)
        await robot.move_j(pts["10kg_3"])
        img2 = await self.capture(self.barcode + "_2")
        await asyncio.sleep(1)

        # Return home
        await robot.move_j(homing)

        # Reporting
        app._set_stage(AppStage.REPORTING)
        image_list = [p for p in (img0, img1, img2) if p is not None]
        ai_raw = await asyncio.to_thread(self.ai.run, image_paths=image_list)
        result = self.check_images_status(ai_raw)
        await asyncio.to_thread(ex.result_reporting, ID=self.barcode, result=result)
        app._count_result(result)

        # Signal robot — BUG-033: مفاتيح config صُحّحت (period بدل preriod)
        cfg = self.cfg
        await robot.set_do(cfg.get(key="test_done"),  1)
        await robot.set_do(cfg.get(key="yellow_led"), 0)
        if result == "pass":
            await robot.set_do(cfg.get(key="test_pass"), 1)
            await asyncio.sleep(float(cfg.get("signal_pass_period", 0.5)))
            await robot.set_do(cfg.get(key="test_pass"), 0)
        elif result == "fail":
            await robot.set_do(cfg.get(key="test_fail"), 1)
            await asyncio.sleep(float(cfg.get("signal_fail_period", 0.5)))
            await robot.set_do(cfg.get(key="test_fail"), 0)

        app._set_stage(AppStage.DONE)
        log.info(f"[program_1] Done — barcode={self.barcode}  result={result}")

    async def program_2(self): pass
    async def program_3(self): pass
    async def program_4(self): pass
    async def program_5(self): pass

    # ── Sequence ──────────────────────────────────────────────────────────

    async def start_sequence(self):
        app, log = self.app, self.log

        # اتحرك لنقطة المسح — BUG-013: "cam" (اسم موجود فعلاً في DB)
        #pts = await self.get_points("cam")
        #await self.robot.move_j(pts["cam"])

        # اقرأ الباركود
        app._set_stage(AppStage.IDLE)
        self._start_barcode_reader()
        try:
            self.barcode = await self.wait_barcode()
        finally:
            await self._stop_barcode_reader()
        app._set_barcode(self.barcode)

        log.info(f"[Sequence] Barcode: {self.barcode}")
        app._set_stage(AppStage.BARCODE_RECEIVED)

        # بحث عن البرنامج — BUG-014: None عند الخطأ
        app._set_stage(AppStage.PROGRAM_LOOKUP)
        program = await asyncio.to_thread(app.determine_program_from_barcode, self.barcode)
        if program is None:
            log.error(f"[Sequence] Could not determine program for barcode: {self.barcode!r}")
            app._set_stage(AppStage.ERROR)
            return
        app._set_program(program)

        app._set_stage(AppStage.SENDING_PROGRAM)
        log.info(f"[Sequence] Program: {program}")

        programs = {1: self.program_1, 2: self.program_2, 3: self.program_3,
                    4: self.program_4, 5: self.program_5}
        fn = programs.get(program)
        if fn is None:
            log.warning(f"[Sequence] Program {program} has no implementation")
            app._set_stage(AppStage.ERROR)
            return
        await fn()

    # ── Main loop ─────────────────────────────────────────────────────────

    async def run(self):
        app, robot, cfg, log = self.app, self.robot, self.cfg, self.log

        pts = await self.get_points("water1")
        await robot.move_j(pts["water1"])
        await robot.set_do(cfg.get(key="test_done"), 1)

        last = robot.get_di(cfg.get(key="input_trigger"))
        app._set_stage(AppStage.IDLE)
        log.info("[App] Ready — waiting for trigger DI0")

        while True:
            try:
                di = await robot.get_di(cfg.get(key="input_trigger"))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"[App] GetDI error: {e} — retrying...")
                await asyncio.sleep(1.0)
                continue

            if di == 1 and last == 0:
                log.info("[App] DI0 HIGH — starting sequence")
                await robot.set_do(cfg.get(key="test_done"), 0)
                await robot.set_do(cfg.get(key="yellow_led"), 1)
                await self.start_sequence()
                app._set_stage(AppStage.IDLE)
                await robot.set_do(cfg.get(key="test_done"), 1)

            last = di
            await asyncio.sleep(0.1)


# ════════════════════════════════════════════════════════════════════════════
#  App — عايش طول عمر السيرفر
# ════════════════════════════════════════════════════════════════════════════

class App:
    STOP_TIMEOUT = 20.0     # أقصى وقت لقفل الـ session (StopMotion + كاميرا + كوبوت)

    def __init__(self):
        self._cfg = _cfg_singleton
        self._mapping_cache_df    = None
        self._mapping_cache_path  = None
        self._mapping_cache_mtime = None

        self._run_state  = RunState.STOPPED
        self._session: Session | None = None
        self._task: asyncio.Task | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._last_error: str | None = None

        # ── Web state tracking ─────────────────────────────────────────
        self.barcode          = None
        self._stage           = AppStage.IDLE
        self._program         = None
        self._step            = 0
        self._stats           = {"total": 0, "pass": 0, "fail": 0, "errors": 0}
        self._last_event_time = None
        self._start_time      = None
        self._state_lock      = threading.Lock()

    # ── Session stats persistence ──────────────────────────────────────

    def _load_session_stats(self) -> dict:
        """تحميل الإحصائيات المحفوظة من الجلسة السابقة (stop/start)."""
        try:
            if os.path.exists(_SESSION_STATS_FILE):
                with open(_SESSION_STATS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                stats = {k: int(data.get(k, 0)) for k in ("total", "pass", "fail", "errors")}
                return {"stats": stats, "last_barcode": data.get("last_barcode")}
        except Exception:
            pass
        return {"stats": {"total": 0, "pass": 0, "fail": 0, "errors": 0}, "last_barcode": None}

    def _save_session_stats(self):
        try:
            with self._state_lock:
                data = dict(self._stats)
                data["last_barcode"] = self.barcode
            with open(_SESSION_STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    def reset_session_stats(self):
        with self._state_lock:
            self._stats  = {"total": 0, "pass": 0, "fail": 0, "errors": 0}
            self.barcode = None
        try:
            if os.path.exists(_SESSION_STATS_FILE):
                os.remove(_SESSION_STATS_FILE)
        except Exception:
            pass

    # ── State helpers (بتتنده من الـ session) ─────────────────────────

    @property
    def is_running(self) -> bool:
        """True من أول ما تدوس Start لحد ما الـ teardown يخلص."""
        return self._run_state != RunState.STOPPED

    @property
    def run_state(self) -> str:
        return self._run_state

    @property
    def camera(self) -> CameraHub | None:
        s = self._session
        return s.camera if s is not None else None

    def _set_stage(self, stage: str, step: int = 0):
        with self._state_lock:
            self._stage = stage
            self._step  = step
            self._last_event_time = time.time()

    def _set_barcode(self, barcode):
        with self._state_lock:
            self.barcode = barcode

    def _set_program(self, program):
        with self._state_lock:
            self._program = program
            self._stats["total"] += 1

    def _count_result(self, result: str):
        with self._state_lock:
            if result == "pass":
                self._stats["pass"] += 1
            elif result == "fail":
                self._stats["fail"] += 1
            else:
                self._stats["errors"] += 1

    def get_state_snapshot(self) -> dict:
        """snapshot للـ dashboard — رخيصة ومش blocking، آمنة من أي مكان."""
        s = self._session
        camera_ok  = bool(s and s.camera and s.camera.is_running())
        robot_ok   = bool(s and s.robot and s.robot.connected)
        scanner_ok = bool(s and s.scanner_active)
        ai_ok      = bool(s and s.ai is not None)

        with self._state_lock:
            running = self._run_state == RunState.RUNNING
            uptime  = (time.time() - self._start_time) if (self._start_time and running) else 0
            return {
                "is_running":        self.is_running,
                "run_state":         self._run_state,
                "last_error":        self._last_error,
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
                    "ai":      ai_ok,
                    "scanner": scanner_ok,
                },
            }

    # ── Program mapping ────────────────────────────────────────────────

    def determine_program_from_barcode(self, barcode, excel_file_path=None):
        """BUG-014: بترجع None عند الخطأ. blocking (pandas) → to_thread."""
        log = _get_thread_logger()

        if excel_file_path is None:
            fname = self._cfg.get("program_mapping_file", "program_mapping.xlsx")
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
            log.error(f"[Program] Char '{target_char}' not found in mapping file")
            return None
        except FileNotFoundError:
            log.error(f"[Program] Excel file not found: {excel_file_path}")
            return None
        except Exception as e:
            log.exception(f"[Program] Unexpected error: {e}")
            return None

    # ── Session task ───────────────────────────────────────────────────

    async def _session_main(self, session: Session):
        log = _get_thread_logger()
        log.info("[App] session started")
        try:
            await session.open()
            with self._state_lock:
                self._run_state  = RunState.RUNNING
                self._start_time = time.time()
            await session.run()
        except asyncio.CancelledError:
            log.info("[App] session cancelled (Stop)")
        except Exception as e:
            log.exception(f"[App] session error: {e}")
            self._last_error = str(e)
            self._set_stage(AppStage.ERROR)
        finally:
            with self._state_lock:
                if self._run_state != RunState.STOPPING:
                    self._run_state = RunState.STOPPING
            try:
                await session.close()
            except Exception as e:
                log.exception(f"[App] session close error: {e}")
            self._save_session_stats()
            with self._state_lock:
                self._session    = None
                self._run_state  = RunState.STOPPED
                self._start_time = None
            log.info("[App] session finished — everything closed")

    # ── start / stop (async) ──────────────────────────────────────────

    async def start(self) -> bool:
        """
        يبدأ session جديدة ويرجع فوراً (الفتح بيكمل في الخلفية — تابع run_state).
        يرجع False لو لسه في session بتقفل.
        """
        async with self._lifecycle_lock:
            if self._run_state in (RunState.STARTING, RunState.RUNNING):
                return True
            if self._run_state == RunState.STOPPING or (self._task and not self._task.done()):
                return False

            saved = self._load_session_stats()
            with self._state_lock:
                self._stage      = AppStage.IDLE
                self._stats      = saved["stats"]       # BUG-050: persistent stats
                self.barcode     = saved["last_barcode"]
                self._program    = None
                self._step       = 0
                self._last_error = None
                self._run_state  = RunState.STARTING
            sc.reset_queue()                             # BUG-050

            try:
                self._session = Session(self)
            except Exception as e:
                with self._state_lock:
                    self._run_state  = RunState.STOPPED
                    self._last_error = str(e)
                raise
            self._task = asyncio.create_task(
                self._session_main(self._session), name="app-session"
            )
            return True

    async def stop(self, timeout: float | None = None) -> bool:
        """
        يوقف كل حاجة ويستنى لحد ما الكاميرا والكوبوت يتقفلوا فعلاً.
        يرجع True لما يبقى STOPPED.
        """
        timeout = self.STOP_TIMEOUT if timeout is None else timeout
        log = _get_thread_logger()
        async with self._lifecycle_lock:
            task, session = self._task, self._session
            if task is None or task.done():
                return True

            with self._state_lock:
                self._run_state = RunState.STOPPING
            log.info("[App] Stop requested")

            # 1) وقّف حركة الكوبوت فوراً (MoveJ blocking هترجع)
            if session is not None:
                await asyncio.to_thread(session.abort_motion)

            # 2) الغي الـ task — finally بتاعه بيقفل الـ session
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if not done:
                log.error(f"[App] session did not close within {timeout}s")
                return False
            self._task = None
            return True


if __name__ == "__main__":
    async def _cli():
        app = App()
        await app.start()
        try:
            while app.is_running:
                await asyncio.sleep(0.5)
        finally:
            await app.stop()

    try:
        asyncio.run(_cli())
    except KeyboardInterrupt:
        pass
