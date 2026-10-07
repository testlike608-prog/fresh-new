"""
lifecycle.py
============
مدير دورة حياة الـ App — الحالات والانتقالات بينها في مكان واحد.

ليه الملف ده موجود؟
-------------------
قبل كده كان الـ App instance واحد عايش طول عمر السيرفر، و start()/stop()
بيحاولوا يعيدوا استخدامه. ده كان بيسيب حالة قديمة (ثريد لسه بيوقف، كاميرا
شغالة، RPC مفتوح) تتسرّب للدورة الجديدة.

دلوقتي:
    Start  →  App instance **جديد بالكامل** (كاميرا جديدة + موديل AI
              يتحمّل من الأول + RPC جديد) = كأنك قفلت البرنامج وفتحته
    Stop   →  teardown كامل ثم التخلص من الـ instance (app = None)

كل الانتقالات محمية بـ asyncio.Lock، فدوستين Start سريعة أو Start و Stop
مع بعض مابيعملوش race — التاني بيستنى الأول يخلص.

الحالات:
    STOPPED ──start()──► STARTING ──► RUNNING ──stop()──► STOPPING ──► STOPPED
                             │                                │
                             └──── فشل ──► STOPPED ◄───────────┘
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import ClientsClass as cc
import debug_monitor
from thread_logger import get_logger as _get_thread_logger


class LifecycleState:
    STOPPED  = "STOPPED"
    STARTING = "STARTING"
    RUNNING  = "RUNNING"
    STOPPING = "STOPPING"

    LABELS = {
        STOPPED:  "متوقف — اضغط Start",
        STARTING: "جاري التشغيل (كاميرا / AI / روبوت)...",
        RUNNING:  "شغّال — ينتظر إشارة DI0",
        STOPPING: "جاري الإيقاف — بيتم غلق الكاميرا والروبوت...",
    }


class AppLifecycle:
    """
    مالك الـ App الوحيد. web_server بيتكلم معاه هو بس، ومابيلمسش
    الـ App instance مباشرة غير للقراءة (lifecycle.app).
    """

    def __init__(self):
        self._app: Optional[cc.App] = None
        self._state = LifecycleState.STOPPED
        self._lock = asyncio.Lock()
        self._last_error: Optional[str] = None
        self._state_since = time.time()

    # ── قراءة ──────────────────────────────────────────────────────────

    @property
    def app(self) -> Optional[cc.App]:
        return self._app

    @property
    def state(self) -> str:
        return self._state

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def is_busy(self) -> bool:
        """True أثناء STARTING/STOPPING — الأزرار لازم تبقى مقفولة."""
        return self._state in (LifecycleState.STARTING, LifecycleState.STOPPING)

    def _set_state(self, state: str):
        self._state = state
        self._state_since = time.time()

    def snapshot(self) -> dict:
        """
        snapshot موحّد للداشبورد — بيشتغل سواء الـ App موجود أو لأ.
        """
        base = {
            "lifecycle_state": self._state,
            "lifecycle_label": LifecycleState.LABELS.get(self._state, self._state),
            "lifecycle_busy":  self.is_busy,
            "error":           self._last_error,
        }

        app = self._app
        if app is None:
            # متوقف تمامًا — مفيش هاردوير شغال، فكل الـ LEDs OFF
            base.update({
                "is_running": False,
                "stage":      cc.AppStage.IDLE,
                "barcode":    None,
                "program":    None,
                "step":       0,
                "vision_test_count": cc.AppStage.get_vision_test_count(),
                "stats":      {"total": 0, "pass": 0, "fail": 0, "errors": 0},
                "queue_sizes": {"vision_queue": 0, "scanner_queue": 0},
                "last_event_time": None,
                "uptime":     0,
                "last_images": [],
                "connections": {"robot": False, "camera": False,
                                "ai": False, "scanner": False},
            })
            # الإحصائيات المحفوظة من آخر جلسة عشان الأرقام ماتختفيش
            try:
                saved = cc.load_session_stats()
                base["stats"]   = saved["stats"]
                base["barcode"] = saved["last_barcode"]
            except Exception:
                pass
            return base

        snap = app.get_state_snapshot()
        snap.update(base)
        # RUNNING الحقيقية = اللوب شغال فعلاً
        snap["is_running"] = (self._state == LifecycleState.RUNNING
                              and snap.get("is_running", False))
        return snap

    # ── تشغيل ──────────────────────────────────────────────────────────

    async def start(self) -> dict:
        """
        يبني App جديد بالكامل ويشغّله. بيرجع فورًا بعد ما اللوب يبدأ
        (التهيئة نفسها — كاميرا/موديل — بتتم قبل الرجوع).
        """
        log = _get_thread_logger()
        async with self._lock:
            if self._state == LifecycleState.RUNNING:
                return {"ok": True, "state": self._state, "msg": "already running"}
            if self._state != LifecycleState.STOPPED:
                return {"ok": False, "state": self._state,
                        "msg": f"busy ({self._state}) — استنى لحد ما يخلص"}

            self._set_state(LifecycleState.STARTING)
            self._last_error = None

        t0 = time.time()
        app = None
        try:
            # App جديد = كاميرا جديدة + موديل الـ AI يتحمّل من الأول.
            # بيتم في thread عشان مايبلوكش الـ event loop (تحميل torch/CUDA
            # ممكن ياخد ثواني).
            log.info("=== lifecycle: بناء App جديد (كاميرا + موديل AI)... ===")
            app = await asyncio.to_thread(cc.App)
            await app.start()

            self._app = app
            debug_monitor.start(app_ref=app, interval=2.0, force=True,
                                verbose_console=False)
            async with self._lock:
                self._set_state(LifecycleState.RUNNING)
            log.info(f"=== lifecycle: RUNNING في {time.time() - t0:.1f}s ===")
            return {"ok": True, "state": self._state}

        except Exception as e:
            log.exception(f"lifecycle.start فشل: {e}")
            self._last_error = str(e)
            # تنضيف اللي اتعمل بالفعل — مانسيبش كاميرا/RPC مفتوحين
            if app is not None:
                try:
                    await app.aclose(timeout=8.0)
                except Exception:
                    pass
            self._app = None
            async with self._lock:
                self._set_state(LifecycleState.STOPPED)
            return {"ok": False, "state": self._state, "msg": str(e)}

    # ── إيقاف ──────────────────────────────────────────────────────────

    async def stop(self, timeout: float = 15.0) -> dict:
        """
        إيقاف كامل: StopMotion فورًا، ثم غلق الكاميرا والسكانر والروبوت،
        ثم التخلص من الـ instance. بيرجع **بعد** ما كل حاجة تتقفل فعلاً
        (مش بيرجع فورًا زي الأول) عشان الداشبورد ماتكدبش على المستخدم.
        """
        log = _get_thread_logger()
        async with self._lock:
            if self._state == LifecycleState.STOPPED:
                return {"ok": True, "state": self._state, "msg": "already stopped"}
            if self._state == LifecycleState.STOPPING:
                return {"ok": True, "state": self._state, "msg": "stop in progress"}
            app = self._app
            self._set_state(LifecycleState.STOPPING)

        # StopMotion فورًا وبرّه أي await — قطع الحركة أهم حاجة
        if app is not None:
            try:
                app.request_stop("user pressed Stop")
            except Exception as e:
                log.warning(f"request_stop فشل: {e}")

        try:
            debug_monitor.stop()
        except Exception:
            pass

        t0 = time.time()
        try:
            if app is not None:
                await app.aclose(timeout=timeout)
        except Exception as e:
            log.exception(f"lifecycle.stop: aclose فشل: {e}")
            self._last_error = str(e)
        finally:
            self._app = None
            async with self._lock:
                self._set_state(LifecycleState.STOPPED)

        log.info(f"=== lifecycle: STOPPED في {time.time() - t0:.1f}s — كل الهاردوير اتقفل ===")
        return {"ok": True, "state": self._state}

    # ── إيقاف السيرفر ──────────────────────────────────────────────────

    async def shutdown(self):
        """بيتنادى من lifespan عند غلق السيرفر."""
        if self._state != LifecycleState.STOPPED or self._app is not None:
            await self.stop(timeout=8.0)


# الـ instance الوحيد اللي web_server بيستخدمه
lifecycle = AppLifecycle()
