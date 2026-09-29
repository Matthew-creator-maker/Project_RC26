"""导航 API 适配：有界请求、实时状态和到站等待。

沿用原 AGVApi 的业务方法和 agv_protocol 的组包/解包；不启动原 Demo。
每次查询使用独立 TCP 连接，防止原 Manager 公用响应队列的迟到回包串台。
"""
from __future__ import annotations

try:
    from .dynamic_obstacle_manager import DynamicObstacleTimeout
except Exception:
    DynamicObstacleTimeout = TimeoutError

import socket
import threading
import time
from dataclasses import dataclass


def import_navigation(directory=None):
    """Return the canonical navigation API modules.

    ``directory`` is retained for compatibility only; no dynamic version folder is used.
    """
    from .agv_api import agv_api as api_module
    from .agv_api import agv_protocol as protocol
    return api_module, protocol


def make_api(config_or_directory, config=None):
    # Accept both make_api(config) and the historical make_api(directory, config).
    if config is None:
        config = config_or_directory
    api_module, protocol = import_navigation()

    class CheckedAGVApi(api_module.AGVApi):
        # 重用原 API 的 navigate_to/get_pose/get_task_status/cancel_navigation。
        # 只替换通信层，所有网络请求串行执行且连接在请求结束时关闭。
        def __init__(self):
            self.host = config["host"]
            self.timeout = config["request_timeout_s"]
            self.ports = {19204: config["status_port"], 19206: config["nav_port"]}

        def _query(self, port, cmd_id, data=None, timeout=5.0):
            port = self.ports.get(port, port)
            with socket.create_connection((self.host, port),
                                          timeout=min(timeout, self.timeout)) as sock:
                sock.sendall(protocol.build_frame(cmd_id, data))
                raw = protocol.recv_full_frame(sock)
            if raw[:4] != protocol.FRAME_HEADER:
                raise RuntimeError("导航响应报头或序号不匹配")
            response = protocol.parse_frame(raw)
            expected = format(int(cmd_id, 16) + 10000, "04X")
            if response["cmd_id"] != expected:
                raise RuntimeError(f"请求 {cmd_id} 收到意外响应 {response['cmd_id']}")
            body = response["data"]
            if not isinstance(body, dict):
                raise RuntimeError("导航响应 data 必须是 JSON 对象")
            if "ret_code" in body and (type(body["ret_code"]) is not int or body["ret_code"] != 0):
                raise RuntimeError(f"底盘拒绝请求 {cmd_id}: {body}")
            return api_module.Result(port=port, cmd_id=cmd_id, response=response)

        def _send(self, port, cmd_id, data=None):
            result = self._query(port, cmd_id, data)
            require_ack(result.response["data"], cmd_id)

        def start(self, timeout=10.0):
            return self.get_pose() is not None

        def stop(self):
            pass

    return CheckedAGVApi(), protocol


def require_ack(data, action):
    if not isinstance(data, dict) or type(data.get("ret_code")) is not int or data["ret_code"] != 0:
        raise RuntimeError(f"{action}未返回 ret_code=0: {data}")


def check_status(data, stopped=False):
    for key in ("is_stop", "blocked", "emergency"):
        if type(data.get(key)) is not bool:
            raise RuntimeError(f"实时推送缺少布尔字段 {key}")
    for key in ("fatals", "errors"):
        if not isinstance(data.get(key), list):
            raise RuntimeError(f"实时推送缺少报警数组 {key}")
        if data[key]:
            raise RuntimeError(f"底盘报警 {key}={data[key]}")
    if data["emergency"]:
        raise RuntimeError("底盘急停已触发")
    if stopped and (not data["is_stop"] or data["blocked"]):
        raise RuntimeError("底盘未静止或被阻挡，不能执行机械臂动作")


@dataclass(frozen=True)
class Snapshot:
    data: dict
    counter: int
    received_at: float


