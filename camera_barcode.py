"""
camera_barcode.py
-----------------
سكانر الباركود عن طريق الكاميرا المشتركة (CameraHub) — نسخة async.

لا يفتح الكاميرا بنفسه — يقرأ الفريمات من camera.get_frame()
ويحط الباركودات في scanner.queue_barcode (نفس الـ queue بتاعة الكيبورد
والـ injection من الـ dashboard).

استخدام (جوه event loop):
    task = asyncio.create_task(camera_barcode.run(camera))
    ...
    task.cancel()          # ← ده الـ stop. مفيش ثريد ولا globals.

الـ decode نفسه (zxing) blocking فبيتنفذ في asyncio.to_thread
عشان ما يوقفش الـ event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import cv2
import zxingcpp

import scanner
from barcode_utils import normalize_barcode
from camera_hub import CameraHub

log = logging.getLogger("camera_barcode")

# كم decode في الثانية (ما نضغطش على المعالج)
DECODE_FPS = 10
# بعد ما نقرأ باركود، نفس الباركود ما يتكررش قبل المدة دي
REPEAT_SUPPRESS_S = 3.0

_DATA_DIR           = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DEBUG_FRAME_PATH    = os.path.join(_DATA_DIR, "results", "test.jpg")  # BUG-017 + Docker
DEBUG_SAVE_INTERVAL = 2.0                   # احفظ كل 2 ثانية


def _decode(frame):
    """3 محاولات decode — blocking، بتتنده من to_thread."""
    gray    = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    results = zxingcpp.read_barcodes(gray)
    if not results:
        results = zxingcpp.read_barcodes(cv2.equalizeHist(gray))
    if not results:
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        results = zxingcpp.read_barcodes(thresh)
    return [(r.text.strip(), str(r.format)) for r in results]


async def run(camera: CameraHub):
    """
    الـ decode loop — coroutine. يخلص بس لما الـ task يتعمله cancel.
    """
    frame_interval      = 1.0 / DECODE_FPS
    last_debug_save_at  = 0.0
    none_warn_at        = 0.0
    last_barcode        = None
    last_barcode_at     = 0.0

    os.makedirs(os.path.dirname(DEBUG_FRAME_PATH), exist_ok=True)
    log.info("[CameraScanner] بدأت — في انتظار باركود...")

    try:
        while True:
            frame = camera.get_frame()
            if frame is None:
                now = time.time()
                if now - none_warn_at > 5.0:
                    log.warning("[CameraScanner] الكاميرا مش بتبعت فريمات — استنى...")
                    none_warn_at = now
                await asyncio.sleep(0.05)
                continue

            now = time.time()
            if now - last_debug_save_at >= DEBUG_SAVE_INTERVAL:
                last_debug_save_at = now
                await asyncio.to_thread(cv2.imwrite, DEBUG_FRAME_PATH, frame)

            started = time.time()
            results = await asyncio.to_thread(_decode, frame)

            for raw, fmt in results:
                if not raw:
                    continue
                barcode = normalize_barcode(raw)
                if not barcode:
                    continue
                # منع التكرار المتتالي لنفس الباركود خلال REPEAT_SUPPRESS_S
                if barcode == last_barcode and time.time() - last_barcode_at < REPEAT_SUPPRESS_S:
                    break
                last_barcode, last_barcode_at = barcode, time.time()

                if raw != barcode:
                    log.info(f"[CameraScanner] QR→SN: {raw!r}  →  {barcode!r}")
                scanner.queue_barcode.put(barcode)
                scanner.last_barcode = barcode
                scanner.flag_barcode = True
                log.info(f"[CameraScanner] ✅ باركود: {barcode!r}  ({fmt}) → queue")
                break   # أول باركود في الفريم بس

            # حافظ على DECODE_FPS
            await asyncio.sleep(max(0.0, frame_interval - (time.time() - started)))
    finally:
        log.info("[CameraScanner] أوقفت.")


def get_available_cameras(max_check: int = 6):
    """يكتشف الكاميرات المتاحة (OpenCV) — مفيد لاختيار رقم الكاميرا."""
    found = []
    for i in range(max_check):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        if cap.isOpened():
            found.append(i)
            cap.release()
    return found


# ─── Standalone test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    async def _main():
        hub = CameraHub.UseePlus(camera_index=0)
        if not await hub.astart(timeout=5.0):
            print("❌ الكاميرا مش شغالة")
            return
        task = asyncio.create_task(run(hub))
        print("اضغط Ctrl+C للإيقاف...")
        try:
            while True:
                await asyncio.sleep(0.5)
                while not scanner.queue_barcode.empty():
                    print(f">>> باركود: {scanner.queue_barcode.get_nowait()}")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await hub.astop()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
