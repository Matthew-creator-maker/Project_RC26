from __future__ import annotations

import sys
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import numpy as np
except ImportError:
    np = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import main


class FakeRail:
    lowered = False
    def prepare(self, station):
        self.lowered = station in {"LM3", "LM4"}
    def travel(self):
        self.lowered = False


class FakeNavigator:
    def __init__(self, _config):
        self.station = main.START_STATION
        self.commands = []

    def connect(self):
        pass

    def current_station(self):
        return self.station

    def assert_at(self, station):
        if self.station != station:
            raise RuntimeError(f"expected {station}, got {self.station}")

    def go_to_station(self, destination):
        self.commands.append((self.station, destination))
        self.station = destination

    def close(self):
        pass


class FakeScanner:
    def __init__(self, grasp_plan, scan_plan=None):
        self.grasp_plan = {k: list(v) for k, v in grasp_plan.items()}
        self.scan_plan = scan_plan
        self.all_calls = []
        self.grasp_calls = []
        self.expected_labels_calls = []
        self.station_attempts = defaultdict(int)
        self.transport_calls = 0
        self.release_calls = 0

    def transport(self, guard):
        guard()
        self.transport_calls += 1

    def scan_station(self, station, seconds, guard):
        # Phase 1 only: LM2 -> LM6 recognition/announcement.
        guard()
        self.all_calls.append(station)
        labels = ([f"Obj{station[-1]}"] if self.scan_plan is None
                  else self.scan_plan.get(station, []))
        return [{"label": label} for label in labels]

    def grasp_station_once(
        self,
        station,
        search_timeout_seconds,
        sample_timeout_seconds,
        guard,
        expected_labels,
    ):
        guard()
        self.grasp_calls.append(station)
        self.expected_labels_calls.append(
            (station, None if expected_labels is None else list(expected_labels))
        )
        idx = self.station_attempts[station]
        self.station_attempts[station] += 1
        choices = self.grasp_plan.get(station, [])
        if idx >= len(choices) or choices[idx] is None:
            raise TimeoutError(f"{station} no stable target")
        if isinstance(choices[idx], BaseException):
            raise choices[idx]
        return {
            "target": choices[idx],
            "action": "carry",
            "result": "grasp_held",
            "holding_verified": False,
        }

    def release_gripper(self, guard):
        guard()
        self.release_calls += 1
        return {
            "action": "place",
            "result": "released",
            "holding_verified": False,
        }

    def close(self):
        pass


class FakeProgramFactory:
    def __init__(self, timeout_once=()):
        self.timeout_once = set(timeout_once)
        self.run_counts = defaultdict(int)
        self.events = []

    def __call__(self, _config, point, _args):
        action = point["action"]
        target = point.get("target")
        owner = self

        class Program:
            def run(self, call_point, guard):
                guard()
                owner.events.append(("run", action, target))
                if action == "carry":
                    owner.run_counts[target] += 1
                    if target in owner.timeout_once and owner.run_counts[target] == 1:
                        raise TimeoutError(f"{target} timeout")
                return {"action": action, "target": target, "ok": True}

            def transport(self, guard):
                guard()
                owner.events.append(("transport", action, target))

            def close(self):
                owner.events.append(("close", action, target))

        return Program()


def make_args():
    return SimpleNamespace(
        execute=True,
        confirm_calibration=True,
        navigation_only=False,
        show=False,
        scan_seconds=0.01,
        match_seconds=480.0,
        exit_reserve_seconds=45.0,
        grasp_timeout_seconds=0.01,
    )


class RetryQueueTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "navigation": {},
            "perception_config": ROOT / "config/perception.yaml",
            "arm_transport": {
                "configured": True,
                "joints_deg": [0, 90, 0, -90, -90, 170],
            },
        }

    def run_mission(self, scanner, programs=None, remaining_patch=None, rail_factory=FakeRail):
        nav = FakeNavigator({})
        ctx = (
            patch.object(main, "_remaining", side_effect=remaining_patch)
            if remaining_patch
            else None
        )
        if ctx:
            ctx.__enter__()
        try:
            with patch.object(main, "speak_blocking", return_value=True):
                result = main.run_competition(
                    self.config,
                    make_args(),
                    navigator_factory=lambda _cfg: nav,
                    grasp_factory=programs or FakeProgramFactory(),
                    scanner_factory=lambda _cfg, _args: scanner,
                    rail_factory=rail_factory,
                )
        finally:
            if ctx:
                ctx.__exit__(None, None, None)
        return result, nav

    def test_every_station_is_recognized_even_when_scan_record_is_empty(self):
        # 扫描阶段只在 LM5 记录到物品；抓取阶段仍必须识别全部 LM2~LM6。
        scanner = FakeScanner(
            grasp_plan={
                "LM6": [None, None],
                "LM5": ["Obj5", None],
                "LM4": [None, None],
                "LM3": [None, None],
                "LM2": [None, None],
            },
            scan_plan={"LM2": [], "LM3": [], "LM4": [], "LM5": ["Obj5"], "LM6": []},
        )
        result, nav = self.run_mission(scanner)

        self.assertEqual(
            scanner.grasp_calls[:5],
            ["LM6", "LM5", "LM4", "LM3", "LM2"],
        )
        # 成功抓到一次后，必须再完整巡检 LM2->LM6 一轮确认已经清空。
        self.assertEqual(
            scanner.grasp_calls[5:],
            ["LM2", "LM3", "LM4", "LM6"],
        )
        self.assertTrue(all(labels is None for _, labels in scanner.expected_labels_calls))
        self.assertEqual(result["delivered_count"], 1)
        self.assertEqual(result["finish_reason"], "all_objects_cleared")
        self.assertEqual(nav.current_station(), "LM8")

    def test_object_missed_during_scan_can_be_found_and_grasped(self):
        # 扫描阶段所有点都漏检，但 LM3 在抓取阶段现场识别到物品。
        scanner = FakeScanner(
            grasp_plan={
                "LM6": [None, None],
                "LM5": [None, None],
                "LM4": [None, None],
                "LM3": ["LateObj", None],
                "LM2": [None, None],
            },
            scan_plan={station: [] for station in main.SCAN_ROUTE},
        )
        result, _ = self.run_mission(scanner)

        self.assertEqual(result["delivered_count"], 1)
        self.assertEqual(result["results"][0]["target"], "LateObj")
        self.assertEqual(result["results"][0]["from_station"], "LM3")
        self.assertEqual(result["finish_reason"], "all_objects_cleared")

    def test_full_empty_round_finishes_without_waiting_for_time_limit(self):
        scanner = FakeScanner(
            grasp_plan={station: [None] for station in main.GRASP_PRIORITY},
            scan_plan={station: [] for station in main.SCAN_ROUTE},
        )
        result, nav = self.run_mission(scanner)

        self.assertEqual(
            scanner.grasp_calls,
            ["LM6", "LM5", "LM4", "LM3", "LM2"],
        )
        self.assertEqual(result["delivered_count"], 0)
        self.assertEqual(result["finish_reason"], "all_objects_cleared")
        self.assertEqual(nav.current_station(), "LM8")

    def test_scan_uses_far_station_and_grasp_uses_task_station(self):
        scanner = FakeScanner(
            grasp_plan={station: [None] for station in main.GRASP_PRIORITY},
            scan_plan={station: [] for station in main.SCAN_ROUTE},
        )
        with patch.dict(main.SCAN_NAV_STATIONS, {"LM5": "LM15"}):
            _, nav = self.run_mission(scanner)
        self.assertIn(("LM4", "LM15"), nav.commands)
        self.assertIn(("LM15", "LM6"), nav.commands)
        self.assertIn(("LM6", "LM5"), nav.commands)
        self.assertEqual(scanner.all_calls, main.SCAN_ROUTE)
        self.assertEqual(scanner.grasp_calls[1], "LM5")

    def test_success_goes_to_lm7_then_continues_next_station(self):
        scanner = FakeScanner(
            grasp_plan={
                "LM6": ["Obj6", None],
                "LM5": [None, None],
                "LM4": [None, None],
                "LM3": [None, None],
                "LM2": [None, None],
            },
            scan_plan={station: [] for station in main.SCAN_ROUTE},
        )
        result, nav = self.run_mission(scanner)

        self.assertEqual(result["delivered_count"], 1)
        self.assertIn(("LM6", "LM7"), nav.commands)
        idx = nav.commands.index(("LM6", "LM7"))
        self.assertEqual(nav.commands[idx + 1], ("LM7", "LM5"))
        self.assertNotIn("LM1", [dst for _, dst in nav.commands])

    def test_time_limit_stops_new_recognition_and_exits(self):
        scanner = FakeScanner(
            grasp_plan={station: [None, None] for station in main.GRASP_PRIORITY},
            scan_plan={station: [] for station in main.SCAN_ROUTE},
        )
        # 前 5 次用于扫描阶段/抓取起点等检查保持有时间；
        # 随后在抓取循环内触发到时。
        calls = {"n": 0}
        def remaining(*_args):
            calls["n"] += 1
            return 100.0 if calls["n"] < 8 else 0.0

        result, nav = self.run_mission(scanner, remaining_patch=remaining)
        self.assertEqual(result["finish_reason"], "time_limit")
        self.assertEqual(nav.current_station(), "LM8")

    def test_grasp_runtime_error_recovers_and_moves_to_next_station(self):
        scanner = FakeScanner(
            grasp_plan={
                "LM6": [RuntimeError("arm communication failed"), None],
                "LM5": [None],
                "LM4": [None],
                "LM3": [None],
                "LM2": [None],
            },
            scan_plan={station: [] for station in main.SCAN_ROUTE},
        )
        result, nav = self.run_mission(scanner)
        self.assertEqual(scanner.grasp_calls[:2], ["LM6", "LM5"])
        self.assertEqual(nav.current_station(), "LM8")
        self.assertEqual(result["finish_reason"], "all_objects_cleared")

    def test_recovery_failure_prevents_navigation(self):
        scanner = FakeScanner(
            grasp_plan={"LM6": [RuntimeError("arm failed")]},
            scan_plan={station: [] for station in main.SCAN_ROUTE},
        )
        original_transport = scanner.transport
        def transport(guard):
            if scanner.grasp_calls:
                raise RuntimeError("arm cannot return")
            original_transport(guard)
        scanner.transport = transport
        nav = FakeNavigator({})
        with patch.object(main, "speak_blocking", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "禁止移动底盘"):
                main.run_competition(
                    self.config, make_args(), navigator_factory=lambda _: nav,
                    scanner_factory=lambda _cfg, _args: scanner, rail_factory=FakeRail,
                )
        self.assertNotIn(("LM6", "LM5"), nav.commands)

