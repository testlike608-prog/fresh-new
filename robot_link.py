"""
robot_link.py
-------------
Async wrapper حوالين Fairino SDK (fairino.Robot.RPC) — بيتعمل جديد مع كل Start
وبيتقفل بالكامل مع كل Stop.

ليه محتاجينه:
  - الـ SDK بيفتح socket على بورت 20004 + ثريد robot_state_routine_thread
    ومفيش طريقة نظيفة تقفلهم (CloseRPC نفسها ممكن ترجع -4 من غير ما تقفل حاجة).
  - RPC.is_conect متعرّف على مستوى الـ class → لو فشل مرة بيفضل False
    لكل الـ instances اللي بعد كده وكل الأوامر بترجع -4 في صمت.
  - xmlrpc ServerProxy مش thread-safe → كل الأوامر بتعدي على lock واحد.
  - StopMotion لازم يشتغل والـ MoveJ لسه blocking → بنستخدم proxy منفصل.

API (كله async ما عدا stop_motion_now):
    robot = RobotLink(ip)
    await robot.connect()
    await robot.move_j(joints, vel=100, acc=100)
    await robot.set_do(id, value)
    di = await robot.get_di(id)
    robot.stop_motion_now()      # sync — آمن من أي thread حتى لو MoveJ شغالة
    await robot.close()
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import xmlrpc.client

from fairino.Robot import RPC

log = logging.getLogger("robot_link")

XMLRPC_PORT = 20003


class RobotConnectionError(RuntimeError):
    pass


class _TimeoutTransport(xmlrpc.client.Transport):
    """Transport بـ timeout — عشان StopMotion متعلقش لو الكنترولر مش بيرد."""

    def __init__(self, timeout: float):
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


class RobotLink:
    def __init__(self, ip: str):
        self.ip = ip
        self._rpc: RPC | None = None
        self._cmd_lock = threading.Lock()    # يسلسل كل أوامر الـ xmlrpc
        self._closed = False                 # بعد close() مفيش رجوع — Start بيعمل link جديد

    # ── State ────────────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._rpc is not None and not self._closed

    @property
    def rpc(self) -> RPC | None:
        return self._rpc

    # ── Connect ──────────────────────────────────────────────────────────────

    def _connect_blocking(self):
        # ماسكين الـ lock طول الاتصال: لو Stop جه في النص، close() هتستنى
        # لحد ما الـ RPC يتعمل وبعدين تقفله — مفيش ثريد/سوكيت يتسرّب.
        with self._cmd_lock:
            if self._closed:
                return
            # الـ flag ده class-level في الـ SDK — لازم يترجع True قبل كل اتصال جديد
            RPC.is_conect = True
            rpc = RPC(self.ip)
            if RPC.is_conect is False:
                self._teardown_rpc(rpc)
                raise RobotConnectionError(f"Robot {self.ip} not reachable (XML-RPC)")
            if not getattr(rpc, "sock_cli_state_state", False):
                log.warning(f"[Robot] realtime port 20004 not connected on {self.ip} — "
                            "GetDI will fall back to XML-RPC")
            self._rpc = rpc

    async def connect(self):
        log.info(f"[Robot] connecting to {self.ip} ...")
        await asyncio.to_thread(self._connect_blocking)
        if self._rpc is None:
            raise RobotConnectionError("Robot connect aborted")
        log.info(f"[Robot] connected ({self.ip})")

    # ── Commands ─────────────────────────────────────────────────────────────

    def _call_blocking(self, name: str, *args, **kwargs):
        with self._cmd_lock:
            rpc = self._rpc
            if rpc is None or self._closed:
                raise RobotConnectionError("Robot link is closed")
            return getattr(rpc, name)(*args, **kwargs)

    async def call(self, name: str, *args, **kwargs):
        """ينفّذ أي method من الـ SDK في worker thread (من غير ما يوقف الـ event loop)."""
        return await asyncio.to_thread(self._call_blocking, name, *args, **kwargs)

    async def move_j(self, joint_pos, tool: int = 0, user: int = 1,
                     vel: float = 100, acc: float = 100):
        err = await self.call("MoveJ", joint_pos=joint_pos, tool=tool, user=user,
                              vel=vel, acc=acc)
        if err not in (0, None):
            log.error(f"[Robot] MoveJ returned error code {err}")
        return err

    async def set_do(self, do_id, value: int):
        return await self.call("SetDO", int(do_id), int(value))

    def _get_di_blocking(self, di_id: int) -> int:
        with self._cmd_lock:
            rpc = self._rpc
            if rpc is None or self._closed:
                raise RobotConnectionError("Robot link is closed")
            pkg = rpc.robot_state_pkg
            # قبل أول packet من بورت 20004 الـ SDK بيسيب الـ class نفسه
            # (ده سبب error: '_ctypes.CField' & int اللي في اللوج)
            if pkg is not None and not isinstance(pkg, type):
                ret = rpc.GetDI(di_id, 0)
            else:
                ret = rpc.robot.GetDI(di_id, 0)     # fallback: XML-RPC مباشرة
        if isinstance(ret, (list, tuple)):
            if len(ret) > 1:
                if ret[0] not in (0, None):
                    raise RobotConnectionError(f"GetDI error code {ret[0]}")
                return int(ret[1])
            return int(ret[0])
        return int(ret) if ret is not None else 0

    async def get_di(self, di_id) -> int:
        return await asyncio.to_thread(self._get_di_blocking, int(di_id))

    # ── Emergency stop of current motion ─────────────────────────────────────

    def stop_motion_now(self, timeout: float = 2.0) -> bool:
        """
        يبعت StopMotion على proxy منفصل — مش محتاج الـ _cmd_lock،
        فبيشتغل حتى لو MoveJ blocking في ثريد تاني. الـ MoveJ بترجع بعدها.
        """
        if self._rpc is None:
            return False
        try:
            proxy = xmlrpc.client.ServerProxy(
                f"http://{self.ip}:{XMLRPC_PORT}",
                transport=_TimeoutTransport(timeout),
            )
            err = proxy.StopMotion()
            log.warning(f"[Robot] StopMotion sent (ret={err})")
            return True
        except Exception as e:
            log.error(f"[Robot] StopMotion failed: {e}")
            return False

    # ── Close ────────────────────────────────────────────────────────────────

    @staticmethod
    def _find_state_threads(rpc: RPC):
        out = []
        for t in threading.enumerate():
            target = getattr(t, "_target", None)
            if getattr(target, "__self__", None) is rpc:
                out.append(t)
        return out

    @classmethod
    def _teardown_rpc(cls, rpc: RPC, join_timeout: float = 5.0):
        """يقفل socket الـ 20004 وثريد الـ state بتاع الـ SDK بالقوة."""
        threads = cls._find_state_threads(rpc)

        # 1) أي loop في الثريد يشوف إنه لازم يخرج
        rpc.closeRPC_state = True
        rpc.robot_realstate_exit = True
        try:
            rpc.stop_event.set()
        except Exception:
            pass
        # 2) reconnect() في الـ SDK بيلف لحد 1000 مرة × 2s ومش بيبص على أي flag
        #    → نخليه يرجع فوراً
        rpc.reconnect = lambda: False
        rpc.connect_to_robot = lambda: True
        # 3) اقفل الـ socket عشان recv يطلع فوراً
        sock = getattr(rpc, "sock_cli_state", None)
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

        deadline = time.time() + join_timeout
        for t in threads:
            t.join(timeout=max(0.1, deadline - time.time()))
            if t.is_alive():
                log.warning(f"[Robot] state thread '{t.name}' still alive after close")

        rpc.robot = None
        rpc.sock_cli_state = None
        RPC.is_conect = True      # جاهز للاتصال الجاي

    def _close_blocking(self, lock_timeout: float):
        # استنى أي أمر شغال (MoveJ مثلاً — بيرجع بعد StopMotion)
        got = self._cmd_lock.acquire(timeout=lock_timeout)
        if not got:
            log.warning("[Robot] a command is still running — closing anyway")
        try:
            self._closed = True
            rpc, self._rpc = self._rpc, None
            if rpc is not None:
                self._teardown_rpc(rpc)
        finally:
            if got:
                self._cmd_lock.release()

    async def close(self, lock_timeout: float = 10.0):
        self._closed = True       # أي connect لسه شغال هيشوفه
        await asyncio.to_thread(self._close_blocking, lock_timeout)
        log.info(f"[Robot] connection closed ({self.ip})")
