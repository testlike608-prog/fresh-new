"""
camera_hub.py
=============
Module موحد لكل أنواع الكاميرات — نفس الـ pattern بتاع ai_vision.py.

API:
    cam = CameraHub.OpenCV(camera_index=0)         ← ويب كام / USB عادي
    cam = CameraHub.UseePlus(camera_index=0)       ← useeplus endoscope (VID=0x2CE3)

    cam.start()          → يبدأ في ثريد خلفي
    cam.stop()           → يوقف
    cam.restart()        → يوقف ويعيد التشغيل
    cam.get_frame()      → numpy array BGR أو None
    cam.is_running()     → True لو شغال
    cam.wait_for_frame() → ينتظر أول فريم

إضافة camera type جديد (3 خطوات):
    1. اعمل class يورث من CameraHub
    2. نفذ _capture_loop(camera_index) فقط — الباقي جاهز
    3. اربطه:  CameraHub.MyCamera = MyCameraClass
"""

from __future__ import annotations

import threading
import time
import logging
from abc import ABC, abstractmethod

log = logging.getLogger("camera_hub")


# ══════════════════════════════════════════════════════════════════════════════
#  PARENT CLASS
# ══════════════════════════════════════════════════════════════════════════════

class CameraHub(ABC):
    """
    الكلاس الأب المشترك لكل camera drivers.

    بيوفر:
      - State management  : thread، locks، latest frame
      - start() / stop()  : lifecycle كامل مع thread safety
      - restart()         : stop + start بـ camera_index جديد
      - get_frame()       : يرجع نسخة من آخر فريم بأمان
      - is_running()      : حالة الـ thread
      - wait_for_frame()  : ينتظر أول فريم (مفيد بعد start)

    _capture_loop() هو الـ abstract الوحيد — كل driver بينفذه بنفسه.

    Interfaces:
      CameraHub.OpenCV    — cv2.VideoCapture (ويب كام / USB عادي)
      CameraHub.UseePlus  — useeplus USB endoscope (VID=0x2CE3 / PID=0x3828)
    """

    DEFAULT_CAM_INDEX = 0

    def __init__(
        self,
        camera_index: int | None = None,
        frame_width: int = 1280,
        frame_height: int = 720,
    ):
        """
        Parameters
        ----------
        camera_index : رقم الكاميرا الافتراضي (ممكن يتغير في start/restart)
        frame_width  : العرض المطلوب (بيُطبَّق لو الـ driver يدعمه)
        frame_height : الارتفاع المطلوب
        """
        self._cam_index    = camera_index if camera_index is not None else self.DEFAULT_CAM_INDEX
        self.frame_width   = frame_width
        self.frame_height  = frame_height

        self._stop_event   = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock         = threading.Lock()   # يحمي _thread
        self._frame_lock   = threading.Lock()   # يحمي _latest_frame
        self._latest_frame = None               # آخر فريم (numpy array BGR)

    # ── Public API ────────────────────────────────────────────────────────────

    def get_frame(self):
        """
        يرجع نسخة (copy) من آخر فريم أو None لو مفيش فريم بعد.
        آمن للاستخدام من أي thread.
        """
        with self._frame_lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    def is_running(self) -> bool:
        """يرجع True لو الـ capture loop شغال."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def wait_for_frame(self, timeout: float = 5.0) -> bool:
        """
        يستنى لحد ما أول فريم يتقرأ (أو timeout).
        يرجع True لو جه الفريم، False لو انتهى الوقت بدون فريم.
        استخدمه دايماً بعد start() وقبل ما تبدأ تقرأ فريمات.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._frame_lock:
                if self._latest_frame is not None:
                    log.info(f"{self._log_name}: ✓ أول فريم اتقرأ")
                    return True
            time.sleep(0.05)
        log.error(f"{self._log_name}: ✗ timeout {timeout}s — مفيش فريم!")
        return False

    def start(self, camera_index: int | None = None):
        """
        يبدأ التقاط الفريمات في ثريد خلفي.
        لو شغالة بالفعل مش بيعمل حاجة.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                log.debug(f"{self._log_name}: start() — شغالة بالفعل")
                return

            if camera_index is not None:
                self._cam_index = camera_index
            elif self._cam_index is None:
                try:
                    from config import config as _cfg
                    self._cam_index = int(_cfg.get("camera_index", self.DEFAULT_CAM_INDEX))
                except Exception:
                    self._cam_index = self.DEFAULT_CAM_INDEX

            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._capture_loop,
                args=(self._cam_index,),
                name=self._thread_name,
                daemon=True,
            )
            self._thread.start()
            log.info(f"{self._log_name}: بدأت (كاميرا {self._cam_index})")

    def stop(self, timeout: float = 3.0):
        """يوقف الكاميرا وينتظر الثريد ينتهي."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                log.debug(f"{self._log_name}: stop() — مش شغالة")
                return
            self._stop_event.set()
            t = self._thread

        t.join(timeout=timeout)
        if t.is_alive():
            log.warning(f"{self._log_name}: الثريد لم ينتهِ في الوقت المحدد")
        else:
            log.info(f"{self._log_name}: أوقفت بنجاح")

        with self._lock:
            self._thread = None

    def restart(self, camera_index: int | None = None) -> bool:
        """
        يوقف الكاميرا ويشغّلها تاني برقم جديد (اختياري).
        يرجع True لو نجح وجه أول فريم، False لو فشل.
        """
        idx = camera_index or self._cam_index
        log.info(f"{self._log_name}: restarting (camera {idx})...")
        self.stop(timeout=3.0)
        time.sleep(0.2)  # استنى الـ driver يحرر الكاميرا
        self.start(camera_index=idx)
        ok = self.wait_for_frame(timeout=6.0)
        if ok:
            log.info(f"{self._log_name}: restarted successfully (camera {idx})")
        else:
            log.error(f"{self._log_name}: restart failed — camera {idx} لم تستجب")
        return ok

    # ── Internal helpers ──────────────────────────────────────────────────────

    @property
    def _log_name(self) -> str:
        return self.__class__.__name__

    @property
    def _thread_name(self) -> str:
        return f"camera-{self.__class__.__name__.lower()}"

    def _set_frame(self, frame):
        """يحدّث الـ latest frame بشكل آمن (للاستخدام داخل _capture_loop)."""
        with self._frame_lock:
            self._latest_frame = frame

    def _clear_frame(self):
        """يمسح الـ frame الأخير (عند الإغلاق)."""
        with self._frame_lock:
            self._latest_frame = None

    # ── Abstract ──────────────────────────────────────────────────────────────

    @abstractmethod
    def _capture_loop(self, camera_index: int):
        """
        الـ loop الأساسي للكاميرا — يشتغل في ثريد خلفي.

        لازم:
          - يستخدم self._set_frame(frame) لتحديث الفريم
          - يراقب self._stop_event.is_set() للخروج
          - ينهي بـ self._clear_frame() في finally
        """
        ...


