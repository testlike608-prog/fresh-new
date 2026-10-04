"""
tests/sim_lifecycle_test.py
Start/Stop lifecycle test بدون هاردوير:
  fake Fairino controller (XML-RPC 20003 + realtime 20004 على 127.0.0.1) + fake camera + fake AI.
بيشغّل web_server الحقيقي ويعمل كذا دورة Start/Stop (منها Stop في نص MoveJ وفي نص الـ Start)
ويتأكد إن مفيش ثريد كاميرا ولا ثريد/سوكيت كوبوت فاضل بعد كل Stop.

تشغيل:  python tests/sim_lifecycle_test.py      (الكوبوت الحقيقي مش لازم يكون متوصل)
Sim test: fake Fairino controller (XML-RPC 20003 + realtime 20004) + fake camera + fake AI.
Runs the real web_server app and exercises Start/Stop cycles.
"""
import os, sys, json, time, socket, sqlite3, tempfile, threading, asyncio
from xmlrpc.server import SimpleXMLRPCServer, SimpleXMLRPCRequestHandler
from socketserver import ThreadingMixIn

DATA = tempfile.mkdtemp(prefix="sim_")
os.environ["DATA_DIR"] = DATA
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── data files ──
json.dump({"cobot_ip": "127.0.0.1", "scan_mode": "manual", "camera_type": "opencv",
           "camera_index": 0, "AI_Agent": "groq", "ai_model": "x", "vision_test_count": 3},
          open(os.path.join(DATA, "config.json"), "w"))
db = sqlite3.connect(os.path.join(DATA, "web_point.db"))
db.execute("create table points(name text, j1 real, j2 real, j3 real, j4 real, j5 real, j6 real)")
for n in ["water1", "10kg_1", "10kg_2", "10kg_3", "ready", "cam"]:
    db.execute("insert into points values(?,1,2,3,4,5,6)", (n,))
db.commit(); db.close()
import pandas as pd
pd.DataFrame({"char": ["A"], "program": [1]}).to_excel(os.path.join(DATA, "program_mapping.xlsx"), index=False)

# ── fake controller ──
SIM = {"di0": 0, "stop": threading.Event(), "moves": 0, "aborted_moves": 0, "rt_conns": 0, "rt_open": 0}
MOVE_TIME = 1.5

class TS(ThreadingMixIn, SimpleXMLRPCServer):
    daemon_threads = True
class H(SimpleXMLRPCRequestHandler):
    def log_message(self, *a): pass
srv = TS(("127.0.0.1", 20003), requestHandler=H, allow_none=True, logRequests=False)
srv.register_function(lambda: "127.0.0.1", "GetControllerIP")
srv.register_function(lambda j: [0, 1, 2, 3, 4, 5, 6], "GetForwardKin")
def MoveJ(*a):
    SIM["stop"].clear(); SIM["moves"] += 1
    if SIM["stop"].wait(MOVE_TIME):
        SIM["aborted_moves"] += 1
        return 1   # interrupted
    return 0
srv.register_function(MoveJ, "MoveJ")
def StopMotion():
    SIM["stop"].set(); return 0
srv.register_function(StopMotion, "StopMotion")
srv.register_function(lambda *a: 0, "SetDO")
srv.register_function(lambda i, b: [0, SIM["di0"] if int(i) == 0 else 0], "GetDI")
threading.Thread(target=srv.serve_forever, daemon=True).start()

def rt_server():
    ls = socket.socket(); ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("127.0.0.1", 20004)); ls.listen(20)
    while True:
        c, _ = ls.accept(); SIM["rt_conns"] += 1
        def feed(c=c):
            SIM["rt_open"] += 1
            try:
                while True:
                    c.sendall(b"\x00" * 16); time.sleep(0.01)
            except Exception:
                pass
            finally:
                SIM["rt_open"] -= 1
        threading.Thread(target=feed, daemon=True).start()
threading.Thread(target=rt_server, daemon=True).start()

# ── fake camera + AI ──
import numpy as np
import camera_hub, ClientsClass, ai_vision
class FakeCam(camera_hub.CameraHub):
    opened = 0
    def _capture_loop(self, idx):
        FakeCam.opened += 1
        try:
            while not self._stop_event.is_set():
                self._set_frame(np.zeros((48, 64, 3), np.uint8)); time.sleep(0.02)
        finally:
            FakeCam.opened -= 1; self._clear_frame()
