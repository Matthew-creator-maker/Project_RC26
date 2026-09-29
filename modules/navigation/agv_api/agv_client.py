"""
仙知 AGV 实时通信客户端
agv_client.py

【解决的问题】
  1. AGV 会主动推送消息（导航完成、报警等），不只是被动响应
  2. 收和发必须并行，不能互相阻塞

【设计】
  - 持久 TCP 连接，程序运行期间不重复断连
  - 后台接收线程专职"收"，主线程专职"发"
  - 回调机制：按 cmd_id 分发给注册方

【核心方法】
  send()    → 发完立即返回（控制指令）
  request() → 发完阻塞等待响应（查询指令）
  on()      → 注册持久回调（监听 AGV 主动推送）
"""

import queue
import threading
from socket import AF_INET, SOCK_STREAM, SOL_SOCKET, SO_KEEPALIVE, SHUT_RDWR, socket
from typing import Optional

from .agv_protocol import AGVConfig, build_frame, parse_frame, recv_full_frame


class AGVClient:
    """
    持久连接的 AGV 通信客户端

        主线程                        后台接收线程
        ─────────────────────         ──────────────────────────────
        send(...)       ──发──→       （发完立即返回）
        request(...)    ──发──→       收到响应 → 放入 Queue → 解除阻塞
        on("0BEC", fn)  ──注册──→     收到推送 → 自动调用 fn(result)
    """

    def __init__(self, config: AGVConfig = None):
        self._cfg  = config
        self._sock: Optional[socket] = None
        self._running = False
        self._recv_thread: Optional[threading.Thread] = None

        # { cmd_id: [callback, ...] }  —— on() 注册的持久监听
        self._callbacks: dict[str, list] = {}

        # { cmd_id: Queue }  —— request() 正在等待的响应
        self._pending: dict[str, queue.Queue] = {}
        self._pending_lock = threading.Lock()

        # 没有任何处理方消费的消息，保留方便调试
        self._unhandled: list[dict] = []

    # ── 连接管理 ──────────────────────────────────────────────────────────

    def connect(self) -> None:
        """建立 TCP 持久连接，启动后台接收线程"""
        self._sock = socket(AF_INET, SOCK_STREAM)
        self._sock.setsockopt(SOL_SOCKET, SO_KEEPALIVE, 1)
        self._sock.connect((self._cfg.host, self._cfg.port))
        self._running = True
        self._recv_thread = threading.Thread(
            target=self._recv_loop,
            name=f"AGVRecv-{self._cfg.port}",
            daemon=False,   # 非守护线程，配合 disconnect() 干净退出
        )
        self._recv_thread.start()
        print(f"[AGV:{self._cfg.port}] 已连接")

    def disconnect(self) -> None:
        """断开连接，等待接收线程完全退出"""
        self._running = False
        if self._sock:
            try:
                self._sock.shutdown(SHUT_RDWR)  # 立即解除 recv() 阻塞
            except OSError:
                pass
            self._sock.close()
            self._sock = None
        if self._recv_thread and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=3)
        print(f"[AGV:{self._cfg.port}] 已断开")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

    # ── 后台接收线程 ──────────────────────────────────────────────────────

    def _recv_loop(self) -> None:
        """持续接收 AGV 数据帧，解析后分发"""
        while self._running:
            try:
                raw = recv_full_frame(self._sock)
                self._dispatch(parse_frame(raw))
            except ConnectionError:
                print(f"[AGV:{self._cfg.port}] 连接断开，接收线程退出")
                self._running = False
                break
            except Exception as e:
                print(f"[AGV:{self._cfg.port}] 接收错误: {e}")

    def _dispatch(self, result: dict) -> None:
        """
        将一条响应分发给对应的处理方

        优先级：
          1. request() 的等待队列（解除阻塞）
          2. on() 注册的持久回调
          3. 以上均无 → 存入 _unhandled（便于调试）

        注意：1 和 2 可同时触发，不互斥。
        """
        cmd_id  = result["cmd_id"]
        handled = False

        with self._pending_lock:
            q = self._pending.get(cmd_id)
        if q:
            q.put(result)
            handled = True

        for cb in self._callbacks.get(cmd_id, []):
            try:
                cb(result)
                handled = True
            except Exception as e:
                print(f"[AGV] 回调异常 cmd={cmd_id}: {e}")

        if not handled:
            self._unhandled.append(result)
            print(f"[AGV] 未处理响应 cmd={cmd_id} (十进制={result['cmd_dec']})")

    # ── 发送 ──────────────────────────────────────────────────────────────

    def send(self, cmd_id: str, data: Optional[dict] = None) -> None:
        """
        发送指令，不等响应（适合控制类指令）

        示例：
            client.send("0BEB", {"source_id": "A1", "id": "B2", "task_id": "..."})
            client.send("0BB9")   # 暂停
        """
        self._sock.send(build_frame(cmd_id, data))

    def request(
        self,
        cmd_id:  str,
        data:    Optional[dict] = None,
        res_cmd: str         = None,
        timeout: float       = 5.0,
    ) -> Optional[dict]:
        """
        发送指令并阻塞等待响应（适合查询类指令）

        【响应 ID 自动推算】
          仙知协议：响应 ID = 请求 ID + 10000
          如 0x03E8 (1000) → 响应 0x2AF8 (11000)，无需手动传 res_cmd

        示例：
            info = client.request("03E8")   # 查询机器人信息
        """
        if res_cmd is None:
            res_cmd = format(int(cmd_id, 16) + 10000, "04X")

        q: queue.Queue = queue.Queue()
        with self._pending_lock:
            self._pending[res_cmd] = q

        try:
            self._sock.send(build_frame(cmd_id, data))
            return q.get(timeout=timeout)
        except queue.Empty:
            print(f"[AGV] 响应超时 cmd={res_cmd} ({timeout}s)")
            return None
        finally:
            with self._pending_lock:
                self._pending.pop(res_cmd, None)

    # ── 回调注册 ──────────────────────────────────────────────────────────

    def on(self, cmd_id: str, callback) -> None:
        """
        注册持久回调：每次收到该 cmd_id 的响应自动触发

        适合监听 AGV 主动推送（导航完成、报警、状态变化等）。

        示例：
            def on_nav_done(result):
                print("导航完成：", result["data"].get("task_id"))

            client.on("0BEC", on_nav_done)
        """
        self._callbacks.setdefault(cmd_id, []).append(callback)

    def off(self, cmd_id: str, callback=None) -> None:
        """
        取消回调注册

        :param callback: 传入函数则只取消该函数；不传则取消该 cmd_id 的全部回调
        """
        if callback is None:
            self._callbacks.pop(cmd_id, None)
        else:
            cbs = self._callbacks.get(cmd_id, [])
            self._callbacks[cmd_id] = [c for c in cbs if c != callback]