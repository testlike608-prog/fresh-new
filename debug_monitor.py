"""
debug_monitor.py
----------------
مراقب حالة دوري للبرنامج.

كل snapshot بيتكتب في ملف logs/monitor.log (rotating)، ومش بيطبع في
الكونسول إلا سطر واحد قصير "tick #N saved" عشان مايغطيش على باقي اللوج.

لو عايز تشوف الـ snapshots وقت ما تطلب:
  - افتح ملف logs/monitor.log في أي editor
  - أو ادي: tail -f logs/monitor.log
  - أو ادي debug_monitor.get_last_snapshot() من الكود
"""

import os
import sys
import time
import threading
import queue


_monitor_started = False
_monitor_lock = threading.Lock()
_monitor_stop = threading.Event()
_monitor_thread = None

# آخر snapshot في الذاكرة عشان تقدر تجيبه فوراً من غير ما تقرا الملف
_last_snapshot_text = ""
_last_snapshot_time = 0.0
_last_snapshot_lock = threading.Lock()


def _peek_queue(q, limit=5):
    try:
        with q.mutex:
            items = list(q.queue)
        return items[-limit:], len(items)
    except Exception as e:
        return [f"<peek error: {e}>"], -1


def _format_value(v, max_len=80):
    s = repr(v)
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s


def _get_file_logger():
    """
    logger مخصص للـ monitor — بيكتب في ملف monitor.log فقط
    (مفيش StreamHandler عشان مايغطيش على الكونسول).
    """
    import logging
    from logging.handlers import RotatingFileHandler

    logger = logging.getLogger("debug_monitor")
    if logger.handlers:
        return logger

    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "monitor.log")

    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    fmt = logging.Formatter("%(message)s")

    fh = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


def get_log_file_path():
    """يرجع الـ path الكامل لملف monitor.log"""
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "logs", "monitor.log"
    )


def get_last_snapshot():
    """يرجع آخر snapshot من الذاكرة (نص + timestamp)."""
    with _last_snapshot_lock:
        return _last_snapshot_text, _last_snapshot_time


