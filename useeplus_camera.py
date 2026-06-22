"""
useeplus_camera.py
------------------
Python driver for the useeplus endoscope camera (SuperCamera)
Vendor ID: 0x2CE3 | Product ID: 0x3828

Requirements:
    pip install pyusb
    + Install libusb via Zadig (see README below)

Usage:
    python useeplus_camera.py              # take 1 photo -> image_000.jpg
    python useeplus_camera.py --count 5   # take 5 photos
    python useeplus_camera.py --stream    # live preview (requires opencv: pip install opencv-python)

----------------------------------------------------------------------
SETUP (Windows – one-time):
1. Download Zadig from https://zadig.akeo.ie/
2. Plug in the camera
3. In Zadig: Options -> List All Devices -> select "SuperCamera" or
   the device with ID 2CE3/3828 -> choose "WinUSB" -> Install Driver
4. pip install pyusb
----------------------------------------------------------------------
"""

import usb.core
import usb.util
import usb.backend.libusb1
import struct
import time
import argparse
import os
import sys
from datetime import datetime

# ── Device constants ───────────────────────────────────────────────
VENDOR_ID    = 0x2CE3
PRODUCT_ID   = 0x3828
INTERFACE    = 1          # interface number
ALT_SETTING  = 1          # alternate setting to activate
EP_OUT       = 0x01       # bulk OUT endpoint
EP_IN        = 0x81       # bulk IN endpoint

# Init command sent to the camera to start streaming
CONNECT_CMD  = bytes([0xBB, 0xAA, 0x05, 0x00, 0x00])

# Packet header signature
HEADER_MAGIC = bytes([0xAA, 0xBB, 0x07])
HEADER_SIZE  = 12         # full proprietary header length

# JPEG markers
JPEG_SOI = bytes([0xFF, 0xD8])   # Start of Image
JPEG_EOI = bytes([0xFF, 0xD9])   # End of Image

READ_TIMEOUT  = 5000   # ms
WRITE_TIMEOUT = 5000   # ms
CHUNK_SIZE    = 64 * 1024  # 64 KB per USB bulk read


# ── Camera class ───────────────────────────────────────────────────