camera_hub.CameraHub.OpenCV = FakeCam
class FakeAI:
    def __init__(self, **kw): pass
    def run(self, image_paths):
        time.sleep(0.5); return {f"image_{i+1}": "No" for i in range(len(image_paths))}
ai_vision.WaterDetector.Groq = FakeAI

import httpx, uvicorn, web_server

def wait_state(c, want, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get("/api/state").json()
        if s["run_state"] == want:
            return s, time.time() - t0
        time.sleep(0.05)
    raise AssertionError(f"state {want} not reached; got {s['run_state']} err={s.get('last_error')}")

def leftover():
    return sorted(t.name for t in threading.enumerate()
                  if "robot_state" in t.name or t.name.startswith("camera-"))

def print(*a):
    sys.__stdout__.write(" ".join(map(str, a)) + "\n"); sys.__stdout__.flush()

def main():
    cfg = uvicorn.Config(web_server.combined_app, host="127.0.0.1", port=8765, log_level="error")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.5)
    c = httpx.Client(base_url="http://127.0.0.1:8765", timeout=30)

    for cycle in range(1, 4):
        r = c.post("/api/start"); assert r.status_code == 200, r.text
        s, dt = wait_state(c, "RUNNING"); print(f"[{cycle}] RUNNING after {dt:.2f}s conns={s['connections']}")
        time.sleep(1.8)  # homing move
        assert FakeCam.opened == 1, FakeCam.opened

        if cycle == 1:
            # full sequence: trigger DI0 → barcode → program_1
            SIM["di0"] = 1; time.sleep(2.0); SIM["di0"] = 0
            r = c.post("/api/barcode", json={"barcode": "XYZA12"}); print("  barcode:", r.status_code)
            t0 = time.time()
            while time.time() - t0 < 20:
                if c.get("/api/state").json()["stage"] == "IDLE" and SIM["moves"] >= 8: break
                time.sleep(0.1)
            print("  stage/stats:", c.get("/api/state").json()["stage"], c.get("/api/state").json()["stats"])
        if cycle == 2:
            # stop while waiting for barcode (keyboard listener / wait loop)
            SIM["di0"] = 1; time.sleep(2.2); SIM["di0"] = 0
            print("  stage before stop:", c.get("/api/state").json()["stage"])
        if cycle == 3:
            # stop in the middle of a MoveJ
            SIM["di0"] = 1; time.sleep(0.4); SIM["di0"] = 0

        t0 = time.time(); r = c.post("/api/stop"); dt = time.time() - t0
        s = c.get("/api/state").json()
        time.sleep(0.3)
        print(f"  stop → {r.status_code} {r.json()} in {dt:.2f}s; state={s['run_state']} "
              f"cam_open={FakeCam.opened} rt_open={SIM['rt_open']} leftover={leftover()} aborted={SIM['aborted_moves']}")
        assert s["run_state"] == "STOPPED"
        assert FakeCam.opened == 0
        assert not leftover(), leftover()

    # stop during STARTING
    c.post("/api/start"); time.sleep(0.05)
    r = c.post("/api/stop"); s = c.get("/api/state").json(); time.sleep(1.5)
    print("stop-during-start:", r.json(), s["run_state"], "leftover", leftover(), "cam", FakeCam.opened, "rt_open", SIM["rt_open"])
    assert s["run_state"] == "STOPPED" and not leftover() and FakeCam.opened == 0

    # robot unreachable → session error → clean STOPPED with last_error
    srv_ip = json.load(open(os.path.join(DATA, "config.json")))
    srv_ip["cobot_ip"] = "127.0.0.2"; json.dump(srv_ip, open(os.path.join(DATA, "config.json"), "w"))
    c.post("/api/start")
    t0 = time.time()
    while time.time() - t0 < 20:
        s = c.get("/api/state").json()
        if s["run_state"] == "STOPPED": break
        time.sleep(0.1)
    print("unreachable robot:", s["run_state"], s["last_error"], "cam", FakeCam.opened, "leftover", leftover())
    srv_ip["cobot_ip"] = "127.0.0.1"; json.dump(srv_ip, open(os.path.join(DATA, "config.json"), "w"))
    c.post("/api/start"); s, dt = wait_state(c, "RUNNING"); print("recovered after error: RUNNING", f"{dt:.2f}s")
    c.post("/api/stop"); print("final:", c.get("/api/state").json()["run_state"], "threads:",
                               sorted(t.name for t in threading.enumerate()))
    print("ALL OK")

main()