# ══════════════════════════════════════════════════════════════════════════════
#  INTERFACE: OpenCV  (ويب كام / USB عادي)
# ══════════════════════════════════════════════════════════════════════════════

class _OpenCV(CameraHub):
    """
    CameraHub.OpenCV — أي كاميرا بيدعمها OpenCV (ويب كام، USB، RTSP).

    مثال:
        cam = CameraHub.OpenCV(camera_index=0)
        cam.start()
        frame = cam.get_frame()
    """

    def _capture_loop(self, camera_index: int):
        import cv2

        # DSHOW أسرع على Windows، وإلا auto-detect
        cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(camera_index)
        if not cap.isOpened():
            log.error(
                f"{self._log_name}: ❌ مش قادر أفتح الكاميرا {camera_index} "
                "— تأكد إنها متوصلة ومش مفتوحة ببرنامج تاني"
            )
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  self.frame_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.frame_height)

        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        log.info(f"{self._log_name}: ✅ Camera {camera_index} شغالة ({actual_w}x{actual_h})")
        print(f"[{self._log_name}] opened camera index={camera_index} ({actual_w}x{actual_h})")

        try:
            while not self._stop_event.is_set():
                ret, frame = cap.read()
                if ret:
                    self._set_frame(frame)
                else:
                    log.warning(f"{self._log_name}: فريم فاشل، هحاول تاني...")
                    time.sleep(0.05)
                    continue
                time.sleep(0.01)   # ~100 fps max
        except Exception as e:
            log.error(f"{self._log_name}: خطأ غير متوقع: {e}")
        finally:
            cap.release()
            self._clear_frame()
            log.info(f"{self._log_name}: الكاميرا اتقفلت.")


# ══════════════════════════════════════════════════════════════════════════════
#  INTERFACE: UseePlus  (USB endoscope — VID=0x2CE3 / PID=0x3828)
# ══════════════════════════════════════════════════════════════════════════════