class PushMonitor:
    FIELDS = ["current_station", "is_stop", "blocked", "emergency", "fatals", "errors"]

    def __init__(self, protocol, config):
        self.protocol, self.config = protocol, config
        self.sock = None
        self.thread = None
        self.closed = False
        self.error = None
        self.latest = None
        self.condition = threading.Condition()

    def connect(self):
        try:
            self.sock = socket.create_connection(
                (self.config["host"], self.config["push_port"]),
                timeout=self.config["request_timeout_s"])
            self.sock.settimeout(self.config["state_timeout_s"])
            self.sock.sendall(self.protocol.build_frame("2454", {
                "interval": 500, "included_fields": self.FIELDS}))
            self.thread = threading.Thread(target=self._receive, name="task-agv-status", daemon=True)
            self.thread.start()
            self.wait_new(0, self.config["state_timeout_s"])
        except BaseException:
            self.close()
            raise

    def _receive(self):
        try:
            while not self.closed:
                raw = self.protocol.recv_full_frame(self.sock)
                if raw[:2] != b"\x5a\x01":
                    raise RuntimeError("状态推送报头无效")
                response = self.protocol.parse_frame(raw)
                data = response["data"]
                if not isinstance(data, dict):
                    raise RuntimeError("状态推送必须为 JSON 对象")
                if response["cmd_id"] == "4B64":
                    require_ack(data, "订阅状态")
                    continue  # ACK 不会刷新状态时效。
                if response["cmd_id"] != "4B65":
                    raise RuntimeError(f"意外推送命令 {response['cmd_id']}")
                if any(key not in data for key in self.FIELDS):
                    raise RuntimeError("状态帧字段不完整，请核对底盘推送协议")
                with self.condition:
                    counter = self.latest.counter + 1 if self.latest else 1
                    self.latest = Snapshot(data, counter, time.monotonic())
                    self.condition.notify_all()
        except Exception as exc:
            with self.condition:
                if not self.closed:
                    self.error = exc
                self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            if self.error:
                raise ConnectionError(f"实时状态失效: {self.error}") from self.error
            if self.closed or self.latest is None:
                raise ConnectionError("尚无实时状态或连接已关闭")
            if time.monotonic() - self.latest.received_at > self.config["state_timeout_s"]:
                raise DynamicObstacleTimeout("底盘状态已过期")
            return Snapshot(dict(self.latest.data), self.latest.counter, self.latest.received_at)

    def wait_new(self, after, timeout):
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.latest is None or self.latest.counter <= after:
                if self.error or self.closed:
                    return self.snapshot()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DynamicObstacleTimeout("未收到新的底盘状态帧")
                self.condition.wait(remaining)
            return self.snapshot()

    def close(self):
        self.closed = True
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
        with self.condition:
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=2)
        self.sock = None


