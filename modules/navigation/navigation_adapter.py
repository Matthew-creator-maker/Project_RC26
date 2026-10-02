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
import math
import json
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


class NavigationSafetyError(RuntimeError):
    """Navigation stopped for safety; mission must not continue automatically."""


def has_block_alarm(data):
    return any(isinstance(item, dict) and item.get("code") in (52200, "52200")
               for item in data.get("errors", []))


def check_status(data, stopped=False, allow_blocked_alarm=False):
    for key in ("is_stop", "blocked", "emergency"):
        if type(data.get(key)) is not bool:
            raise RuntimeError(f"实时推送缺少布尔字段 {key}")
    for key in ("fatals", "errors"):
        if not isinstance(data.get(key), list):
            raise RuntimeError(f"实时推送缺少报警数组 {key}")
    if data["emergency"]:
        raise RuntimeError("底盘急停已触发")
    if data["fatals"]:
        raise RuntimeError(f"底盘报警 fatals={data['fatals']}")
    errors = data["errors"]
    if allow_blocked_alarm and not stopped:
        errors = [item for item in errors if not (
            isinstance(item, dict) and item.get("code") in (52200, "52200"))]
    if errors:
        raise RuntimeError(f"底盘报警 errors={errors}")
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
        self.recovery_required = False

    def _check_recovery(self):
        if self.recovery_required:
            raise NavigationSafetyError("导航安全锁已触发，禁止自动发车；请人工核实停止与障碍后重新启动程序")

    def _seconds(self, key, default):
        value = float(self.config.get(key, default))
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"navigation.{key} 必须为有限正数")
        return value

    def connect(self):
        self.monitor.connect()
        if not self.api.start():
            raise ConnectionError("导航状态查询失败")
        print(
            "[导航] 受阻等待，持续受阻取消并确认停止；无自动回退；底盘原生安全停车仍须实测",
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
        self._check_recovery()
        data = self.monitor.snapshot().data
        check_status(data, stopped=True)
        if data.get("current_station") != station:
            raise RuntimeError(f"实际站点 {data.get('current_station')!r} 与期望 {station!r} 不一致")
        return data

    def current_station(self):
        self._check_recovery()
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
        deadline = time.monotonic() + self._seconds("cancel_stop_timeout_s", max(
            self.config["state_timeout_s"] * 2.0,
            self.config["request_timeout_s"],
        ))
        settled = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NavigationSafetyError("取消后未确认停止，禁止后续发车；请立即核对底盘，必要时实体急停")
            snap = self.monitor.wait_new(counter, min(remaining, self.config["state_timeout_s"]))
            counter = snap.counter
            check_status(snap.data, allow_blocked_alarm=True)
            status = self.task_status()
            settled = settled + 1 if status not in (1, 2, 3) and snap.data["is_stop"] else 0
            if settled >= self.config["settled_frames"]:
                return snap


    def go_to(self, destination, expected_source):
        self._check_recovery()
        self.assert_at(expected_source)
        if self.task_status() in (1, 2, 3):
            raise RuntimeError("底盘已有导航任务，不能覆盖")
        deadline = time.monotonic() + self.config["navigation_timeout_s"]
        moving = expected_source != destination
        # These are supervisory budgets, not guaranteed braking times.
        # Old blocked_timeout_s/suspended_timeout_s are intentionally not used.
        blocked_wait = self._seconds("blocked_wait_timeout_s", 10.0)
        stop_timeout = self._seconds("blocked_stop_timeout_s", 2.0)
        suspended_timeout = self._seconds("suspended_wait_timeout_s", 10.0)
        try:
            if moving:
                self.active = True  # ACK 丢失时也尝试取消。
                require_ack(self.api.navigate_to(expected_source, destination), "导航")
            counter = self.monitor.snapshot().counter
            settled, blocked_since, suspended_since = 0, None, None
            moving_blocked_since = None
            clear_frames = 0
            previous_status = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DynamicObstacleTimeout(f"导航到 {destination} 超时")
                snap = self.monitor.wait_new(counter, min(remaining, self.config["state_timeout_s"]))
                counter = snap.counter
                check_status(snap.data, allow_blocked_alarm=True)
                now = time.monotonic()
                obstacle = snap.data["blocked"] or has_block_alarm(snap.data)
                if obstacle:
                    settled = 0
                    clear_frames = 0
                    if blocked_since is None:
                        blocked_since = now
                        print(f"[受阻] blocked={snap.data['blocked']}，is_stop={snap.data['is_stop']}；最多等待 {blocked_wait:.1f}s，不回退、不重新发车", flush=True)
                    if snap.data["is_stop"]:
                        moving_blocked_since = None
                    elif moving_blocked_since is None:
                        moving_blocked_since = now
                    elif now - moving_blocked_since >= stop_timeout:
                        raise NavigationSafetyError("已受阻但仍未上报停止，取消导航；软件取消不能代替实体急停")
                    if now - blocked_since >= blocked_wait:
                        raise NavigationSafetyError(f"持续受阻超过 {blocked_wait:.1f}s，取消并停止本轮任务，不自动回退")
                elif blocked_since is not None:
                    moving_blocked_since = None
                    clear_frames += 1
                    if clear_frames >= self.config["settled_frames"]:
                        print("[障碍清除] 连续新状态帧确认，无新发车指令；观察底盘是否继续原任务", flush=True)
                        blocked_since = None
                        clear_frames = 0

                status = self.task_status()
                if self.config.get("log_navigation_frames", False):
                    print(json.dumps({"event": "navigation_status", "t": round(time.time(), 3),
                                      "from": expected_source, "to": destination,
                                      "task_status": status, **snap.data},
                                     ensure_ascii=False), flush=True)
                if status != previous_status:
                    print(f"[导航] {expected_source} -> {destination}，task_status={status}", flush=True)
                    previous_status = status

                # A suspended task is not necessarily an obstacle; never auto-resume it.
                if moving and status == 3:
                    if suspended_since is None:
                        suspended_since = now
                        print(
                            f"[导航] 任务暂时暂停，最多等待 {suspended_timeout:.1f}s 自动恢复",
                            flush=True,
                        )
                    elif now - suspended_since >= suspended_timeout:
                        raise NavigationSafetyError(f"导航持续暂停超过 {suspended_timeout:.1f}s，取消并确认停止，不自动回退")
                else:
                    suspended_since = None

                if moving and status in (5, 6):
                    raise RuntimeError(f"导航失败或取消，task_status={status}")
                if time.monotonic() >= deadline:
                    raise DynamicObstacleTimeout(f"导航到 {destination} 超时")
                arrived = (snap.data.get("current_station") == destination
                           and snap.data["is_stop"] and not obstacle and blocked_since is None
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
        self.recovery_required = True
        counter = None
        try:
            counter = self.monitor.snapshot().counter
        except Exception:
            pass  # Still send cancellation even if the status stream is lost.
        try:
            self.api.cancel_navigation()
            print("[取消] 已收到取消请求响应，正在核对任务状态和实际停止上报", flush=True)
            if counter is None:
                raise NavigationSafetyError("已请求取消，但状态连接失效，无法确认停止；禁止后续发车")
            self._wait_cancelled_and_stopped(counter)
        except Exception as exc:
            raise NavigationSafetyError(f"取消/停车确认失败：{exc}；请核对底盘并准备实体急停") from exc
        self.active = False
        print("[停止确认] 任务已不在执行且连续上报静止；导航安全锁保持，禁止自动发车", flush=True)

    def close(self):
        try:
            if self.active:
                self.cancel()
        finally:
            try:
                self.monitor.close()
            finally:
                self.api.stop()