class _UseePlus(CameraHub):
    """
    CameraHub.UseePlus — useeplus SuperCamera (USB endoscope).

    المتطلبات:
        pip install pyusb opencv-python numpy libusb-package
        + Zadig (WinUSB driver) للكاميرا على Windows

    مثال:
        cam = CameraHub.UseePlus(camera_index=0)
        cam.start()
        frame = cam.get_frame()
    """

    # ── USB Constants ─────────────────────────────────────────────────────────
    VENDOR_ID    = 0x2CE3
    PRODUCT_ID   = 0x3828
    INTERFACE    = 1
    ALT_SETTING  = 1
    EP_OUT       = 0x01
    EP_IN        = 0x81
    CONNECT_CMD  = bytes([0xBB, 0xAA, 0x05, 0x00, 0x00])
    HEADER_MAGIC = bytes([0xAA, 0xBB, 0x07])
    HEADER_SIZE  = 12
    JPEG_SOI     = bytes([0xFF, 0xD8])
    JPEG_EOI     = bytes([0xFF, 0xD9])

    READ_TIMEOUT  = 500       # ms — أقصر عشان نكتشف freeze بسرعة
    WRITE_TIMEOUT = 5000      # ms
    CHUNK_SIZE    = 64 * 1024 # 64 KB per USB read
    FREEZE_TIMEOUT = 2.0      # ثواني بدون فريم → recovery
    BUF_MAX        = 2 * 1024 * 1024  # 2 MB حد أقصى للبفر

    def _capture_loop(self, camera_index: int):
        import queue
        import numpy as np
        import cv2

        # ── تحميل pyusb ───────────────────────────────────────────────────────
        try:
            import usb.core
            import usb.util
            try:
                import libusb_package
                import usb.backend.libusb1 as _lb1
                _backend = _lb1.get_backend(find_library=libusb_package.find_library)
            except ImportError:
                _backend = None
        except ImportError:
            log.error(f"{self._log_name}: ❌ pyusb مش متثبّت — شغّل: pip install pyusb")
            return

        # ── إيجاد الجهاز ──────────────────────────────────────────────────────
        kw = {"backend": _backend} if _backend else {}
        devices = list(usb.core.find(
            idVendor=self.VENDOR_ID, idProduct=self.PRODUCT_ID,
            find_all=True, **kw,
        ))
        if not devices:
            log.error(
                f"{self._log_name}: ❌ الكاميرا مش موجودة "
                "— تأكد USB متوصل + Zadig مثبّت"
            )
            return

        if camera_index >= len(devices):
            log.warning(
                f"{self._log_name}: camera_index={camera_index} أكبر من "
                f"عدد الأجهزة ({len(devices)}) — هستخدم 0"
            )
            camera_index = 0

        dev = devices[camera_index]

        # ── إعداد USB ─────────────────────────────────────────────────────────
        try:
            try:
                if dev.is_kernel_driver_active(self.INTERFACE):
                    dev.detach_kernel_driver(self.INTERFACE)
            except (NotImplementedError, Exception):
                pass
            dev.set_configuration()
            dev.set_interface_altsetting(
                interface=self.INTERFACE,
                alternate_setting=self.ALT_SETTING,
            )
            dev.write(self.EP_OUT, self.CONNECT_CMD, self.WRITE_TIMEOUT)
            log.info(
                f"{self._log_name}: ✅ Camera {camera_index} شغالة "
                f"(VID={self.VENDOR_ID:#06x} PID={self.PRODUCT_ID:#06x})"
            )
            print(f"[{self._log_name}] opened useeplus camera index={camera_index}")
        except Exception as e:
            log.error(f"{self._log_name}: ❌ فشل تهيئة USB: {e}")
            return

        # ── Decode thread — منفصل لتجنب blocking ─────────────────────────────
        decode_q: queue.Queue = queue.Queue(maxsize=3)

        def _decode_worker():
            while True:
                item = decode_q.get()
                if item is None:
                    break
                arr     = np.frombuffer(item, dtype=np.uint8)
                decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if decoded is not None:
                    self._set_frame(decoded)
                decode_q.task_done()

        dec_thread = threading.Thread(
            target=_decode_worker, name="cam-useeplus-decode", daemon=True
        )
        dec_thread.start()

        # ── Accumulation buffer helpers ────────────────────────────────────────
        buf = bytearray()

        def _feed(raw: bytes):
            if len(raw) >= 3 and raw[:3] == self.HEADER_MAGIC:
                buf.extend(raw[self.HEADER_SIZE:])
            else:
                buf.extend(raw)

        def _extract_jpeg() -> bytes | None:
            soi = buf.find(self.JPEG_SOI)
            if soi == -1:
                return None
            eoi = buf.find(self.JPEG_EOI, soi + 2)
            if eoi == -1:
                return None
            end  = eoi + 2
            jpeg = bytes(buf[soi:end])
            del buf[:end]
            return jpeg

        def _recover():
            """يصحّح الـ USB endpoint بعد pipe error أو freeze."""
            try:
                dev.clear_halt(self.EP_IN)
                dev.write(self.EP_OUT, self.CONNECT_CMD, self.WRITE_TIMEOUT)
                del buf[:]
                log.info(f"{self._log_name}: ✅ endpoint cleared — CONNECT_CMD resent")
            except Exception as e:
                log.warning(f"{self._log_name}: ⚠️ recovery failed: {e}")

        # ── Read loop ─────────────────────────────────────────────────────────
        last_frame_time = time.time()
        recovery_count  = 0

        try:
            while not self._stop_event.is_set():
                try:
                    raw = bytes(dev.read(self.EP_IN, self.CHUNK_SIZE, self.READ_TIMEOUT))
                except Exception as e:
                    err = str(e).lower()

                    if "timed out" in err:
                        if time.time() - last_frame_time > self.FREEZE_TIMEOUT:
                            recovery_count += 1
                            log.warning(
                                f"{self._log_name}: ⚠️ freeze #{recovery_count} — recovering..."
                            )
                            _recover()
                            last_frame_time = time.time()
                        continue

                    if "pipe" in err or "errno 32" in err or "stall" in err:
                        recovery_count += 1
                        log.warning(
                            f"{self._log_name}: ⚠️ pipe error #{recovery_count} — recovering..."
                        )
                        _recover()
                        last_frame_time = time.time()
                        time.sleep(0.05)
                        continue

                    log.error(f"{self._log_name}: خطأ USB read: {e}")
                    time.sleep(0.1)
                    continue

                if not raw:
                    continue

                _feed(raw)

                # لو البفر كبر — احذف القديم وابدأ من أحدث SOI
                if len(buf) > self.BUF_MAX:
                    soi = buf.rfind(self.JPEG_SOI)
                    if soi > 0:
                        del buf[:soi]
                    else:
                        del buf[:]

                # استخرج JPEGs وابعتها لـ decode thread
                got_jpeg = False
                while True:
                    jpeg = _extract_jpeg()
                    if jpeg is None:
                        break
                    got_jpeg = True
                    try:
                        decode_q.put_nowait(jpeg)
                    except queue.Full:
                        pass  # دايماً نحافظ على أحدث فريم

                if got_jpeg:
                    last_frame_time = time.time()

        except Exception as e:
            log.error(f"{self._log_name}: خطأ غير متوقع: {e}")
        finally:
            decode_q.put(None)
            dec_thread.join(timeout=2.0)
            try:
                dev.set_interface_altsetting(
                    interface=self.INTERFACE, alternate_setting=0
                )
                usb.util.dispose_resources(dev)
            except Exception:
                pass
            self._clear_frame()
            log.info(
                f"{self._log_name}: الكاميرا اتقفلت. (recoveries={recovery_count})"
            )


# ══════════════════════════════════════════════════════════════════════════════
#  ربط الـ interfaces بالـ parent class
# ══════════════════════════════════════════════════════════════════════════════

CameraHub.OpenCV    = _OpenCV    # type: ignore[attr-defined]
CameraHub.UseePlus  = _UseePlus  # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════════════════════════════
#  تشغيل مباشر للاختبار
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    import cv2
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    cam_type  = sys.argv[1] if len(sys.argv) > 1 else "opencv"  # opencv / useeplus
    cam_index = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    cam = CameraHub.OpenCV(camera_index=cam_index) if cam_type == "opencv" \
          else CameraHub.UseePlus(camera_index=cam_index)

    cam.start()
    print(f"[{cam_type}] اضغط Ctrl+C للإيقاف...")

    try:
        if cam.wait_for_frame(timeout=5.0):
            while True:
                frame = cam.get_frame()
                if frame is not None:
                    cv2.imshow("camera_hub", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        cam.stop()
        cv2.destroyAllWindows()
