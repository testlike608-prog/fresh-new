"""
camera_hub_useeplus.py
----------------------
مصدر الكاميرا المشترك — نفس API الـ camera_hub.py الأصلي
لكن يشتغل على كاميرا useeplus (SuperCamera) عبر USB مباشرة.

USB:  VID=0x2CE3 / PID=0x3828
Protocol: proprietary bulk transfer → JPEG frames

API (مطابق للـ camera_hub الأصلي):
    start(camera_index)   → يبدأ التقاط الفريمات
    stop()                → يوقف الكاميرا
    get_frame()           → يرجع نسخة من آخر فريم (numpy array أو None)
    is_running()          → True لو شغالة
    wait_for_frame(timeout) → ينتظر أول فريم
    restart(camera_index) → يوقف ويعيد التشغيل

متطلبات:
    pip install pyusb opencv-python numpy libusb-package
    + Zadig (WinUSB driver) للكاميرا
"""

import threading
import time
import logging
import queue
import numpy as np
import cv2

log = logging.getLogger("camera_hub_useeplus")

# ── ثوابت USB ─────────────────────────────────────────────────────
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

_DEFAULT_CAM_INDEX = 0
READ_TIMEOUT       = 500    # ms — أقصر عشان نكتشف freeze بسرعة
WRITE_TIMEOUT      = 5000   # ms
CHUNK_SIZE         = 64 * 1024
FREEZE_TIMEOUT     = 2.0    # ثواني بدون فريم → recovery
BUF_MAX            = 2 * 1024 * 1024  # 2 MB حد أقصى للبفر

# ── state داخلي ──────────────────────────────────────────────────
_stop_event   = threading.Event()
_thread       = None
_lock         = threading.Lock()
_frame_lock   = threading.Lock()
_latest_frame = None
_cam_index    = _DEFAULT_CAM_INDEX


# ─────────────────────────────────────────────────────────────────
def get_frame():
    """يرجع نسخة من آخر فريم (numpy array BGR) أو None."""
    with _frame_lock:
        return _latest_frame.copy() if _latest_frame is not None else None


def is_running():
    """يرجع True لو الـ capture loop شغال."""
    with _lock:
        return _thread is not None and _thread.is_alive()


