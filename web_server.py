"""
web_server.py
-------------
FastAPI + Socket.IO web interface for the Test Station Controller.

Run:
    pip install fastapi uvicorn[standard] python-socketio
    python web_server.py
    ثم افتح http://localhost:8000
"""

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import socketio

# ── ensure project root in sys.path ──────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# ── Project imports ───────────────────────────────────────────────────
import thread_logger
import debug_monitor
from config import config
import ClientsClass as cc
import scanner


# ════════════════════════════════════════════════════════════════════
#                    Async log bridge
# ════════════════════════════════════════════════════════════════════
# BUG-021: لا تنشئ asyncio.Queue على مستوى الـ module — ينتظر lifespan
_log_queue: asyncio.Queue = None   # type: ignore


class _AsyncBridgeHandler(logging.Handler):
    """logging.Handler آمن للـ threads — يضع records في asyncio.Queue."""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        super().__init__()
        self._loop = loop
        self._queue = queue
        self.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d [%(levelname)-8s] [%(threadName)s] %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record):
        try:
            msg   = self.format(record)
            entry = {"level": record.levelname, "message": msg, "ts": time.time()}
            self._loop.call_soon_threadsafe(self._put_nowait, entry)
        except Exception:
            pass

    def _put_nowait(self, entry):
        try:
            # BUG-021: استخدم self._queue بدل المتغير العام
            self._queue.put_nowait(entry)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════
#                    Stdout/Stderr capture
# ════════════════════════════════════════════════════════════════════
class _StreamToLogger:
    """يحوّل أي print() → log.info عشان يطلع في الـ web dashboard."""

    def __init__(self, logger: logging.Logger, level: int = logging.INFO):
        self._logger  = logger
        self._level   = level
        self._buf     = ""
        self._in_write = False

    def write(self, msg: str):
        if not msg or self._in_write:
            return
        self._in_write = True
        try:
            self._buf += msg
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self._logger.log(self._level, line.rstrip())
        finally:
            self._in_write = False

    def flush(self):
        if self._buf.strip() and not self._in_write:
            self._in_write = True
            try:
                self._logger.log(self._level, self._buf.rstrip())
                self._buf = ""
            finally:
                self._in_write = False

    def isatty(self):
        return False


# ════════════════════════════════════════════════════════════════════
#                    Socket.IO server
# ════════════════════════════════════════════════════════════════════
# SEC-002: قلّل الـ CORS — اسمح فقط لـ localhost/LAN (عدّل لو احتجت)
# يمكن التعديل من متغير البيئة CORS_ORIGINS (مفصولة بفاصلة)
_CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*")  # "*" = اترك كما هو لـ LAN
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=_CORS_ORIGINS,
    logger=False,
    engineio_logger=False,
    ping_timeout=60,
    ping_interval=25,
)


@sio.event
async def connect(sid, environ):
    """لما client يتصل نبعتله snapshot فوري."""
    if app_ref is not None:
        try:
            state = await asyncio.to_thread(app_ref.get_state_snapshot)
            await sio.emit("state_update", state, to=sid)
        except Exception:
            pass


@sio.event
async def disconnect(sid):
    pass


@sio.event
async def ping(sid):
    await sio.emit("pong", {}, to=sid)


# ════════════════════════════════════════════════════════════════════
#                    App state (globals)
# ════════════════════════════════════════════════════════════════════
app_ref: Optional[cc.App] = None
app_init_error: Optional[str] = None


# ════════════════════════════════════════════════════════════════════
#                    Background async tasks
# ════════════════════════════════════════════════════════════════════
async def _state_broadcaster():
    """يعمل emit لكل الـ clients بـ state snapshot كل 500ms."""
    while True:
        try:
            await asyncio.sleep(0.5)
            if app_ref is not None:
                state = await asyncio.to_thread(app_ref.get_state_snapshot)
                await sio.emit("state_update", state)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(1)


async def _log_broadcaster():
    """يسحب من _log_queue ويبعت لكل الـ clients."""
    while True:
        try:
            entry = await asyncio.wait_for(_log_queue.get(), timeout=1.0)
            await sio.emit("log_entry", entry)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(0.1)


