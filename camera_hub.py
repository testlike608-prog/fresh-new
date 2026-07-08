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

    ملاحظات مهمة:
      - السنسور بيبعت 640×480 MJPEG فقط (أقصى دقة هاردوير حقيقية).
      - أبلكيشن UseePlus بيعمل upscale برمجي ×3 → 1920×1440.
        الكلاس بيعمل نفس الحاجة افتراضياً (upscale=True + sharpen=True).
      - البروتوكول (UPP): كل رسالة USB = هيدر 5 بايت (magic + camera_id + length)
        + هيدر كاميرا 7 بايت (frame_id + cam_num + flags + g_sensor) + جزء من الـ JPEG.
        بنجمّع الأجزاء لحد ما الـ frame_id يتغير = فريم كامل.
      - أي فريم هيدرز رسايله متضاربة أو الـ JPEG بتاعه ناقص بيترمي بالكامل —
        عشان كده مش هتشوف فريمات مشوهة أبداً (زي الأبلكيشن بالظبط).

    المتطلبات:
        pip install pyusb opencv-python numpy libusb-package
        + Zadig (WinUSB driver) للكاميرا على Windows

    مثال:
        cam = CameraHub.UseePlus(camera_index=0)   # 1920×1440 زي الأبلكيشن
        cam = CameraHub.UseePlus(upscale=False)    # 640×480 خام
        cam.start()
        frame = cam.get_frame()
    """

    # ── USB Constants ─────────────────────────────────────────────────────────
    VENDOR_ID    = 0x2CE3
    PRODUCT_ID   = 0x3828
    INTERFACE    = 1
    ALT_SETTING  = 1
    EP_OUT       = 0x01
    EP_OUT2      = 0x02
    EP_IN        = 0x81
    MAGIC_WORDS  = bytes([0xFF, 0x55, 0xFF, 0x55, 0xEE, 0x10])  # init على EP2 (الأبلكيشن بيبعتها)
    CONNECT_CMD  = bytes([0xBB, 0xAA, 0x05, 0x00, 0x00])        # start stream

    # ── UPP protocol ──────────────────────────────────────────────────────────
    USB_MAGIC    = bytes([0xAA, 0xBB])  # uint16 LE = 0xBBAA
    USB_HDR_LEN  = 5     # magic(2) + camera_id(1) + length(2 LE، مش شاملة الهيدر)
    CAM_HDR_LEN  = 7     # frame_id(1) + cam_num(1) + flags(1) + g_sensor(4)
    VALID_CIDS   = (7, 11)
    MAX_MSG_LEN  = 4096  # sanity limit للـ length field
    JPEG_SOI     = bytes([0xFF, 0xD8])
    JPEG_EOI     = bytes([0xFF, 0xD9])

    READ_TIMEOUT  = 500       # ms — أقصر عشان نكتشف freeze بسرعة
    WRITE_TIMEOUT = 5000      # ms
    CHUNK_SIZE    = 64 * 1024 # 64 KB per USB read
    FREEZE_TIMEOUT = 2.0      # ثواني بدون فريم → recovery
    STARTUP_GRACE  = 6.0      # مهلة أطول قبل أول recovery (الكاميرا بتاخد وقت تثبّت)
    BUF_MAX        = 2 * 1024 * 1024  # 2 MB حد أقصى للبفر

    NATIVE_SIZE    = (640, 480)  # دقة السنسور الحقيقية

    def __init__(
        self,
        camera_index: int | None = None,
        frame_width: int = 1920,
        frame_height: int = 1440,
        upscale: bool = True,
        sharpen: bool = True,
    ):
        """
        upscale : يكبّر الفريم لـ frame_width×frame_height (زي الأبلكيشن بالظبط)
        sharpen : unsharp mask خفيف بعد التكبير (نفس مظهر الأبلكيشن)
        """
        super().__init__(camera_index, frame_width, frame_height)
        self.upscale = upscale
        self.sharpen = sharpen
        self.on_button_press = None  # callback اختياري لزرار الإندوسكوب

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

        # ── إعداد USB (نفس تسلسل الأبلكيشن الرسمي) ───────────────────────────
        try:
            for _intf in (0, self.INTERFACE):
                try:
                    if dev.is_kernel_driver_active(_intf):
                        dev.detach_kernel_driver(_intf)
                except Exception:
                    pass
            dev.set_configuration()
            try:
                usb.util.claim_interface(dev, 0)
                usb.util.claim_interface(dev, self.INTERFACE)
            except Exception:
                pass
            dev.set_interface_altsetting(
                interface=self.INTERFACE,
                alternate_setting=self.ALT_SETTING,
            )
            for _ep in (self.EP_OUT, self.EP_IN):
                try:
                    dev.clear_halt(_ep)
                except Exception:
                    pass
            # "الكلمات السحرية" — الأبلكيشن بيبعتها على EP2 قبل بدء الستريم
            try:
                dev.write(self.EP_OUT2, self.MAGIC_WORDS, self.WRITE_TIMEOUT)
            except Exception as e:
                log.warning(f"{self._log_name}: ⚠️ EP2 init فشل (غالباً مش مشكلة): {e}")
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
        out_size = (self.frame_width, self.frame_height)

        def _decode_worker():
            while True:
                item = decode_q.get()
                if item is None:
                    break
                arr     = np.frombuffer(item, dtype=np.uint8)
                decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if decoded is not None:
                    if self.upscale and (decoded.shape[1], decoded.shape[0]) != out_size:
                        # نفس اللي الأبلكيشن بيعمله: upscale + شوية sharpening
                        decoded = cv2.resize(
                            decoded, out_size, interpolation=cv2.INTER_LANCZOS4
                        )
                        if self.sharpen:
                            blur    = cv2.GaussianBlur(decoded, (0, 0), 1.5)
                            decoded = cv2.addWeighted(decoded, 1.4, blur, -0.4, 0)
                    self._set_frame(decoded)
                decode_q.task_done()

        dec_thread = threading.Thread(
            target=_decode_worker, name="cam-useeplus-decode", daemon=True
        )
        dec_thread.start()

        # ── UPP stream parser ─────────────────────────────────────────────────
        buf       = bytearray()  # بيانات USB خام
        frame_buf = bytearray()  # payload الفريم الحالي
        cur_hdr   = None         # (fid, cam_num, has_g, other) بتوع الفريم الحالي
        frame_bad = False
        stats     = {"ok": 0, "dropped": 0}

        def _reset_frame():
            nonlocal cur_hdr, frame_bad
            frame_buf.clear()
            cur_hdr   = None
            frame_bad = False

        def _finish_frame() -> bytes | None:
            """يقفل الفريم الحالي — يرجع JPEG سليم أو None (فريم بايظ = يترمي)."""
            jpeg = None
            if not frame_bad and frame_buf[:2] == self.JPEG_SOI:
                eoi = frame_buf.rfind(self.JPEG_EOI)
                if eoi != -1:
                    jpeg = bytes(frame_buf[:eoi + 2])
            if jpeg:
                stats["ok"] += 1
            else:
                stats["dropped"] += 1
                log.debug(
                    f"{self._log_name}: فريم مرفوض "
                    f"(bad={frame_bad}, len={len(frame_buf)})"
                )
            _reset_frame()
            return jpeg

        def _parse_stream() -> list:
            """
            يفكك رسائل UPP من buf ويرجع الفريمات الكاملة السليمة فقط.
            رسالة = [AA BB][cid][len LE][fid][cam][flags][g_sensor x4][payload]
            """
            nonlocal cur_hdr, frame_bad
            frames = []

            while True:
                if len(buf) < self.USB_HDR_LEN:
                    break

                # resync: الماجيك لازم يبقى في أول البفر
                if buf[:2] != self.USB_MAGIC:
                    idx = buf.find(self.USB_MAGIC, 1)
                    if idx == -1:
                        del buf[:-1]  # سيب آخر بايت (الماجيك ممكن يكون متقسم)
                        break
                    del buf[:idx]
                    continue

                cid    = buf[2]
                length = buf[3] | (buf[4] << 8)
                if cid not in self.VALID_CIDS or \
                        not (self.CAM_HDR_LEN <= length <= self.MAX_MSG_LEN):
                    del buf[:2]  # هيدر بايظ → دور على الماجيك اللي بعده
                    continue

                total = self.USB_HDR_LEN + length
                if len(buf) < total:
                    break  # الرسالة لسه ما كملتش — استنى بيانات أكتر

                fid     = buf[5]
                cam_num = buf[6]
                flags   = buf[7]
                has_g   = flags & 0x01
                button  = (flags >> 1) & 0x01
                other   = flags >> 2
                payload = bytes(buf[self.USB_HDR_LEN + self.CAM_HDR_LEN:total])
                del buf[:total]

                if button and self.on_button_press:
                    try:
                        self.on_button_press()
                    except Exception:
                        pass

                # frame_id اتغير = الفريم اللي فات اكتمل
                if cur_hdr is not None and fid != cur_hdr[0]:
                    jpeg = _finish_frame()
                    if jpeg:
                        frames.append(jpeg)

                if cur_hdr is None:
                    # أول رسالة في فريم جديد — الهيدر لازم يكون سليم
                    if cam_num < 2 and has_g == 0 and other == 0:
                        cur_hdr = (fid, cam_num, has_g, other)
                        frame_buf.extend(payload)
                    # لو مش سليم: منتصف فريم قديم — نتجاهل لحد بداية فريم نضيف
                else:
                    if (fid, cam_num, has_g, other) != cur_hdr:
                        frame_bad = True  # تضارب → الفريم كله هيترمي
                    else:
                        frame_buf.extend(payload)

            return frames

        def _recover():
            """يصحّح الـ USB endpoint بعد pipe error أو freeze."""
            try:
                dev.clear_halt(self.EP_IN)
                try:
                    dev.write(self.EP_OUT2, self.MAGIC_WORDS, self.WRITE_TIMEOUT)
                except Exception:
                    pass
                dev.write(self.EP_OUT, self.CONNECT_CMD, self.WRITE_TIMEOUT)
                buf.clear()
                _reset_frame()
                log.info(f"{self._log_name}: ✅ endpoint cleared — stream restarted")
            except Exception as e:
                log.warning(f"{self._log_name}: ⚠️ recovery failed: {e}")

        # ── Read loop ─────────────────────────────────────────────────────────
        last_frame_time = time.time()
        got_first_frame = False
        recovery_count  = 0

        try:
            while not self._stop_event.is_set():
                try:
                    raw = bytes(dev.read(self.EP_IN, self.CHUNK_SIZE, self.READ_TIMEOUT))
                except Exception as e:
                    err = str(e).lower()

                    if "timed out" in err:
                        # قبل أول فريم بنستنى أطول — عشان recovery مايقطعش
                        # الستريم والكاميرا لسه بتثبّت (ده كان سبب التقطيع في الأول)
                        limit = self.FREEZE_TIMEOUT if got_first_frame \
                                else self.STARTUP_GRACE
                        if time.time() - last_frame_time > limit:
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

                buf.extend(raw)

                # حماية — مفروض مايحصلش مع البارسر الجديد
                if len(buf) > self.BUF_MAX:
                    buf.clear()
                    _reset_frame()

                for jpeg in _parse_stream():
                    got_first_frame = True
                    last_frame_time = time.time()
                    try:
                        decode_q.put_nowait(jpeg)
                    except queue.Full:
                        # ارمي الأقدم وحافظ على الأحدث (مش العكس!)
                        try:
                            decode_q.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            decode_q.put_nowait(jpeg)
                        except queue.Full:
                            pass

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
                f"{self._log_name}: الكاميرا اتقفلت. "
                f"(frames ok={stats['ok']} dropped={stats['dropped']} "
                f"recoveries={recovery_count})"
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
# EOF
