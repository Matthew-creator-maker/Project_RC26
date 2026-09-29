"""
AGV 多端口客户端 Demo
api_demo.py

【架构】

  主线程                          客户端线程
  ────────────────────────        ──────────────────────────────────────
  send() / query()                从 cmd_queue 取出指令 → 找对应客户端 → 发送
    └─ 放入 cmd_queue  ──→        收到 query 响应 → response_queue
  query()  ←─ response_queue ←── 收到 AGV 推送  → push_queue
  poll()   ←─ push_queue    ←──

  【两个独立队列，互不干扰】
    response_queue：仅存 query() 的响应，poll() 不会误取
    push_queue：仅存 AGV 主动推送，query() 不会误取

【停止流程】
  stop()
    ├─ 并行 disconnect 所有 AGVClient（ThreadPoolExecutor）
    ├─ 向 cmd_queue 放 None（通知客户端线程退出）
    └─ join 客户端线程
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional, Union

from .agv_protocol import AGVConfig
from .agv_client import AGVClient


# ── 端口配置 ──────────────────────────────────────────────────────────────

#如果需要使用其它端口，在这里添加即可
PORT_CONFIG: dict[int, AGVConfig] = {
    19204: AGVConfig(port=19204),   # 状态
    19205: AGVConfig(port=19205),   # 控制
    19206: AGVConfig(port=19206),   # 导航
    19207: AGVConfig(port=19207),   # 配置
    19210: AGVConfig(port=19210),   # 其它
    19301: AGVConfig(port=19301),   # 推送
}

# AGV 主动推送的 cmd_id（收到后放入 push_queue）
PUSH_CMD_IDS = ["4B65"]

# 推送订阅配置：连接后自动发送订阅请求，AGV 才会开始推送数据
# 参考 rec.py 中的订阅帧（cmd_id=2454，即十进制 9300）
PUSH_SUBSCRIBE = {
    19301: {
        "cmd_id": "2454",
        "data": {
            "interval": 500,
            "included_fields": [
                "x", "y", "angle",
                 
            ]
        }
    }
}


# ── 消息类型 ──────────────────────────────────────────────────────────────

@dataclass
class Cmd:
    """主线程 → 客户端线程 的指令"""
    port:   int
    cmd_id: str
    data:   Optional[dict] = None
    wait:   bool = False            # True = query，False = send


@dataclass
class Result:
    """客户端线程 → 主线程 的结果"""
    port:     int
    cmd_id:   str
    response: Optional[dict] = None
    error:    Optional[str] = None

    @property
    def ok(self) -> bool:
        """响应是否成功（有 response 且无 error）"""
        return self.error is None and self.response is not None


# ── 客户端线程 ────────────────────────────────────────────────────────────

class AGVClientThread:

    def __init__(
        self,
        cmd_queue:      queue.Queue[Union[Cmd, None]],
        response_queue: queue.Queue[Result],
        push_queue:     queue.Queue[Result],
    ):
        self._cmd_queue      = cmd_queue
        self._response_queue = response_queue  # 仅存 query() 的响应
        self._push_queue     = push_queue      # 仅存 AGV 主动推送
        self._clients: dict[int, AGVClient] = {}
        self._thread:  Optional[threading.Thread] = None
        self._running = False
        self._ready   = threading.Event()      # 所有端口连接完成后 set

    # ── 启动 / 停止 ───────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name="AGVClientThread",
            daemon=False,
        )
        self._thread.start()
        print("[客户端线程] 已启动")

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """阻塞直到所有端口连接就绪，返回是否在超时内完成"""
        return self._ready.wait(timeout=timeout)

    def stop(self) -> None:
        """
        并行断开所有客户端，再通知客户端线程退出

        【并行 disconnect 的原因】
          串行 join 4 个线程最坏需要 4×3=12 秒，
          并行后总耗时 = 最慢那个，通常 < 1 秒。

        【为何不在 _run() 里 disconnect】
          stop() 和 _run() 同时操作同一客户端会产生竞争条件，
          导致 join() 互相等待，程序卡死。
          由 stop() 统一负责 disconnect，_run() 只负责退出循环。
        """
        with ThreadPoolExecutor(max_workers=max(len(self._clients), 1)) as pool:
            futs = {
                pool.submit(self._safe_disconnect, port, client): port
                for port, client in self._clients.items()
            }
            for fut in as_completed(futs, timeout=5):
                pass   # 异常已在 _safe_disconnect 内处理

        self._running = False
        self._cmd_queue.put(None)   # 退出信号

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    @staticmethod
    def _safe_disconnect(port: int, client: AGVClient) -> None:
        try:
            client.disconnect()
        except Exception as e:
            print(f"[客户端线程] 端口 {port} 断开异常: {e}")

    # ── 线程主函数 ────────────────────────────────────────────────────────

    def _run(self) -> None:
        self._connect_all()
        self._ready.set()   # 通知 wait_ready() 连接已就绪

        while self._running:
            try:
                cmd = self._cmd_queue.get(timeout=1)
            except queue.Empty:
                continue
            if cmd is None:
                break
            self._handle_cmd(cmd)

        print("[客户端线程] 已退出")

    # ── 并行连接所有端口 ──────────────────────────────────────────────────

    def _connect_all(self) -> None:
        lock = threading.Lock()

        def connect_one(port: int, config: AGVConfig) -> None:
            try:
                client = AGVClient(config)
                client.connect()
                for cmd_id in PUSH_CMD_IDS:
                    client.on(cmd_id, self._make_push_callback(port))
                # 发送推送订阅请求（如端口19301需要先订阅才能收到推送）
                if port in PUSH_SUBSCRIBE:
                    sub = PUSH_SUBSCRIBE[port]
                    client.send(sub["cmd_id"], sub["data"])
                    print(f"[客户端线程] 端口 {port} 已发送推送订阅请求")
                with lock:
                    self._clients[port] = client
                print(f"[客户端线程] 端口 {port} 连接成功")
            except Exception as e:
                print(f"[客户端线程] 端口 {port} 连接失败: {e}")

        with ThreadPoolExecutor(max_workers=len(PORT_CONFIG)) as pool:
            futs = [pool.submit(connect_one, port, cfg) for port, cfg in PORT_CONFIG.items()]
            for fut in as_completed(futs, timeout=10):
                pass

    def _make_push_callback(self, port: int):
        """工厂函数，确保每个 port 的闭包独立捕获"""
        def callback(result: dict) -> None:
            self._push_queue.put(Result(
                port=port, cmd_id=result["cmd_id"], response=result,
            ))
        return callback

    # ── 处理主线程指令 ────────────────────────────────────────────────────

    def _handle_cmd(self, cmd: Cmd) -> None:
        client = self._clients.get(cmd.port)
        if not client:
            self._response_queue.put(Result(
                port=cmd.port, cmd_id=cmd.cmd_id,
                error=f"端口 {cmd.port} 未连接",
            ))
            return

        try:
            if cmd.wait:
                response = client.request(cmd.cmd_id, cmd.data)
                self._response_queue.put(Result(
                    port=cmd.port, cmd_id=cmd.cmd_id, response=response,
                ))
            else:
                client.send(cmd.cmd_id, cmd.data)
        except Exception as e:
            self._response_queue.put(Result(
                port=cmd.port, cmd_id=cmd.cmd_id, error=str(e),
            ))


# ── 主线程接口 ────────────────────────────────────────────────────────────

class AGVManager:
    """
    主线程唯一入口，封装队列通信细节

    支持 with 语句自动管理生命周期：
        with AGVManager() as mgr:
            result = mgr.query(19204, "03E8")
    """

    def __init__(self) -> None:
        self._cmd_queue      : queue.Queue[Union[Cmd, None]] = queue.Queue()
        self._response_queue : queue.Queue[Result]     = queue.Queue()
        self._push_queue     : queue.Queue[Result]     = queue.Queue()
        self._client_thread = AGVClientThread(
            self._cmd_queue, self._response_queue, self._push_queue,
        )

    def start(self, timeout: float = 10.0) -> bool:
        """启动并等待所有端口连接就绪，返回是否成功"""
        self._client_thread.start()
        ready = self._client_thread.wait_ready(timeout=timeout)
        if not ready:
            print("[主线程] 警告：连接超时，部分端口可能未就绪")
        return ready

    def stop(self) -> None:
        """停止所有客户端，等待线程完全退出"""
        self._client_thread.stop()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    def send(self, port: int, cmd_id: str, data: Optional[dict] = None) -> None:
        """发送控制指令，不等响应"""
        self._cmd_queue.put(Cmd(port=port, cmd_id=cmd_id, data=data, wait=False))

    def query(
        self,
        port:    int,
        cmd_id:  str,
        data:    Optional[dict] = None,
        timeout: float       = 5.0,
    ) -> Optional[Result]:
        """
        发送查询指令，阻塞等待响应

        响应来自独立的 response_queue，不会被 AGV 推送消息干扰。
        """
        self._cmd_queue.put(Cmd(port=port, cmd_id=cmd_id, data=data, wait=True))
        try:
            return self._response_queue.get(timeout=timeout)
        except queue.Empty:
            print(f"[主线程] 查询超时（端口{port} cmd={cmd_id}）")
            return None

    def poll(self) -> Optional[Result]:
        """非阻塞取出一条 AGV 主动推送，无消息返回 None"""
        try:
            return self._push_queue.get_nowait()
        except queue.Empty:
            return None


# ── Demo ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    with AGVManager() as mgr:

        print("\n" + "=" * 50)
        print("主线程开始执行，客户端线程在后台运行")
        print("=" * 50 + "\n")

        # 查询（等响应）
        print("[主线程] 查询机器人信息...")
        result = mgr.query(port=19204, cmd_id="03E8")
        if result and result.ok:
            info = result.response["data"]
            print(f"[主线程] 型号: {info.get('model')}，地图: {info.get('current_map')}")
        elif result:
            print(f"[主线程] 查询失败: {result.error}")

        # 发送导航（不等响应）
        task_id = uuid.uuid4().hex[:8].upper()
        print(f"\n[主线程] 发送导航指令 task_id={task_id}")
        mgr.send(
            port=19206,
            cmd_id="0BEB",
            data={"source_id": "A1", "id": "B2", "task_id": task_id},
        )
        mgr.send(
            port=19206,
            cmd_id="0BEF",
            data={"dist":1.0,"vx":0.0,"vy":0.9},
        )

        # 主循环：做自己的事，定期 poll AGV 推送
        print("\n[主线程] 进入主循环，每秒 poll 一次 AGV 推送")
        for i in range(5):
            print(f"[主线程] 第 {i + 1} 次业务逻辑")
            time.sleep(1)
            while msg := mgr.poll():
                data_str = msg.response["data"] if msg.ok else msg.error
                print(f"  [poll] 端口{msg.port} cmd={msg.cmd_id}: {data_str}")

    print("[主线程] 程序正常结束")