# ════════════════════════════════════════════════════════════════════
#                    FastAPI lifespan
# ════════════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    global app_ref, app_init_error, _log_queue

    # 1. Setup thread logger + log bridge
    loop = asyncio.get_event_loop()

    # BUG-021: إنشاء Queue هنا وليس على مستوى الـ module
    _log_queue = asyncio.Queue(maxsize=5000)

    # BUG-007: watchdog_interval من config بدل hardcoded 2.0
    watchdog_interval = float(config.get("watchdog_interval", 2.0))
    log  = thread_logger.setup(watchdog_interval=watchdog_interval)

    bridge = _AsyncBridgeHandler(loop, _log_queue)
    bridge.setLevel(logging.DEBUG)
    for name in ("threadlog", "debug_monitor", "camera_hub", "camera_barcode",
                 "camera_hub_useeplus", "live_image", "stdout", "stderr", ""):
        lg = logging.getLogger(name)
        if not any(isinstance(h, _AsyncBridgeHandler) for h in lg.handlers):
            lg.addHandler(bridge)

    # 2. Redirect stdout/stderr → logger
    if not hasattr(sys, "_original_stdout"):
        sys._original_stdout = sys.stdout
        sys._original_stderr = sys.stderr
    sys.stdout = _StreamToLogger(logging.getLogger("stdout"), logging.INFO)
    sys.stderr = _StreamToLogger(logging.getLogger("stderr"), logging.WARNING)

    # 3. Create App instance
    try:
        app_ref = cc.App()
        debug_monitor.start(app_ref=app_ref, interval=2.0, force=True, verbose_console=False)
        log.info("=== web_server: App created (STOPPED) — press Start in dashboard ===")
    except Exception as e:
        app_init_error = str(e)
        log.exception(f"Could not create App: {e}")

    # 4. Start background async tasks
    state_task = asyncio.create_task(_state_broadcaster(), name="state-broadcaster")
    log_task   = asyncio.create_task(_log_broadcaster(),   name="log-broadcaster")

    log.info("=== web_server: listening on http://0.0.0.0:8000 ===")

    yield  # ← server runs here ←

    # 5. Graceful shutdown
    state_task.cancel()
    log_task.cancel()
    try:
        await asyncio.gather(state_task, log_task, return_exceptions=True)
    except Exception:
        pass

    if app_ref is not None and app_ref.is_running:
        await asyncio.to_thread(app_ref.stop, True, 8.0)  # BUG-049: انتظار الـ thread

    if hasattr(sys, "_original_stdout"):
        sys.stdout = sys._original_stdout
        sys.stderr = sys._original_stderr

    log.info("=== web_server: shutdown complete ===")


# ════════════════════════════════════════════════════════════════════
#                    FastAPI app
# ════════════════════════════════════════════════════════════════════
app = FastAPI(
    title="Test Station Controller",
    description="Web interface for industrial barcode + vision test station",
    lifespan=lifespan,
)

# ── Static files ─────────────────────────────────────────────────────
_STATIC_DIR = os.path.join(_HERE, "static")
os.makedirs(_STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(_STATIC_DIR, "index.html")
    with open(html_path, encoding="utf-8") as f:
        return f.read()


# ════════════════════════════════════════════════════════════════════
#                    REST: State & Control
# ════════════════════════════════════════════════════════════════════

@app.get("/api/state")
async def get_state():
    """Snapshot of current app state."""
    if app_ref is None:
        return {"error": app_init_error or "App not initialised", "is_running": False}
    return await asyncio.to_thread(app_ref.get_state_snapshot)


@app.post("/api/start")
async def start_app():
    """يشغّل البرنامج (non-blocking — App.start() يرجع فوراً).
    start() هي اللي بتتحكم في race conditions — مش محتاجين نعمل check هنا."""
    if app_ref is None:
        raise HTTPException(500, app_init_error or "App not initialised")
    ok = await asyncio.to_thread(app_ref.start)
    if ok is False:
        raise HTTPException(503, "Previous run thread is still stopping — try again in a moment")
    return {"ok": bool(ok)}


@app.post("/api/stop")
async def stop_app():
    """يوقف البرنامج."""
    if app_ref is None:
        raise HTTPException(500, "App not initialised")
    if not app_ref.is_running:
        return {"ok": True, "msg": "already stopped"}
    app_ref.stop()
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════
#                    REST: Reports
# ════════════════════════════════════════════════════════════════════

@app.get("/api/reports/file")
async def download_report():
    """يخدم ملف الـ Excel كـ download."""
    path = config.get("results_report_file", "results_report.xlsx")
    if not os.path.isabs(path):
        path = os.path.join(_HERE, path)
    if not os.path.exists(path):
        raise HTTPException(404, detail="ملف التقرير غير موجود")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=os.path.basename(path),
        headers={"Content-Disposition": f'attachment; filename="{os.path.basename(path)}"'},
    )


# ════════════════════════════════════════════════════════════════════
#                    REST: Barcode injection
# ════════════════════════════════════════════════════════════════════

@app.post("/api/barcode")
async def inject_barcode(body: dict):
    """حقن باركود يدوي من الـ dashboard."""
    # BUG-029: تحقق إن البرنامج شغّال قبل حقن الباركود
    if app_ref is None or not app_ref.is_running:
        raise HTTPException(400, "App is not running — press Start first")

    barcode = (body.get("barcode") or "").strip()
    if not barcode:
        raise HTTPException(400, "barcode field required")

    from barcode_utils import normalize_barcode
    barcode = normalize_barcode(barcode)
    if not barcode:
        raise HTTPException(400, "barcode empty after normalization")

    scanner.queue_barcode.put(barcode)
    return {"ok": True, "barcode": barcode}


# ════════════════════════════════════════════════════════════════════
#                    REST: Camera
# ════════════════════════════════════════════════════════════════════

def _encode_frame_jpeg(frame, quality: int = 75) -> bytes:
    """Encode numpy frame to JPEG bytes."""
    import cv2
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes()