class Navigator:
    def __init__(self, config_or_directory, config=None, api=None, monitor=None):
        # New style: Navigator(config). Old style Navigator(directory, config) still works.
        if config is None:
            config = config_or_directory
        self.config = config
        if api is None:
            api, protocol = make_api(config)
            monitor = PushMonitor(protocol, config)
        self.api, self.monitor = api, monitor
        self.active = False

    def connect(self):
        self.monitor.connect()
        if not self.api.start():
            raise ConnectionError("导航状态查询失败")
        print(
            "[导航] 保留底盘原生避障；程序不做绕路，只在持续阻塞后回退上一任务点",
            flush=True,
        )

    def task_status(self):
        data = self.api.get_task_status()
        if not isinstance(data, dict) or type(data.get("task_status")) is not int:
            raise RuntimeError(f"任务状态无效: {data}")
        status = data["task_status"]
        if status not in range(7):
            raise RuntimeError(f"未知任务状态: {status}")
        return status

    def assert_at(self, station):
        data = self.monitor.snapshot().data
        check_status(data, stopped=True)
        if data.get("current_station") != station:
            raise RuntimeError(f"实际站点 {data.get('current_station')!r} 与期望 {station!r} 不一致")
        return data

    def current_station(self):
        data = self.monitor.snapshot().data
        check_status(data, stopped=True)
        station = data.get("current_station")
        if not isinstance(station, str) or not station.strip():
            raise RuntimeError("机器人不在已知站点，请先定位到路线起点")
        if self.task_status() in (1, 2, 3):
            raise RuntimeError("底盘已有等待、运行或暂停的任务，请先处理现有任务")
        return station

    def go_to_station(self, destination):
        """从底盘实时上报的当前 LM 点前往 destination。

        比赛主流程不再手工维护 expected_source，避免扫描/抓取动态跳点后
        源点变量与底盘真实站点不一致。只有机器人静止在已知 LM 点时才会发车。
        """
        source = self.current_station()
        if source == destination:
            self.assert_at(destination)
            print(f"[到站] 已位于 {destination}，无需重复导航", flush=True)
            return
        self.go_to(destination, source)

    def _wait_cancelled_and_stopped(self, counter):
        """取消当前导航后，等待底盘确认不再执行任务且已经停止。"""
        deadline = time.monotonic() + max(
            self.config["state_timeout_s"] * 2.0,
            self.config["request_timeout_s"],
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DynamicObstacleTimeout("取消当前导航后底盘未及时停止")
            snap = self.monitor.wait_new(counter, min(remaining, self.config["state_timeout_s"]))
            counter = snap.counter
            check_status(snap.data)
            status = self.task_status()
            if status not in (1, 2, 3) and snap.data["is_stop"]:
                return snap

    def _return_to_previous_task_point(self, previous_station, counter):
        """阻塞回退：取消当前目标，并从当前位置返回上一任务点；不尝试绕路。"""
        print(
            f"[阻塞回退] 持续阻塞，取消当前导航并返回上一任务点 {previous_station}",
            flush=True,
        )
        self.cancel()
        snap = self._wait_cancelled_and_stopped(counter)

        # 取消后如果底盘仍处在上一任务点范围内，则不重复下发导航。
        if (snap.data.get("current_station") == previous_station
                and snap.data["is_stop"] and not snap.data["blocked"]):
            self.assert_at(previous_station)
            print(f"[阻塞回退] 已回到上一任务点 {previous_station}", flush=True)
            return

        # source_id 留空表示从当前实际位置重新规划到上一任务点。
        # 项目自带 AGV API demo 也保留了 navigate_to("", "LM1") 的调用方式。
        self.active = True
        require_ack(self.api.navigate_to("", previous_station), "阻塞回退")

        deadline = time.monotonic() + self.config["navigation_timeout_s"]
        counter = snap.counter
        settled = 0
        previous_status = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DynamicObstacleTimeout(f"返回上一任务点 {previous_station} 超时")
            snap = self.monitor.wait_new(counter, min(remaining, self.config["state_timeout_s"]))
            counter = snap.counter
            check_status(snap.data)
            status = self.task_status()
            if status != previous_status:
                print(
                    f"[阻塞回退] 当前 -> {previous_station}，task_status={status}",
                    flush=True,
                )
                previous_status = status
            if status in (5, 6):
                raise RuntimeError(
                    f"返回上一任务点 {previous_station} 失败或取消，task_status={status}"
                )

            arrived = (
                snap.data.get("current_station") == previous_station
                and snap.data["is_stop"]
                and not snap.data["blocked"]
                and status == 4
            )
            settled = settled + 1 if arrived else 0
            if settled >= self.config["settled_frames"]:
                self.assert_at(previous_station)
                self.active = False
                print(
                    f"[阻塞回退] 已返回上一任务点 {previous_station}，"
                    f"连续 {settled} 个新状态帧确认静止",
                    flush=True,
                )
                return

    def go_to(self, destination, expected_source):
        self.assert_at(expected_source)
        if self.task_status() in (1, 2, 3):
            raise RuntimeError("底盘已有导航任务，不能覆盖")
        deadline = time.monotonic() + self.config["navigation_timeout_s"]
        moving = expected_source != destination
        suspended_timeout = float(self.config.get("suspended_timeout_s", 8.0))
        if suspended_timeout <= 0:
            raise ValueError("navigation.suspended_timeout_s 必须大于 0")
        try:
            if moving:
                self.active = True  # ACK 丢失时也尝试取消。
                require_ack(self.api.navigate_to(expected_source, destination), "导航")
            counter = self.monitor.snapshot().counter
            settled, blocked_since, suspended_since = 0, None, None
            previous_status = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DynamicObstacleTimeout(f"导航到 {destination} 超时")
                snap = self.monitor.wait_new(counter, min(remaining, self.config["state_timeout_s"]))
                counter = snap.counter
                check_status(snap.data)
                now = time.monotonic()
                if snap.data["blocked"]:
                    if blocked_since is None:
                        blocked_since = now
                    if now - blocked_since >= self.config["blocked_timeout_s"]:
                        self._return_to_previous_task_point(expected_source, counter)
                        raise DynamicObstacleTimeout(
                            f"前往 {destination} 持续受阻，已返回上一任务点 {expected_source}"
                        )
                else:
                    blocked_since = None

                status = self.task_status()
                if status != previous_status:
                    print(f"[导航] {expected_source} -> {destination}，task_status={status}", flush=True)
                    previous_status = status

                # SUSPENDED 持续存在时不等待底盘另寻路径；达到阈值后直接回退。
                # 短暂暂停仍留出少量去抖时间，避免单帧状态抖动触发回退。
                if moving and status == 3:
                    if suspended_since is None:
                        suspended_since = now
                        print(
                            f"[导航] 任务暂时暂停，最多等待 {suspended_timeout:.1f}s 自动恢复",
                            flush=True,
                        )
                    elif now - suspended_since >= suspended_timeout:
                        self._return_to_previous_task_point(expected_source, counter)
                        raise DynamicObstacleTimeout(
                            f"导航持续暂停超过 {suspended_timeout:.1f}s，"
                            f"已返回上一任务点 {expected_source}: "
                            f"{expected_source} -> {destination}"
                        )
                else:
                    suspended_since = None

                if moving and status in (5, 6):
                    raise RuntimeError(f"导航失败或取消，task_status={status}")
                if time.monotonic() >= deadline:
                    raise DynamicObstacleTimeout(f"导航到 {destination} 超时")
                arrived = (snap.data.get("current_station") == destination
                           and snap.data["is_stop"] and not snap.data["blocked"]
                           and (status == 4 if moving else status in (0, 4, 5, 6)))
                settled = settled + 1 if arrived else 0
                if settled >= self.config["settled_frames"]:
                    self.assert_at(destination)
                    self.active = False
                    print(f"[到站] {destination}，连续 {settled} 个新状态帧确认静止", flush=True)
                    return
        except BaseException:
            if self.active:
                self.cancel()
            raise

    def cancel(self):
        try:
            self.api.cancel_navigation()
            print("[取消] 底盘已确认取消请求；请核对实际停止状态", flush=True)
        except Exception as exc:
            print(f"[取消] 未能确认取消导航: {exc}", flush=True)
        finally:
            self.active = False

    def close(self):
        try:
            if self.active:
                self.cancel()
        finally:
            try:
                self.monitor.close()
            finally:
                self.api.stop()
