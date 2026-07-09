# -*- coding: utf-8 -*-
"""
capture_app.py
==============
برنامج مستقل تماماً (Standalone) لعرض الكاميرا لايف والتقاط صور بزرار Capture.

⚠️ مهم: البرنامج ده منفصل عن نظام الفحص — بيستورد camera_hub.py فقط
   (درايفر الكاميرا الموحد) وماحتاجش أي حاجة تانية من المشروع.
   بس الكاميرا مينفعش تتفتح من برنامجين في نفس الوقت،
   فلازم توقف web_server / ClientsClass الأول قبل ما تشغله.

التشغيل:
    python capture_app.py

المميزات:
    - عرض لايف للكاميرا (UseePlus USB أو أي كاميرا OpenCV عادية)
    - زرار Capture بيحفظ الصورة بالدقة الكاملة في فولدر تختاره
    - تغيير الفولدر واسم البادئة (prefix) من الواجهة
    - عداد للصور المحفوظة

المتطلبات (كلها موجودة في requirements.txt):
    opencv-python, Pillow, numpy, pyusb, libusb (للـ UseePlus فقط)
    tkinter — بييجي مع بايثون نفسه
"""

import os
import time
import threading
from datetime import datetime

import cv2
import numpy as np
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk

# ════════════════════════════════════════════════════════════════════
#  مصدر 1: كاميرا OpenCV عادية (ويب كام / USB UVC)
# ════════════════════════════════════════════════════════════════════

class OpenCVSource:
    def __init__(self, camera_index: int = 0):
        self.camera_index = camera_index
        self._cap = None
        self._frame = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        if not self._cap.isOpened():
            self._cap = cv2.VideoCapture(self.camera_index)  # fallback بدون DSHOW
        if not self._cap.isOpened():
            raise RuntimeError(f"مقدرتش أفتح كاميرا OpenCV رقم {self.camera_index}")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            ok, frame = self._cap.read()
            if ok and frame is not None:
                with self._lock:
                    self._frame = frame
            else:
                time.sleep(0.05)

    def get_frame(self):
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._cap:
            self._cap.release()
        self._frame = None


# ════════════════════════════════════════════════════════════════════
#  مصدر 2: كاميرا UseePlus (USB endoscope — VID 0x2CE3 / PID 0x3828)
#  wrapper رفيع حوالين الدرايفر الموحد في camera_hub.py —
#  نفس البارسر الجديد (UPP protocol + فلترة الفريمات البايظة + upscale 1920×1440)
# ════════════════════════════════════════════════════════════════════

from camera_hub import CameraHub


class UseePlusSource:
    def __init__(self, camera_index: int = 0, **kwargs):
        # upscale=False → 640×480 خام | غيّرها لـ True عشان 1920×1440 زي الأبلكيشن
        # kwargs بتتبعت زي ما هي للدرايفر — مثال لو جهاز محتاج التهيئة الآمنة:
        #   UseePlusSource(camera_index=0, ep2_init=False, clear_halt_init=False)
        kwargs.setdefault("upscale", False)
        self._cam = CameraHub.UseePlus(camera_index=camera_index, **kwargs)

    def start(self):
        self._cam.start()
        if not self._cam.wait_for_frame(timeout=15.0):
            self._cam.stop()
            raise RuntimeError(
                "كاميرا UseePlus مش موجودة أو مش بتبعت فريمات!\n"
                "- اتأكد إنها متوصلة\n"
                "- اتأكد إن WinUSB متسطب بـ Zadig\n"
                "- اتأكد إن البرنامج الرئيسي (web_server) واقف")

    def get_frame(self):
        return self._cam.get_frame()

    def stop(self):
        self._cam.stop()


# ════════════════════════════════════════════════════════════════════
#  واجهة Tkinter
# ════════════════════════════════════════════════════════════════════

PREVIEW_W, PREVIEW_H = 800, 600

class CaptureApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("📸 Capture Tool — Standalone")
        self.geometry("980x760")
        self.minsize(720, 560)

        self.source = None
        self.save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "captures_standalone")
        self.saved_count = 0
        self._photo = None   # مرجع لصورة Tk عشان الـ GC

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._update_preview()

    # ── UI ────────────────────────────────────────────────────────
    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="نوع الكاميرا:").pack(side="right", padx=4)
        self.cam_type = tk.StringVar(value="useeplus")
        ttk.Combobox(top, textvariable=self.cam_type, state="readonly",
                     values=["useeplus", "opencv"], width=10).pack(side="right")

        ttk.Label(top, text="رقم الكاميرا (opencv):").pack(side="right", padx=4)
        self.cam_index = tk.IntVar(value=0)
        ttk.Spinbox(top, from_=0, to=10, textvariable=self.cam_index,
                    width=4).pack(side="right")

        self.btn_start = ttk.Button(top, text="▶ تشغيل الكاميرا",
                                    command=self._toggle_camera)
        self.btn_start.pack(side="left", padx=4)

        # ── منطقة العرض ──
        self.preview = tk.Label(self, bg="#222",
                                text="الكاميرا متوقفة — اضغط تشغيل",
                                fg="#aaa", font=("Segoe UI", 14))
        self.preview.pack(fill="both", expand=True, padx=8, pady=4)

        # ── شريط الحفظ ──
        bottom = ttk.Frame(self, padding=8)
        bottom.pack(fill="x")

        self.btn_capture = ttk.Button(bottom, text="📸  CAPTURE",
                                      command=self._capture, state="disabled")
        self.btn_capture.pack(side="left", padx=4, ipadx=20, ipady=6)

        ttk.Button(bottom, text="📁 اختيار الفولدر",
                   command=self._choose_folder).pack(side="left", padx=4)

        ttk.Label(bottom, text="البادئة:").pack(side="left", padx=(12, 2))
        self.prefix = tk.StringVar(value="capture")
        ttk.Entry(bottom, textvariable=self.prefix, width=14).pack(side="left")

        # ── شريط الحالة ──
        self.status = tk.StringVar(value=f"فولدر الحفظ: {self.save_dir}")
        ttk.Label(self, textvariable=self.status, anchor="e",
                  padding=4).pack(fill="x")

        # اختصار: مسطرة (Space) = Capture
        self.bind("<space>", lambda e: self._capture())

    # ── Camera control ────────────────────────────────────────────
    def _toggle_camera(self):
        if self.source is None:
            self._start_camera()
        else:
            self._stop_camera()

    def _start_camera(self):
        cls = UseePlusSource if self.cam_type.get() == "useeplus" else OpenCVSource
        try:
            src = cls(camera_index=self.cam_index.get())
            src.start()
        except Exception as e:
            messagebox.showerror("خطأ في الكاميرا", str(e))
            return
        self.source = src
        self.btn_start.config(text="⏹ إيقاف الكاميرا")
        self.btn_capture.config(state="normal")
        self.status.set(f"الكاميرا شغالة ({self.cam_type.get()}) — فولدر الحفظ: {self.save_dir}")

    def _stop_camera(self):
        if self.source:
            self.source.stop()
            self.source = None
        self.btn_start.config(text="▶ تشغيل الكاميرا")
        self.btn_capture.config(state="disabled")
        self.preview.config(image="", text="الكاميرا متوقفة — اضغط تشغيل")
        self._photo = None

    # ── Live preview loop ─────────────────────────────────────────
    def _update_preview(self):
        if self.source is not None:
            frame = self.source.get_frame()
            if frame is not None:
                h, w = frame.shape[:2]
                # ملائمة حجم العرض مع الحفاظ على النسبة
                pw = self.preview.winfo_width() or PREVIEW_W
                ph = self.preview.winfo_height() or PREVIEW_H
                scale = min(pw / w, ph / h, 1.0)
                disp = cv2.resize(frame, (max(1, int(w * scale)),
                                          max(1, int(h * scale))))
                rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
                self._photo = ImageTk.PhotoImage(Image.fromarray(rgb))
                self.preview.config(image=self._photo, text="")
        self.after(33, self._update_preview)   # ~30 fps

    # ── Capture ───────────────────────────────────────────────────
    def _capture(self):
        if self.source is None:
            return
        frame = self.source.get_frame()
        if frame is None:
            self.status.set("⚠️ مفيش فريم متاح — استنى الكاميرا")
            return
        os.makedirs(self.save_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.saved_count += 1
        name = f"{self.prefix.get() or 'capture'}_{ts}_{self.saved_count:04d}.png"
        path = os.path.join(self.save_dir, name)
        if cv2.imwrite(path, frame):
            self.status.set(f"✅ اتحفظت ({self.saved_count}): {path}")
        else:
            self.status.set(f"❌ فشل الحفظ في: {path}")

    def _choose_folder(self):
        folder = filedialog.askdirectory(initialdir=self.save_dir,
                                         title="اختار فولدر الحفظ")
        if folder:
            self.save_dir = folder
            self.status.set(f"فولدر الحفظ: {self.save_dir}")

    # ── Cleanup ───────────────────────────────────────────────────
    def _on_close(self):
        self._stop_camera()
        self.destroy()


if __name__ == "__main__":
    CaptureApp().mainloop()