@app.get("/api/camera/frame.jpg")
async def camera_frame_jpg():
    """
    آخر فريم من كاميرا الـ App كـ JPEG.
    Frontend بيستخدمه في <img src="...?t=timestamp"> للتحديث المستمر.
    """
    if app_ref is None:
        raise HTTPException(503, "App not initialised")
    try:
        frame = app_ref._camera.get_frame()
        if frame is None:
            raise HTTPException(503, "No camera frame — camera not running")
        data = await asyncio.to_thread(_encode_frame_jpeg, frame)
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/camera/status")
async def camera_status():
    if app_ref is None:
        return {"running": False, "has_frame": False}
    try:
        running   = app_ref._camera.is_running()
        has_frame = app_ref._camera.get_frame() is not None
        return {"running": running, "has_frame": has_frame}
    except Exception as e:
        return {"running": False, "has_frame": False, "error": str(e)}


# ════════════════════════════════════════════════════════════════════
#                    REST: Config
# ════════════════════════════════════════════════════════════════════

@app.get("/api/config")
async def get_config():
    """يرجع كل الـ config بدون الـ password hash."""
    return await asyncio.to_thread(config.get_all)


@app.post("/api/config/verify_password")
async def verify_password(body: dict):
    pw = body.get("password", "")
    ok = await asyncio.to_thread(config.verify_password, pw)
    return {"ok": ok}


@app.post("/api/config")
async def update_config(body: dict):
    """يحدّث الـ config — يجب إرسال _password في الـ body."""
    password = body.pop("_password", "")
    ok = await asyncio.to_thread(config.verify_password, password)
    if not ok:
        raise HTTPException(403, "Wrong password")

    INT_KEYS = {
        "vision_test_count", "camera_index", "live_camera_index",
        "input_trigger", "test_done", "test_pass", "test_fail",
        "yellow_led", "Switch_camera",
    }
    FLOAT_KEYS = {
        "watchdog_interval", "reconnect_check_interval",
        "reconnect_retry_delay", "debug_monitor_interval",
        "signal_pass_period", "signal_fail_period",   # BUG-033: إصلاح التهجئة
    }
    BOOL_KEYS = {"ai_enhancement"}

    for k, v in list(body.items()):
        if k in INT_KEYS:
            try:
                body[k] = int(float(v)) if v != "" else 0
            except (ValueError, TypeError):
                pass
        elif k in FLOAT_KEYS:
            try:
                body[k] = float(v)
            except (ValueError, TypeError):
                pass
        elif k in BOOL_KEYS:
            if isinstance(v, str):
                body[k] = v.lower() in ("true", "1", "yes")

    changed = await asyncio.to_thread(config.update_many, body)

    # لو camera_index أو camera_type اتغير → restart camera
    if ("camera_index" in body or "camera_type" in body) and app_ref is not None:
        try:
            new_idx = int(body.get("camera_index", config.get("camera_index", 0)))
            if app_ref._camera.is_running():
                asyncio.create_task(
                    asyncio.to_thread(app_ref._camera.restart, new_idx)
                )
        except Exception:
            pass

    return {"ok": True, "changed": changed}


@app.post("/api/config/set_password")
async def set_password(body: dict):
    old = body.get("old_password", "")
    new = body.get("new_password", "")
    ok  = await asyncio.to_thread(config.verify_password, old)
    if not ok:
        raise HTTPException(403, "Wrong current password")
    result = await asyncio.to_thread(config.set_password, new)
    return {"ok": result}


@app.post("/api/config/reset")
async def reset_config(body: dict):
    ok = await asyncio.to_thread(config.verify_password, body.get("password", ""))
    if not ok:
        raise HTTPException(403, "Wrong password")
    await asyncio.to_thread(config.reset_to_defaults)
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════
#                    REST: Session Stats Reset
# ════════════════════════════════════════════════════════════════════

@app.post("/api/stats/reset")
async def reset_stats():
    """إعادة تعيين الإحصائيات المتراكمة (total/pass/fail/errors/last_barcode)."""
    if app_ref is None:
        raise HTTPException(500, "App not initialised")
    await asyncio.to_thread(app_ref.reset_session_stats)
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════
#                    REST: Debug
# ════════════════════════════════════════════════════════════════════

@app.get("/api/debug/snapshot")
async def debug_snapshot():
    snap, ts = debug_monitor.get_last_snapshot()
    return {"snapshot": snap, "ts": ts}


@app.get("/api/debug/threads")
async def list_threads():
    import threading
    threads = [
        {"name": t.name, "alive": t.is_alive(), "daemon": t.daemon}
        for t in threading.enumerate()
    ]
    return {"count": len(threads), "threads": threads}


# ════════════════════════════════════════════════════════════════════
#                    Combined ASGI app
# ════════════════════════════════════════════════════════════════════
combined_app = socketio.ASGIApp(sio, other_asgi_app=app)


# ════════════════════════════════════════════════════════════════════
#                    Entry point
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn

    print("=" * 60)
    print("  Test Station Controller — Web Mode")
    print("  http://localhost:8000")
    print("=" * 60)

    uvicorn.run(
        "web_server:combined_app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="warning",
        workers=1,
    )