def snapshot(app_ref=None):
    """يرجع نص بيوصف حالة البرنامج في اللحظة دي. مفيد للـ tests."""
    lines = []
    lines.append("=" * 70)
    lines.append(f"[MONITOR] snapshot @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("-" * 70)

    try:
        import scanner
        q_items, q_size = _peek_queue(scanner.queue_barcode)
        lines.append(f"scanner.queue_barcode      size = {q_size}")
        lines.append(f"  last items (up to 5)     = {q_items}")
        lines.append(f"scanner.flag_barcode       = {scanner.flag_barcode}")
        lines.append(f"scanner.last_barcode       = {_format_value(scanner.last_barcode)}")
        lines.append(f"scanner._recorded_keys     = {_format_value(scanner._recorded_keys)}")
        lines.append(f"scanner._listener_started  = {scanner._listener_started}")
    except Exception as e:
        lines.append(f"scanner: <import/read failed: {e}>")

    if app_ref is not None:
        lines.append("-" * 70)
        for attr_name in dir(app_ref):
            if attr_name.startswith("_"):
                continue
            try:
                attr = getattr(app_ref, attr_name)
            except Exception:
                continue

            if isinstance(attr, queue.Queue):
                items, size = _peek_queue(attr)
                lines.append(f"app.{attr_name:<22} size = {size}, last = {items}")

            for sub in ("shared_queue", "receive_queue"):
                try:
                    sub_q = getattr(attr, sub, None)
                except Exception:
                    sub_q = None
                if isinstance(sub_q, queue.Queue):
                    items, size = _peek_queue(sub_q)
                    lines.append(f"app.{attr_name}.{sub:<14} size = {size}, last = {items}")

            try:
                connected = getattr(attr, "connected", None)
                ip = getattr(attr, "ip", None)
                port = getattr(attr, "port", None)
                if connected is not None and ip is not None:
                    state = "CONNECTED" if connected else "DISCONNECTED"
                    lines.append(f"app.{attr_name:<22} {ip}:{port}  {state}")
            except Exception:
                pass

            clients = getattr(attr, "clients", None)
            if isinstance(clients, list):
                lines.append(f"app.{attr_name}.clients         count = {len(clients)}")

    lines.append("-" * 70)
    alive = sorted(t.name for t in threading.enumerate())
    lines.append(f"threads ({len(alive)}): {alive}")
    lines.append("=" * 70)

    return "\n".join(lines)


def _monitor_loop(interval, app_ref, verbose_console):
    """
    اللوب اللي بيكتب snapshot في الملف كل فترة.
    :param verbose_console: لو True يطبع الـ snapshot كامل في الكونسول.
                            الافتراضي False = سطر واحد كل 10 ticks بس.
    """
    global _last_snapshot_text, _last_snapshot_time

    log = _get_file_logger()
    counter = 0
    log_path = get_log_file_path()
    msg = "[DEBUG MONITOR] started - interval=" + str(interval) + "s - writing to " + log_path
    print(msg, flush=True)

    while not _monitor_stop.is_set():
        try:
            counter += 1
            now = time.time()
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
            header = (
                "\n" + ("#" * 70) + "\n"
                + "# tick #" + str(counter) + "  @ " + ts + "\n"
                + ("#" * 70)
            )
            snap = snapshot(app_ref)
            full_text = header + "\n" + snap

            log.debug(full_text)

            with _last_snapshot_lock:
                _last_snapshot_text = full_text
                _last_snapshot_time = now

            if verbose_console:
                print(full_text, flush=True)
            else:
                # سطر قصير كل 10 ticks (أول واحد دايماً بيتطبع)
                if counter == 1 or counter % 10 == 0:
                    n_threads = len(threading.enumerate())
                    line = (
                        "[MONITOR] tick #" + str(counter)
                        + " saved -> " + os.path.basename(log_path)
                        + " (threads=" + str(n_threads) + ")"
                    )
                    print(line, flush=True)
        except Exception as e:
            print("[DEBUG MONITOR] error: " + str(e), file=sys.stderr, flush=True)

        if _monitor_stop.wait(timeout=interval):
            break

    print("[DEBUG MONITOR] stopped after " + str(counter) + " ticks - log at " + log_path, flush=True)


def start(app_ref=None, interval=None, force=False, verbose_console=False):
    """
    يشغّل المونيتور في thread منفصل.

    :param app_ref: instance من App عشان نراقب الكيوز اللي جواه.
    :param interval: ثواني بين كل snapshot. لو None ياخدها من DEBUG_INTERVAL env (افتراضي 2.0).
    :param force: لو True يشغّل المونيتور حتى لو DEBUG مش في environment.
    :param verbose_console: لو True يطبع كل snapshot في الكونسول كامل (الـ behavior القديم).
                            الافتراضي False = يطبع سطر واحد قصير كل 10 ticks بس،
                            وكل التفاصيل بتروح لملف logs/monitor.log
    """
    global _monitor_started, _monitor_thread

    if not force and not is_enabled():
        return None

    if interval is None:
        try:
            interval = float(os.environ.get("DEBUG_INTERVAL", "2.0"))
        except ValueError:
            interval = 2.0

    with _monitor_lock:
        if _monitor_started:
            return _monitor_thread
        _monitor_started = True
        _monitor_stop.clear()

    _monitor_thread = threading.Thread(
        target=_monitor_loop,
        args=(interval, app_ref, verbose_console),
        name="DebugMonitor",
        daemon=True,
    )
    _monitor_thread.start()
    return _monitor_thread


def stop():
    """إيقاف المونيتور."""
    global _monitor_started
    _monitor_stop.set()
    with _monitor_lock:
        _monitor_started = False


def is_enabled():
    """يرجع True لو DEBUG=1 في الـ environment."""
    return os.environ.get("DEBUG", "").strip() in ("1", "true", "True", "yes")
