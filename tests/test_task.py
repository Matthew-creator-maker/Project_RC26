"""Mission/controller tests.  Only localhost sockets are used; no robot hardware."""
from __future__ import annotations

import json
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import main
from modules.navigation.navigation_adapter import Navigator, check_status


def settings(*argv):
    args = main.build_parser().parse_args(list(argv))
    return main.load_settings(ROOT / "config/task_config.json", args), args


def state(station="LM1", stopped=True, **extra):
    return {
        "current_station": station,
        "is_stop": stopped,
        "blocked": False,
        "emergency": False,
        "errors": [],
        "fatals": [],
        **extra,
    }


class SimulatedRobot:
    """Three localhost ports using the same frame format as the supplied AGV API."""

    def __init__(self, outcome="success", start_station="LM1"):
        self.outcome = outcome
        self.station, self.status, self.target = start_station, 0, None
        self.stopped, self.started, self.cancelled = True, 0.0, False
        self.commands = []
        self.raw_nav_payloads = []
        self.closed = threading.Event()
        self.listeners, self.clients, self.threads = [], [], []
        self.lock = threading.Lock()
        for role in ("status", "nav", "push"):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(0.1)
            self.listeners.append(listener)
            thread = threading.Thread(target=self._accept, args=(listener, role), daemon=True)
            thread.start()
            self.threads.append(thread)

    @staticmethod
    def read_frame(sock):
        def exact(size):
            data = b""
            while len(data) < size:
                part = sock.recv(size - len(data))
                if not part:
                    raise ConnectionError("closed")
                data += part
            return data

        header = exact(16)
        body = exact(int.from_bytes(header[4:8], "big"))
        return int.from_bytes(header[8:10], "big"), json.loads(body) if body else {}

    @staticmethod
    def frame(cmd, data):
        body = json.dumps(data).encode()
        return struct.pack("!BBHIH6s", 0x5A, 1, 1, len(body), cmd, b"\0" * 6) + body

    def _accept(self, listener, role):
        while not self.closed.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.clients.append(client)
            thread = threading.Thread(target=self._handle, args=(client, role), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _advance(self):
        if not self.target or self.status != 2 or time.monotonic() - self.started < 0.12:
            return
        if self.outcome == "timeout":
            return
        if self.outcome == "failed":
            self.status, self.stopped = 5, True
            return
        self.status, self.stopped = 4, True
        if self.outcome != "wrong_station":
            self.station = self.target

    def _handle(self, client, role):
        try:
            client.settimeout(2)
            cmd, data = self.read_frame(client)
            if role == "push":
                client.sendall(self.frame(19300, {"ret_code": 0}))
                while not self.closed.wait(0.02):
                    with self.lock:
                        self._advance()
                        snapshot = state(self.station, self.stopped)
                        if self.target and self.outcome == "emergency":
                            snapshot["emergency"] = True
                        if self.target and self.outcome == "lost_push":
                            return
                        if self.target and self.outcome == "blocked":
                            snapshot["blocked"] = True
                        if (self.target and self.outcome == "blocked_then_clear"
                                and len(self.commands) == 1):
                            snapshot["blocked"] = True
                        if self.target and self.outcome == "incomplete":
                            del snapshot["is_stop"]
                    packet = self.frame(19301, snapshot)
                    client.sendall(packet[:7])
                    client.sendall(packet[7:19])
                    client.sendall(packet[19:])
                return

            with self.lock:
                self._advance()
                if cmd == 1004:
                    response = {"current_station": self.station}
                elif cmd == 1020:
                    response = {"task_status": self.status}
                elif cmd == 3051:
                    self.raw_nav_payloads.append(dict(data))
                    self.commands.append((data.get("source_id", ""), data["id"]))
                    if self.outcome == "rejected":
                        response = {"ret_code": 40010}
                    else:
                        self.target = data["id"]
                        self.started = time.monotonic()
                        self.status, self.stopped = 2, False
                        response = {"ret_code": 0}
                elif cmd == 3003:
                    self.cancelled = True
                    self.status, self.stopped = 6, True
                    response = {"ret_code": 0}
                else:
                    response = {"ret_code": 1}
            client.sendall(self.frame(cmd + 10000, response))
        except (OSError, ConnectionError):
            pass
        finally:
            client.close()

    def config(self):
        config, _ = settings()
        config["navigation"].update(
            host="127.0.0.1",
            status_port=self.listeners[0].getsockname()[1],
            nav_port=self.listeners[1].getsockname()[1],
            push_port=self.listeners[2].getsockname()[1],
            request_timeout_s=0.4,
            state_timeout_s=0.3,
            navigation_timeout_s=0.7,
            blocked_timeout_s=0.1,
            settled_frames=3,
        )
        return config

    def close(self):
        self.closed.set()
        for sock in [*self.listeners, *self.clients]:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        for thread in self.threads:
            thread.join(timeout=0.5)


class FakePrograms:
    def __init__(self, events, fail_action=None):
        self.events = events
        self.fail_action = fail_action

    def factory(self, config, point, args):
        events, action, fail_action = self.events, point["action"], self.fail_action
        events.append(f"prepare:{action}")

        class Program:
            closed = False

            def transport(self, guard):
                guard()
                events.append(f"transport:{action}")

            def run(self, call_point, guard):
                guard()
                events.append(f"run:{action}")
                if action == fail_action:
                    raise RuntimeError(f"{action} failed")
                return {"action": action, "result": f"ok:{action}"}

            def close(self):
                if not self.closed:
                    events.append(f"close:{action}")
                    self.closed = True

        return Program()


class TaskTests(unittest.TestCase):
    def test_preview_has_exact_competition_route_without_hardware(self):
        config, args = settings()

        def forbidden(*_):
            self.fail("preview must not construct hardware objects")

        result = main.run_task(config, args, forbidden, forbidden)
        self.assertEqual(result["route"], main.MISSION_ROUTE)
        self.assertEqual(result["actions"]["LM3"], "detect_grasp_release")
        self.assertEqual(result["actions"]["LM5"], "detect_grasp_hold")
        self.assertEqual(result["actions"]["LM7"], "release")

    def test_config_paths_are_resolved_from_task_file(self):
        config, _ = settings()
        self.assertEqual(config["perception_config"], ROOT / "config/perception.yaml")
        self.assertNotIn("navigation_dir", config)
        self.assertNotIn("perception_dir", config)

    def test_reject_bad_route_or_action(self):
        config, args = settings()
        bad = dict(config)
        bad["route"] = ["LM1", "LM3"]
        with self.assertRaisesRegex(ValueError, "route"):
            main.run_task(bad, args)
        config["task_lm5"] = dict(config["task_lm5"], action="release")
        with self.assertRaisesRegex(ValueError, "task_lm5.action"):
            main.run_task(config, args)

    def test_reject_incomplete_and_string_status(self):
        for value in (state(is_stop="true"), {"current_station": "LM3"}, state(errors=["error"])):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                check_status(value, stopped=True)

    def test_complete_route_and_action_order(self):
        robot = SimulatedRobot()
        events = []
        programs = FakePrograms(events)
        config = robot.config()
        args = settings("--execute", "--confirm-calibration")[1]
        try:
            result = main.run_task(config, args, grasp_factory=programs.factory)
            self.assertEqual(result["station"], "LM7")
            self.assertEqual(
                robot.commands,
                [("LM1", "LM2"), ("LM2", "LM3"), ("LM3", "LM4"),
                 ("LM4", "LM5"), ("LM5", "LM6"), ("LM6", "LM7")],
            )
            self.assertEqual(
                events,
                [
                    "prepare:release",
                    "transport:release", "run:release", "transport:release", "close:release",
                    "prepare:carry", "run:carry", "transport:carry", "close:carry",
                    "prepare:place", "run:place", "close:place",
                ],
            )
            self.assertEqual(result["results"]["LM3"]["action"], "release")
            self.assertEqual(result["results"]["LM5"]["action"], "carry")
            self.assertEqual(result["results"]["LM7"]["action"], "place")
        finally:
            robot.close()

    def test_navigation_only_goes_all_the_way_to_lm7_without_grasp_programs(self):
        robot = SimulatedRobot()
        config = robot.config()
        args = settings("--execute", "--navigation-only")[1]
        try:
            result = main.run_task(
                config,
                args,
                grasp_factory=lambda *_: self.fail("navigation-only must not construct grasp program"),
            )
            self.assertEqual(result["station"], "LM7")
            self.assertEqual(len(robot.commands), 6)
        finally:
            robot.close()

    def test_full_task_requires_calibration_confirmation_before_factories(self):
        config, args = settings("--execute")
        with self.assertRaisesRegex(RuntimeError, "confirm-calibration"):
            main.run_task(
                config,
                args,
                navigator_factory=lambda *_: self.fail("must fail before navigator"),
                grasp_factory=lambda *_: self.fail("must fail before grasp factory"),
            )

    def test_wrong_start_stops_before_navigation(self):
        robot = SimulatedRobot(start_station="LM9")
        config = robot.config()
        args = settings("--execute", "--confirm-calibration")[1]
        events = []
        try:
            with self.assertRaisesRegex(RuntimeError, "实际起点"):
                main.run_task(config, args, grasp_factory=FakePrograms(events).factory)
            self.assertEqual(robot.commands, [])
        finally:
            robot.close()

    def test_navigation_failure_never_runs_arm_station_action_and_cancels(self):
        robot = SimulatedRobot("failed")
        config = robot.config()
        args = settings("--execute", "--confirm-calibration")[1]
        events = []
        try:
            with self.assertRaises(RuntimeError):
                main.run_task(config, args, grasp_factory=FakePrograms(events).factory)
            self.assertTrue(robot.cancelled)
            self.assertFalse(any(event.startswith("run:") for event in events))
        finally:
            robot.close()

    def test_action_failure_does_not_continue_to_later_navigation(self):
        robot = SimulatedRobot()
        config = robot.config()
        args = settings("--execute", "--confirm-calibration")[1]
        events = []
        try:
            with self.assertRaisesRegex(RuntimeError, "release failed"):
                main.run_task(config, args, grasp_factory=FakePrograms(events, "release").factory)
            self.assertEqual(robot.commands, [("LM1", "LM2"), ("LM2", "LM3")])
            self.assertNotIn("run:carry", events)
            self.assertNotIn("run:place", events)
        finally:
            robot.close()

    def test_dynamic_block_returns_to_previous_task_point(self):
        robot = SimulatedRobot("blocked_then_clear")
        config = robot.config()
        navigator = Navigator(config["navigation"])
        try:
            navigator.connect()
            with self.assertRaisesRegex(TimeoutError, "已返回上一任务点 LM1"):
                navigator.go_to("LM2", "LM1")
            self.assertEqual(navigator.current_station(), "LM1")
            self.assertEqual(robot.commands, [("LM1", "LM2"), ("", "LM1")])
            self.assertEqual(robot.raw_nav_payloads[1], {"id": "LM1"})
        finally:
            navigator.close()
            robot.close()

    def test_common_navigation_failures_abort(self):
        for outcome in ("rejected", "timeout", "wrong_station", "emergency", "lost_push", "incomplete", "blocked"):
            with self.subTest(outcome=outcome):
                robot = SimulatedRobot(outcome)
                config = robot.config()
                args = settings("--execute", "--confirm-calibration")[1]
                events = []
                try:
                    with self.assertRaises((RuntimeError, TimeoutError, ConnectionError)):
                        main.run_task(config, args, grasp_factory=FakePrograms(events).factory)
                    self.assertFalse(any(event.startswith("run:") for event in events))
                finally:
                    robot.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