class ScannerTargetTests(unittest.TestCase):
    def test_empty_expected_labels_allows_live_recognition(self):
        # 新规则：空 expected_labels 不再因“扫描阶段无记录”而直接拒绝。
        scanner = main.CompetitionScanner.__new__(main.CompetitionScanner)
        with self.assertRaises(AttributeError):
            scanner.grasp_station_once("LM5", 1, 1, lambda: None, expected_labels=[])

    def test_scanner_ignores_unrecorded_higher_confidence_target(self):
        scanner = main.CompetitionScanner.__new__(main.CompetitionScanner)
        scanner.vision_config = {"grasp_test": {"stable_samples": 1}}
        scanner._arm_connected = True
        scanner._camera_started = True
        scanner.observation_pose = [0] * 6
        scanner.settle_seconds = 0
        scanner.show = False
        scanner.arm = SimpleNamespace(movej=lambda _pose: None,
                                      get_current_pose=lambda: [0] * 6)
        scanner.camera = SimpleNamespace(get_aligned_frames=lambda **_kw: {
            "color_image": None, "depth_frame": None, "intrinsics": None,
        })
        targets = [
            {"label": "Wrong", "stable": True, "localization_ok": True,
             "confidence": 0.99, "xyz_camera": [1, 1, 1]},
            {"label": "Cola", "stable": True, "localization_ok": True,
             "confidence": 0.55, "xyz_camera": [0.1, 0.2, 0.3]},
        ]
        scanner.pipeline = SimpleNamespace(reset_stability=lambda: None,
                                           process_frame=lambda *_args: targets)
        scanner.module = SimpleNamespace(
            np=np,
            finite_vector=lambda value, count, name: value,
            find_stable_target=lambda results, label: next(
                (item for item in results if item["label"] == label and item["stable"]), None
            ),
            median_point=lambda points: np.median(points, axis=0),
            execute_prelocalized_grasp=lambda _cfg, label, point, _pose, _arm, **_kw: {
                "target": label, "xyz_camera_m": point.tolist(),
            },
        )
        result = scanner.grasp_station_once(
            "LM5", 1, 1, lambda: None, expected_labels=["Cola"]
        )
        self.assertEqual(result["target"], "Cola")
        self.assertEqual(result["xyz_camera_m"], [0.1, 0.2, 0.3])


if __name__ == "__main__":
    unittest.main(verbosity=2)
