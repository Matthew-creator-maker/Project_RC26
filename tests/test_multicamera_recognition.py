"""多相机识别回归测试：全部使用模拟设备，不导入 RealSense/YOLO，不连接机器人。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import main
from modules.perception.recognition_camera import RecognitionCameraSwitcher
from modules.perception.recognition_config import RecognitionSettings, display_enabled, validate_display
from modules.perception.recognition_service import ConsecutiveLabelTracker, RecognitionService, RecognitionWindow


def configuration(**changes):
    cfg = {"cameras": {
        "chest": {"serial": "chest-123", "stations": ["LM2", "LM3"]},
        "head": {"serial": "head-456", "stations": ["LM9"]},
    }, "required_frames": 2, "warmup_frames": 0, "show_window": False}
    cfg.update(changes)
    return cfg


def detection(label="cola", confidence=0.9):
    return {"label": label, "confidence": confidence, "bbox": [1, 1, 10, 10], "pixel_center": [5, 5]}


class Clock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now


class FakeWindow:
    def __init__(self, cancel=False):
        self.calls = []
        self.closed = False
        self.cancel = cancel
    def show(self, *args):
        self.calls.append(args)
        if self.cancel:
            raise KeyboardInterrupt("cancel")
    def close(self):
        self.closed = True


class FakeSwitcher:
    def __init__(self, clock, frames=None):
        self.clock = clock
        self.frames = frames
        self.index = 0
        self.spec = None
        self.closed = False
    def select(self, spec, guard):
        guard()
        self.spec = spec
        self.index = 0
    def read(self, _timeout):
        self.clock.now += 0.01
        index = self.index
        self.index += 1
        if self.frames is not None:
            return self.frames[index] if index < len(self.frames) else None
        return {"color_image": "RGB", "depth_frame": None, "timestamp_ms": index}
    def close(self):
        self.closed = True


class SettingsTests(unittest.TestCase):
    def test_bad_camera_config_rejected_before_loading_grasp_or_model(self):
        cfg = configuration()
        cfg["cameras"]["chest"]["serial"] = ""
        settings = RecognitionSettings(cfg)
        with patch.object(main, "load_recognition_settings", return_value=settings), \
             patch.object(main, "load_grasp_module", side_effect=AssertionError("hardware/model imported")):
            with self.assertRaisesRegex(ValueError, "序列号为空"):
                main.CompetitionScanner({"perception_config": "unused"}, SimpleNamespace(show=False))

    def test_two_containers_choose_expected_camera(self):
        settings = RecognitionSettings(configuration())
        self.assertEqual(settings.camera_for_station("lm2").role, "chest")
        self.assertEqual(settings.camera_for_station("LM9").role, "head")

    def test_station_in_two_containers_rejected(self):
        cfg = configuration()
        cfg["cameras"]["head"]["stations"].append("LM2")
        with self.assertRaisesRegex(ValueError, "重复"):
            RecognitionSettings(cfg)

    def test_duplicate_in_single_container_rejected(self):
        cfg = configuration()
        cfg["cameras"]["chest"]["stations"].append("lm2")
        with self.assertRaisesRegex(ValueError, "重复"):
            RecognitionSettings(cfg)

    def test_unknown_station_does_not_fall_back_to_random_camera(self):
        with self.assertRaisesRegex(ValueError, "未分配"):
            RecognitionSettings(configuration()).validate_route(["LM10"])

    def test_missing_head_serial_checked_before_arrival(self):
        cfg = configuration()
        cfg["cameras"]["head"]["serial"] = ""
        settings = RecognitionSettings(cfg)
        settings.validate_route(["LM2"])
        with self.assertRaisesRegex(ValueError, "head"):
            settings.validate_route(["LM2", "LM9"])

    def test_same_serial_for_two_roles_rejected(self):
        cfg = configuration()
        cfg["cameras"]["head"]["serial"] = "chest-123"
        with self.assertRaisesRegex(ValueError, "同一个"):
            RecognitionSettings(cfg)

    def test_arm_camera_cannot_be_used_as_chest(self):
        with self.assertRaisesRegex(ValueError, "左臂"):
            RecognitionSettings(configuration()).validate_route(["LM2"], left_serial="chest-123")

    def test_cli_can_override_default_live_display(self):
        settings = RecognitionSettings(configuration(show_window=True))
        self.assertTrue(display_enabled(None, settings))
        self.assertFalse(display_enabled(False, settings))
        self.assertTrue(display_enabled(True, settings))

    def test_gui_is_checked_before_motion_on_linux(self):
        with patch.object(sys, "platform", "linux"), patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "图形桌面"):
                validate_display(True)
            validate_display(False)

    def test_invalid_boolean_and_nonfinite_parameters_rejected(self):
        for changes in [{"required_frames": True}, {"width": 0}, {"warmup_frames": -1},
                        {"startup_timeout_seconds": float("nan")}, {"show_window": "true"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                RecognitionSettings(configuration(**changes))


class CameraTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.settings = RecognitionSettings(configuration())
        self.start_error = False
        self.stop_error = False
        self.clock = Clock()
        self.timestamps = iter([1, 1, 2])
        owner = self
        class Camera:
            def __init__(self, cfg):
                self.serial = cfg["camera"]["serial"]
                owner.events.append(("construct", self.serial))
            def start(self):
                owner.events.append(("start", self.serial))
                if owner.start_error:
                    raise RuntimeError("device start failure")
            def stop(self):
                owner.events.append(("stop", self.serial))
                if owner.stop_error:
                    raise RuntimeError("device stop failure")
            def get_aligned_frames(self, **_kwargs):
                owner.clock.now += 1
                return {"timestamp_ms": next(owner.timestamps, 2)}
        self.switcher = RecognitionCameraSwitcher(self.settings, Camera, self.clock)

    def test_same_camera_reused_across_two_points(self):
        self.switcher.select(self.settings.camera_for_station("LM2"))
        self.switcher.select(self.settings.camera_for_station("LM3"))
        self.assertEqual(self.events, [("construct", "chest-123"), ("start", "chest-123")])

    def test_old_device_closed_before_new_device_started(self):
        self.switcher.select(self.settings.camera_for_station("LM2"))
        self.switcher.select(self.settings.camera_for_station("LM9"))
        self.assertLess(self.events.index(("stop", "chest-123")), self.events.index(("start", "head-456")))

    def test_start_failure_releases_device(self):
        self.start_error = True
        with self.assertRaisesRegex(RuntimeError, "start failure"):
            self.switcher.select(self.settings.camera_for_station("LM2"))
        self.assertIn(("stop", "chest-123"), self.events)
        self.assertIsNone(self.switcher.active_camera)

    def test_stop_failure_prevents_starting_next_device(self):
        self.switcher.select(self.settings.camera_for_station("LM2"))
        self.stop_error = True
        with self.assertRaisesRegex(RuntimeError, "stop failure"):
            self.switcher.select(self.settings.camera_for_station("LM9"))
        self.assertNotIn(("construct", "head-456"), self.events)

    def test_repeated_frame_timestamp_not_counted_twice(self):
        self.switcher.select(self.settings.camera_for_station("LM2"))
        self.assertIsNotNone(self.switcher.read(10))
        self.assertIsNone(self.switcher.read(10))
        self.assertIsNotNone(self.switcher.read(10))

    def test_warmup_timeout_releases_device(self):
        self.switcher.settings = RecognitionSettings(configuration(warmup_frames=4, startup_timeout_seconds=2))
        with self.assertRaisesRegex(RuntimeError, "预热超时"):
            self.switcher.select(self.settings.camera_for_station("LM2"))
        self.assertIsNone(self.switcher.active_camera)

    def test_cancel_guard_aborts_device_selection(self):
        def guard():
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.switcher.select(self.settings.camera_for_station("LM2"), guard)
        self.assertEqual(self.events, [])


class RecognitionTests(unittest.TestCase):
    def make_service(self, frames=None, show=False, window=None):
        clock = Clock()
        settings = RecognitionSettings(configuration())
        detector = SimpleNamespace(detect=lambda _image: [detection()], draw_detections=None)
        switcher = FakeSwitcher(clock, frames)
        window = window or FakeWindow()
        service = RecognitionService(settings, detector, show=show, switcher=switcher, window=window, clock=clock)
        return service, switcher, window

    def test_rgb_recognition_works_with_no_valid_depth(self):
        service, _, _ = self.make_service()
        items = service.scan_station("LM9", 0.05)
        self.assertEqual(items[0]["label"], "cola")
        self.assertEqual(items[0]["camera_role"], "head")
        self.assertNotIn("xyz_camera", items[0])

    def test_count_same_class_only_once_per_frame(self):
        tracker = ConsecutiveLabelTracker(3)
        items = tracker.update([detection(), detection()])
        self.assertFalse(any(item["stable"] for item in items))
        self.assertEqual(tracker.counts["cola"], 1)

    def test_detection_disappearance_resets_confirmation(self):
        tracker = ConsecutiveLabelTracker(2)
        tracker.update([detection()])
        tracker.update([])
        self.assertFalse(tracker.update([detection()])[0]["stable"])

    def test_station_change_does_not_reuse_stability(self):
        service, _, _ = self.make_service()
        service.scan_station("LM2", 0.05)
        self.assertEqual(service.scan_station("LM9", 0.005), [])

    def test_no_frames_is_hardware_error_not_empty_scene(self):
        service, _, _ = self.make_service(frames=[])
        with self.assertRaisesRegex(RuntimeError, "没有有效图像"):
            service.scan_station("LM2", 0.05)

    def test_no_gui_calls_when_display_disabled(self):
        service, _, window = self.make_service()
        service.scan_station("LM2", 0.05)
        self.assertEqual(window.calls, [])

    def test_live_window_receives_station_and_camera(self):
        service, _, window = self.make_service(show=True)
        service.scan_station("LM9", 0.05)
        self.assertEqual(window.calls[0][2], "LM9")
        self.assertEqual(window.calls[0][3].role, "head")

    def test_live_cancel_releases_camera_in_callers_finally(self):
        service, switcher, window = self.make_service(show=True, window=FakeWindow(cancel=True))
        with self.assertRaises(KeyboardInterrupt):
            try:
                service.scan_station("LM9", 0.05)
            finally:
                service.close()
        self.assertTrue(switcher.closed)
        self.assertTrue(window.closed)

    def test_scan_never_calls_grasp_pipeline_or_arm(self):
        scanner = main.CompetitionScanner.__new__(main.CompetitionScanner)
        scanner.recognition_service, _, _ = self.make_service()
        # 没有任何 arm/pipeline 属性也能识别，证明第一阶段不依赖抓取操作。
        self.assertEqual(scanner.scan_station("LM9", 0.05, lambda: None)[0]["label"], "cola")

    def test_grasp_camera_starts_only_after_recognition_device_stopped(self):
        events = []
        scanner = main.CompetitionScanner.__new__(main.CompetitionScanner)
        scanner.recognition_service = SimpleNamespace(close=lambda: events.append("recognition_stop"))
        scanner._camera_started = False
        scanner.camera = SimpleNamespace(start=lambda: events.append("left_start"))
        scanner._ensure_camera(lambda: None)
        self.assertEqual(events, ["recognition_stop", "left_start"])


class PhaseTests(unittest.TestCase):
    def test_recognition_only_point_excluded_from_all_grasp_rounds(self):
        events = []
        clock = Clock()
        settings = RecognitionSettings(configuration())
        service = RecognitionService(settings, SimpleNamespace(detect=lambda _: [detection()]),
                                     show=False, switcher=FakeSwitcher(clock), window=FakeWindow(), clock=clock)
        class Scanner:
            attempts = 0
            def scan_station(self, station, seconds, guard):
                events.append(("scan", station))
                return service.scan_station(station, seconds, guard)
            def finish_recognition(self):
                events.append(("finish_scan", None))
                service.close()
            def grasp_station_once(self, station, *_args, guard, **_kwargs):
                guard()
                events.append(("grasp", station))
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("目标未通过工作空间检查: outside_workspace_x")
                return {"target": "cola"}
            def transport(self, guard):
                guard()
            def release_gripper(self, guard):
                guard()
                return {"result": "released"}
            def close(self):
                service.close()
        class Navigator:
            station = "LM1"
            def connect(self): pass
            def current_station(self): return self.station
            def assert_at(self, station):
                if station != self.station: raise RuntimeError("moving")
            def go_to_station(self, station): self.station = station
            def close(self): pass
        class Rail:
            lowered = False
            def travel(self): pass
            def prepare(self, _station): pass
        cfg = {"navigation": {}, "competition": {
            "scan_route": ["LM2", "LM9"], "grasp_priority": ["LM2"], "home_station": "LM2",
        }}
        args = SimpleNamespace(execute=True, confirm_calibration=True, navigation_only=False,
                               scan_seconds=0.05, match_seconds=480, grasp_timeout_seconds=1)
        scanner = Scanner()
        with patch.object(main, "speak_blocking", side_effect=lambda text: events.append(("speak", text)) or True):
            result = main.run_competition(cfg, args, navigator_factory=lambda _: Navigator(),
                                         scanner_factory=lambda *_: scanner, rail_factory=Rail)
        self.assertEqual([station for kind, station in events if kind == "scan"], ["LM2", "LM9"])
        self.assertEqual([station for kind, station in events if kind == "grasp"], ["LM2", "LM2"])
        self.assertLess(events.index(("scan", "LM9")), events.index(("grasp", "LM2")))
        self.assertLess(events.index(("finish_scan", None)), events.index(("grasp", "LM2")))
        self.assertEqual(len([item for item in events if item[0] == "speak"]), 1)
        self.assertEqual(result["announced"], ["cola"])

    def test_exit_point_cannot_accidentally_be_recognition_point(self):
        with self.assertRaisesRegex(ValueError, "exit_station"):
            main.competition_plan({"competition": {"scan_route": ["LM2", "LM8", "LM9"]}})

    def test_grasp_only_station_can_still_be_retried(self):
        plan = main.competition_plan({"competition": {"scan_route": ["LM2", "LM9"], "grasp_priority": ["LM3", "LM2"]}})
        self.assertEqual(plan.grasp_retry_route, ("LM2", "LM3"))

    def test_preview_never_constructs_hardware(self):
        cfg = {"competition": {"scan_route": ["LM2", "LM9"], "grasp_priority": ["LM2"]}}
        def forbidden(*_args):
            self.fail("preview created hardware")
        args = main.build_parser().parse_args([])
        result = main.run_competition(cfg, args, navigator_factory=forbidden, scanner_factory=forbidden, rail_factory=forbidden)
        self.assertEqual(result["scan_route"], ["LM2", "LM9"])

    def test_far_navigation_point_keeps_logical_station_camera_assignment(self):
        cfg = {"competition": {"scan_route": ["LM2", "LM9"], "grasp_priority": ["LM2"],
                               "scan_nav_stations": {"LM9": "LM19"}}}
        plan = main.competition_plan(cfg)
        self.assertEqual(plan.scan_nav_stations["LM9"], "LM19")
        self.assertEqual(RecognitionSettings(configuration()).camera_for_station("LM9").role, "head")


class WindowTests(unittest.TestCase):
    def make_window(self, folder, key):
        cv = SimpleNamespace(
            rectangle=lambda *_: None, putText=lambda *_: None, imshow=lambda *_: None,
            FONT_HERSHEY_SIMPLEX=0, LINE_AA=0, WND_PROP_VISIBLE=0,
            waitKey=lambda _: key, getWindowProperty=lambda *_: 1,
            imwrite=lambda path, _: Path(path).write_bytes(b"fake-image") or True,
            destroyWindow=lambda _: None,
        )
        canvas = SimpleNamespace(shape=(480, 640, 3))
        window = RecognitionWindow(SimpleNamespace(draw_detections=lambda *_args, **_kw: canvas), Path(folder))
        return window, cv

    def test_s_key_saves_image_and_matching_camera_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            window, cv = self.make_window(folder, ord("s"))
            with patch.dict(sys.modules, {"cv2": cv}):
                window.show(None, [detection()], "LM9", RecognitionSettings(configuration()).cameras["head"], 5)
                window.close()
            files = list(Path(folder).glob("*.json"))
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(data["station"], "LM9")
            self.assertEqual(data["camera_serial"], "head-456")
            self.assertTrue(files[0].with_suffix(".jpg").exists())

    def test_q_key_raises_cancel_instead_of_entering_grasp(self):
        with tempfile.TemporaryDirectory() as folder:
            window, cv = self.make_window(folder, ord("q"))
            with patch.dict(sys.modules, {"cv2": cv}), self.assertRaises(KeyboardInterrupt):
                window.show(None, [], "LM9", RecognitionSettings(configuration()).cameras["head"], 5)


if __name__ == "__main__":
    unittest.main()
