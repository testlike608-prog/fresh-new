# ════════════════════════════════════════════════════════════════
#  Water Inspection System — Dockerfile
#  Base: python:3.12-slim
#  Build: docker build -t water-inspection .
#  Run:   docker compose up
# ════════════════════════════════════════════════════════════════

FROM python:3.12-slim

# ── System dependencies ──────────────────────────────────────────
# libgl1 + libglib2.0-0 : OpenCV
# libusb-1.0-0          : UseePlus USB camera (pyusb)
# libudev-dev           : keyboard / evdev device access
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libusb-1.0-0 \
        libudev-dev \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies (cached layer) ──────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ─────────────────────────────────────────────
COPY *.py          ./
COPY static/       ./static/
COPY fairino/      ./fairino/
# AI super-resolution models (كبيرة — طبقة منفصلة)
COPY *.pb          ./

# ── Data directory (سيُعلَّق عليه Volume من الـ Host) ────────────
# بننشئه هنا بس عشان الـ permissions تبقى صح
RUN mkdir -p /app/data/results \
             /app/data/captures \
             /app/data/enhanced_images \
             /app/data/logs

# ── Environment ──────────────────────────────────────────────────
# DATA_DIR: كل الملفات الديناميكية (config, Excel, صور, logs, DB)
ENV DATA_DIR=/app/data
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# ── Volume declaration ───────────────────────────────────────────
# الـ Host بيـ mount فولدر هنا — كل حاجة بتتحفظ على الـ Host
VOLUME ["/app/data"]

# ── Port ─────────────────────────────────────────────────────────
EXPOSE 8000

# ── Healthcheck ──────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/state')" \
    || exit 1

# ── Startup ──────────────────────────────────────────────────────
CMD ["python", "-u", "web_server.py"]
