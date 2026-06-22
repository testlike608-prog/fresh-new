"""
test_camera_diag.py — تشخيص كامل لكاميرا useeplus
"""
import time, sys, logging, collections

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("diag")

print("=" * 60)
print("  useeplus Camera Diagnostic")
print("=" * 60)

# ── 1. تحقق من الـ USB backend ───────────────────────────────────
print("\n[1] Checking USB backend...")
try:
    import usb.core, usb.util, usb.backend.libusb1 as lb1
    backend = None
    try:
        import libusb_package
        backend = lb1.get_backend(find_library=libusb_package.find_library)
        print("    ✅ libusb_package backend OK")
    except ImportError:
        backend = lb1.get_backend()
        if backend:
            print("    ✅ system libusb backend OK")
        else:
            print("    ❌ No libusb backend found!")
            print("       Fix: pip install libusb-package")
            sys.exit(1)
except ImportError as e:
    print(f"    ❌ pyusb not installed: {e}")
    sys.exit(1)

# ── 2. تحقق من وجود الكاميرا ────────────────────────────────────
print("\n[2] Looking for useeplus camera (VID=0x2CE3, PID=0x3828)...")
VENDOR_ID, PRODUCT_ID = 0x2CE3, 0x3828
kwargs = {'backend': backend} if backend else {}
devs = list(usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID, find_all=True, **kwargs))
if not devs:
    print("    ❌ Camera not found!")
    print("       Fix: تأكد الكاميرا متوصلة + Zadig مثبّت WinUSB driver")
    sys.exit(1)
print(f"    ✅ Found {len(devs)} device(s)")
for i, d in enumerate(devs):
    try:
        print(f"       [{i}] {d.manufacturer} / {d.product}  bus={d.bus} addr={d.address}")
    except Exception:
        print(f"       [{i}] bus={d.bus} addr={d.address}")

# ── 3. شغّل الـ camera hub واتبع الفريمات ────────────────────────
MONITOR_SECS = 30   # وقت المراقبة
print(f"\n[3] Starting camera hub — monitoring frames for {MONITOR_SECS}s...")
import camera_hub_useeplus as cam

cam.start(camera_index=0)
ok = cam.wait_for_frame(timeout=8.0)
if not ok:
    print("    ❌ No frame received in 8s — camera not streaming!")
    cam.stop()
    sys.exit(1)

print("    ✅ First frame received\n")
print(f"    Monitoring {MONITOR_SECS}s — سيظهر تحذير عند كل recovery...")

frame_times   = []
gaps_over_200 = []
gaps_over_1000 = []   # تعليق > 1 ثانية (freeze واضح)

t_start    = time.time()
last_ts    = None
last_hash  = None      # نقارن hash الفريم عشان نعرف لو اتغير

while time.time() - t_start < MONITOR_SECS:
    f   = cam.get_frame()
    now = time.time()

    if f is not None:
        # hash سريع من ركن الصورة عشان نعرف لو الفريم اتغير
        h = f[0, 0, 0].item()   # بكسل واحد كافي كـ quick-check

        if h != last_hash:
            if last_ts is not None:
                gap_ms = (now - last_ts) * 1000
                if gap_ms > 200:
                    gaps_over_200.append(gap_ms)
                    print(f"    ⚠️  gap {gap_ms:.0f}ms @ t={now - t_start:.1f}s")
                if gap_ms > 1000:
                    gaps_over_1000.append(gap_ms)
            frame_times.append(now)
            last_ts   = now
            last_hash = h

    time.sleep(0.005)

cam.stop()

# ── 4. تقرير ────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("  RESULTS")
print("=" * 60)

if len(frame_times) < 2:
    print("❌ لم يُستقبل سوى فريم واحد أو أقل")
    sys.exit(1)

duration = frame_times[-1] - frame_times[0]
fps      = (len(frame_times) - 1) / duration if duration > 0 else 0
all_gaps = [(frame_times[i+1] - frame_times[i]) * 1000
            for i in range(len(frame_times)-1)]
avg_gap = sum(all_gaps) / len(all_gaps)
max_gap = max(all_gaps)

print(f"\n  Total frames   : {len(frame_times)}")
print(f"  Duration       : {duration:.1f}s")
print(f"  Average FPS    : {fps:.1f}")
print(f"  Avg gap        : {avg_gap:.0f}ms    Max gap: {max_gap:.0f}ms")
print(f"  Gaps > 200ms   : {len(gaps_over_200)}")
print(f"  Gaps > 1000ms  : {len(gaps_over_1000)}  ← freezes واضحة")

if gaps_over_1000:
    print(f"\n  أطول freezes:")
    for g in sorted(gaps_over_1000, reverse=True)[:5]:
        print(f"    {g:.0f} ms")

print()
if len(gaps_over_1000) == 0:
    print("  ✅ الكاميرا شغالة سلس — auto-recovery نجح")
elif len(gaps_over_1000) <= 2:
    print("  ⚠️  freeze نادر — auto-recovery شغال لكن ممكن تحسين")
else:
    print("  ❌ freeze متكرر — جرب USB port مختلف أو cable أقصر")

print("=" * 60)