class UseePlusCamera:
    def __init__(self):
        self.dev = None
        self._buffer = bytearray()   # accumulation buffer for raw data

    # ── Connect ────────────────────────────────────────────────────
    def connect(self):
        """Find the USB device and initialise it."""
        # Try to load libusb backend automatically (works after: pip install libusb)
        backend = None
        try:
            import libusb
            backend = usb.backend.libusb1.get_backend(find_library=lambda x: libusb.dll._name)
        except Exception:
            pass  # fall back to system-installed libusb

        self.dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID, backend=backend)
        if self.dev is None:
            raise RuntimeError(
                "Camera not found! Make sure:\n"
                "  1. The camera is plugged in\n"
                "  2. You installed WinUSB via Zadig\n"
                "  3. VID=0x2CE3 / PID=0x3828 matches your device"
            )

        print(f"Found camera: {self.dev.manufacturer} {self.dev.product}")

        # Detach kernel driver if needed (Linux only; harmless on Windows)
        try:
            if self.dev.is_kernel_driver_active(INTERFACE):
                self.dev.detach_kernel_driver(INTERFACE)
        except (NotImplementedError, usb.core.USBError):
            pass

        # Set configuration
        self.dev.set_configuration()

        # Switch to alternate setting 1 on interface 1
        self.dev.set_interface_altsetting(interface=INTERFACE, alternate_setting=ALT_SETTING)
        print(f"Interface {INTERFACE} set to alt-setting {ALT_SETTING}")

        # Send init / connect command
        written = self.dev.write(EP_OUT, CONNECT_CMD, WRITE_TIMEOUT)
        print(f"Sent init command ({written} bytes): {CONNECT_CMD.hex(' ').upper()}")

    # ── Disconnect ─────────────────────────────────────────────────
    def disconnect(self):
        if self.dev:
            try:
                # Reset to alt-setting 0
                self.dev.set_interface_altsetting(interface=INTERFACE, alternate_setting=0)
            except Exception:
                pass
            usb.util.dispose_resources(self.dev)
            self.dev = None
            print("Camera disconnected.")

    # ── Read raw USB data ──────────────────────────────────────────
    def _read_chunk(self):
        """Read one bulk chunk from the camera. Returns raw bytes or None on timeout."""
        try:
            data = self.dev.read(EP_IN, CHUNK_SIZE, READ_TIMEOUT)
            return bytes(data)
        except usb.core.USBError as e:
            if "timed out" in str(e).lower():
                return None
            raise

    # ── Parse proprietary packets ──────────────────────────────────
    def _feed(self, raw: bytes):
        """
        Strip the 12-byte proprietary header (AA BB 07 ...) from each packet
        and append the payload to the internal accumulation buffer.
        """
        if len(raw) >= 3 and raw[:3] == HEADER_MAGIC:
            payload = raw[HEADER_SIZE:]   # skip header
        else:
            # No recognised header – append everything
            # (some packets carry continuation data without a header)
            payload = raw

        self._buffer.extend(payload)

    # ── Extract JPEG images ────────────────────────────────────────
    def _extract_jpegs(self):
        """
        Scan the accumulation buffer for complete JPEG images
        (FF D8 ... FF D9). Returns a list of complete JPEG bytes objects
        and removes them from the buffer.
        """
        images = []
        buf = self._buffer
        pos = 0

        while True:
            soi = buf.find(JPEG_SOI, pos)
            if soi == -1:
                break
            eoi = buf.find(JPEG_EOI, soi + 2)
            if eoi == -1:
                break           # incomplete frame – wait for more data
            end = eoi + 2
            images.append(bytes(buf[soi:end]))
            pos = end

        # Remove consumed data; keep tail (might be start of next frame)
        if pos > 0:
            self._buffer = buf[pos:]

        return images

    # ── Capture N frames ──────────────────────────────────────────
    def capture(self, count=1, output_dir=".", prefix="image"):
        """
        Capture `count` JPEG images and save them to `output_dir`.
        Returns list of saved file paths.
        """
        os.makedirs(output_dir, exist_ok=True)
        saved = []
        img_index = 0
        no_data_streak = 0
        MAX_NO_DATA = 20   # give up after 20 consecutive empty reads

        print(f"Capturing {count} image(s)...")

        while img_index < count:
            chunk = self._read_chunk()
            if chunk is None or len(chunk) == 0:
                no_data_streak += 1
                if no_data_streak >= MAX_NO_DATA:
                    print("No data received – stopping.")
                    break
                continue

            no_data_streak = 0
            self._feed(chunk)

            for jpeg_data in self._extract_jpegs():
                path = os.path.join(output_dir, f"{prefix}_{img_index:03d}.jpg")
                with open(path, "wb") as f:
                    f.write(jpeg_data)
                print(f"  Saved: {path}  ({len(jpeg_data):,} bytes)")
                saved.append(path)
                img_index += 1
                if img_index >= count:
                    break

        return saved

    # ── Live preview (requires opencv) ────────────────────────────
    def stream_preview(self):
        """Show a live preview window. Press 'q' to quit, 's' to save a snapshot."""
        try:
            import cv2
            import numpy as np
        except ImportError:
            print("opencv-python not installed. Run: pip install opencv-python")
            return

        print("Live preview – press 'q' to quit, 's' to save snapshot")
        snap_count = 0
        no_data_streak = 0

        while True:
            chunk = self._read_chunk()
            if chunk is None or len(chunk) == 0:
                no_data_streak += 1
                if no_data_streak > 50:
                    print("Stream lost.")
                    break
                continue

            no_data_streak = 0
            self._feed(chunk)

            for jpeg_data in self._extract_jpegs():
                arr = np.frombuffer(jpeg_data, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is None:
                    continue
                cv2.imshow("useeplus camera", frame)
                key = cv2.waitKey(1) & 0xFF
                if key == ord('q'):
                    cv2.destroyAllWindows()
                    return
                if key == ord('s'):
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    fname = f"snapshot_{ts}.jpg"
                    cv2.imwrite(fname, frame)
                    print(f"Snapshot saved: {fname}")
                    snap_count += 1


# ── CLI entry point ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="useeplus endoscope camera – Python driver for Windows"
    )
    parser.add_argument("--count",  type=int, default=1,
                        help="Number of photos to capture (default: 1)")
    parser.add_argument("--output", type=str, default=".",
                        help="Output directory for saved images (default: current dir)")
    parser.add_argument("--prefix", type=str, default="image",
                        help="Filename prefix (default: image)")
    parser.add_argument("--stream", action="store_true",
                        help="Show live preview window (requires opencv-python)")
    args = parser.parse_args()

    cam = UseePlusCamera()
    try:
        cam.connect()

        if args.stream:
            cam.stream_preview()
        else:
            files = cam.capture(count=args.count, output_dir=args.output, prefix=args.prefix)
            if files:
                print(f"\nDone. {len(files)} image(s) saved.")
            else:
                print("\nNo images captured.")
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        cam.disconnect()


if __name__ == "__main__":
    main()