def wait_for_frame(timeout: float = 5.0) -> bool:
    """
    ينتظر لحد ما أول فريم يتقرأ (أو timeout).
    يرجع True لو جه الفريم، False لو انتهى الوقت.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _frame_lock:
            if _latest_frame is not None:
                log.info("camera_hub_useeplus: ✓ أول فريم اتقرأ")
                return True
        time.sleep(0.05)
    log.error(f"camera_hub_useeplus: ✗ timeout {timeout}s — مفيش فريم!")
    return False


# ── الـ capture loop ──────────────────────────────────────────────
def _capture_loop(camera_index: int):
    """يشتغل في ثريد خلفي — يفتح USB، يقرأ packets، يحدّث _latest_frame."""
    global _latest_frame

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
        log.error("camera_hub_useeplus: ❌ pyusb مش متثبّت — شغّل: pip install pyusb")
        return

    # ── إيجاد الجهاز ─────────────────────────────────────────────
    devices = list(usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID, find_all=True,
                                 **({'backend': _backend} if _backend else {})))
    if not devices:
        log.error("camera_hub_useeplus: ❌ الكاميرا مش موجودة — تأكد USB متوصل + Zadig مثبّت")
        return

    if camera_index >= len(devices):
        log.warning(f"camera_hub_useeplus: camera_index={camera_index} أكبر من عدد الأجهزة ({len(devices)}) — هستخدم 0")
        camera_index = 0

    dev = devices[camera_index]

    # ── إعداد USB ─────────────────────────────────────────────────
    try:
        try:
            if dev.is_kernel_driver_active(INTERFACE):
                dev.detach_kernel_driver(INTERFACE)
        except (NotImplementedError, Exception):
            pass

        dev.set_configuration()
        dev.set_interface_altsetting(interface=INTERFACE, alternate_setting=ALT_SETTING)
        dev.write(EP_OUT, CONNECT_CMD, WRITE_TIMEOUT)

        log.info(f"camera_hub_useeplus: ✅ Camera {camera_index} شغالة (VID={VENDOR_ID:#06x} PID={PRODUCT_ID:#06x})")
        print(f"[camera_hub_useeplus] opened useeplus camera index={camera_index}")
    except Exception as e:
        log.error(f"camera_hub_useeplus: ❌ فشل تهيئة USB: {e}")
        return

    # ── decode thread — منفصل عن USB read لتجنب blocking ─────────
    _decode_q: queue.Queue = queue.Queue(maxsize=3)

    def _decode_worker():
        global _latest_frame
        while True:
            item = _decode_q.get()
            if item is None:
                break
            arr     = np.frombuffer(item, dtype=np.uint8)
            decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if decoded is not None:
                with _frame_lock:
                    _latest_frame = decoded
            _decode_q.task_done()

    _dec_thread = threading.Thread(target=_decode_worker,
                                   name="cam-decode", daemon=True)
    _dec_thread.start()

    # ── accumulation buffer ───────────────────────────────────────
    buf = bytearray()

    def _feed(raw: bytes):
        if len(raw) >= 3 and raw[:3] == HEADER_MAGIC:
            buf.extend(raw[HEADER_SIZE:])
        else:
            buf.extend(raw)

    def _extract_jpeg() -> bytes | None:
        soi = buf.find(JPEG_SOI)
        if soi == -1:
            return None
        eoi = buf.find(JPEG_EOI, soi + 2)
        if eoi == -1:
            return None
        end = eoi + 2
        jpeg = bytes(buf[soi:end])
        del buf[:end]
        return jpeg

    def _recover():
        """يصحّح الـ USB endpoint بعد pipe error أو freeze."""
        try:
            dev.clear_halt(EP_IN)                      # PyUSB 1.x API الصح
            dev.write(EP_OUT, CONNECT_CMD, WRITE_TIMEOUT)
            del buf[:]
            log.info("camera_hub_useeplus: ✅ endpoint cleared — CONNECT_CMD resent")
        except Exception as e:
            log.warning(f"camera_hub_useeplus: ⚠️ recovery failed: {e}")

    # ── الـ read loop ─────────────────────────────────────────────
    last_frame_time = time.time()
    recovery_count  = 0

    try:
        while not _stop_event.is_set():
            try:
                raw = bytes(dev.read(EP_IN, CHUNK_SIZE, READ_TIMEOUT))
            except Exception as e:
                err = str(e).lower()

                if "timed out" in err:
                    # freeze check
                    if time.time() - last_frame_time > FREEZE_TIMEOUT:
                        recovery_count += 1
                        log.warning(f"camera_hub_useeplus: ⚠️ freeze #{recovery_count} — recovering...")
                        _recover()
                        last_frame_time = time.time()
                    continue

                if "pipe" in err or "errno 32" in err or "stall" in err:
                    # Pipe error = endpoint stall → recover فوراً
                    recovery_count += 1
                    log.warning(f"camera_hub_useeplus: ⚠️ pipe error #{recovery_count} — recovering...")
                    _recover()
                    last_frame_time = time.time()
                    time.sleep(0.05)
                    continue

                log.error(f"camera_hub_useeplus: خطأ USB read: {e}")
                time.sleep(0.1)
                continue

            if not raw:
                continue

            _feed(raw)

            # لو البفر كبر — احذف القديم وابدأ من أحدث SOI
            if len(buf) > BUF_MAX:
                soi = buf.rfind(JPEG_SOI)
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
                    _decode_q.put_nowait(jpeg)
                except queue.Full:
                    pass   # skip لو decode متأخر — دايماً نحافظ على الحديث

            if got_jpeg:
                last_frame_time = time.time()   # reset freeze timer

    except Exception as e:
        log.error(f"camera_hub_useeplus: خطأ غير متوقع: {e}")
    finally:
        _decode_q.put(None)
        _dec_thread.join(timeout=2.0)
        try:
            dev.set_interface_altsetting(interface=INTERFACE, alternate_setting=0)
            usb.util.dispose_resources(dev)
        except Exception:
            pass
        with _frame_lock:
            _latest_frame = None
        log.info(f"camera_hub_useeplus: الكاميرا اتقفلت. (recoveries={recovery_count})")


# ── start / stop / restart ────────────────────────────────────────
def start(camera_index: int = None):
    """يبدأ التقاط في ثريد خلفي. لو شغالة بالفعل مش بيعمل حاجة."""
    global _thread, _cam_index

    with _lock:
        if _thread is not None and _thread.is_alive():
            log.debug("camera_hub_useeplus: start() — شغالة بالفعل")
            return

        if camera_index is None:
            try:
                from config import config as _cfg
                camera_index = int(_cfg.get("camera_index", _DEFAULT_CAM_INDEX))
            except Exception:
                camera_index = _DEFAULT_CAM_INDEX

        _cam_index = camera_index
        _stop_event.clear()
        _thread = threading.Thread(
            target=_capture_loop,
            args=(camera_index,),
            name="camera-hub-useeplus",
            daemon=True,
        )
        _thread.start()
        log.info(f"camera_hub_useeplus: بدأت (كاميرا {camera_index})")


def stop(timeout: float = 3.0):
    """يوقف الكاميرا وينتظر الثريد ينتهي."""
    global _thread

    with _lock:
        if _thread is None or not _thread.is_alive():
            log.debug("camera_hub_useeplus: stop() — مش شغالة")
            return
        _stop_event.set()
        t = _thread

    t.join(timeout=timeout)
    if t.is_alive():
        log.warning("camera_hub_useeplus: الثريد لم ينتهِ في الوقت المحدد")
    else:
        log.info("camera_hub_useeplus: أوقفت بنجاح")

    with _lock:
        _thread = None


def restart(camera_index: int = None):
    """يوقف الكاميرا ويشغّلها تاني برقم جديد."""
    global _cam_index
    log.info(f"camera_hub_useeplus: restarting (camera {camera_index})...")
    stop(timeout=3.0)
    time.sleep(0.2)
    start(camera_index=camera_index)
    ok = wait_for_frame(timeout=6.0)
    if ok:
        log.info(f"camera_hub_useeplus: restarted successfully (camera {camera_index})")
    else:
        log.error(f"camera_hub_useeplus: restart failed — camera {camera_index} لم تستجب")
    return ok


# ── تشغيل مباشر للاختبار ──────────────────────────────────────────
if __name__ == "__main__":
    import sys, os

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    cam = int(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_CAM_INDEX
    out_dir = "./result"
    os.makedirs(out_dir, exist_ok=True)

    start(camera_index=cam)
    print("اضغط Ctrl+C للإيقاف...")

    try:
        while True:
            if wait_for_frame(timeout=5.0):
                f = get_frame()
                if f is not None:
                    path = os.path.join(out_dir, "test.jpg")
                    cv2.imwrite(path, f)
                    print(f"saved: {path}")
                    time.sleep(0.05)
    except KeyboardInterrupt:
        print("\nإيقاف...")
        stop()
