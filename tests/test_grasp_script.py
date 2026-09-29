"""Tests for the real grasp entry with arm/camera motion replaced by fakes."""
import copy
import os
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules.grasp.grasp_entry import load_grasp_module

try:
    g = load_grasp_module()
except ImportError:
    g = None


@unittest.skipIf(g is None, "请在 requirements-ubuntu22.txt 对应环境运行完整抓取测试")
class GraspTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.config = g.load_config(str(ROOT / "config/perception.yaml"))
        self.config["grasp_test"].update(workspace_configured=True, stable_samples=2, timeout_seconds=0.2)
        self.config["arm"]["observation_settle_seconds"] = 0
        self.config["grasp_test"]["workspace_m"] = {key: [-2, 2] for key in "xyz"}
        self.targets = [{"label": "Cola", "stable": True, "localization_ok": True,
                         "confidence": 0.9, "xyz_camera": [0.1, 0.1, 0.3]}]
        self.camera_error = None
        self.stop_error = None
        self.arm_error = None
        self.camera_frames = True
        events, owner = self.events, self

        class Arm:
            def __init__(self, *a, **kw):
                self.connected = False
                self.lines = 0
            def connect(self):
                if not self.connected:
                    events.append("arm_connect")
                    self.connected = True
            def movej(self, joints):
                events.append("movej")
            def get_current_pose(self):
                events.append("read_pose")
                return [0] * 6
            def movej_p(self, pose):
                events.append("transition")
            def open_gripper(self, cfg):
                events.append("open_gripper")
            def movel(self, pose):
                self.lines += 1
                events.append(f"movel:{self.lines}")
                if owner.arm_error:
                    raise owner.arm_error
            def close_gripper(self, cfg):
                events.append("close_gripper")
            def disconnect(self):
                if self.connected:
                    events.append("arm_disconnect")
                    self.connected = False
            def stop_best_effort(self):
                events.append("arm_stop")

        class Camera:
            def __init__(self, *_):
                events.append("camera_construct")
            def start(self):
                events.append("camera_start")
                if owner.camera_error:
                    raise owner.camera_error
            def get_aligned_frames(self, timeout_ms=1000):
                if not owner.camera_frames:
                    return None
                return {"color_image": g.np.zeros((8, 8, 3), dtype=g.np.uint8),
                        "depth_frame": None, "intrinsics": None}
            def stop(self):
                events.append("camera_stop")
                if owner.stop_error:
                    raise owner.stop_error

        class Pipeline:
            def __init__(self, *_):
                self.detector = SimpleNamespace(class_names={0: "Cola", 1: "Water"}, detect=lambda *_: [])
            def reset_stability(self):
                events.append("reset")
            def process_frame(self, *a):
                events.append("detect")
                return owner.targets

        self.Arm = Arm
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("load_config", lambda *_: copy.deepcopy(self.config)),
                            ("RealManArm", Arm), ("RealSenseCamera", Camera),
                            ("PerceptionPipeline", Pipeline), ("check_sdk", lambda: None)):
            self.stack.enter_context(patch.object(g, name, value))
        self.stack.enter_context(patch.object(g.time, "sleep", lambda _: None))
        self.argv = ["--execute", "--confirm-calibration", "--no-display", "--target", "Cola"]

    def test_carry_grasps_and_does_not_release_after_retreat(self):
        result = g.main([*self.argv, "--action", "carry"])
        self.assertEqual(result["result"], "grasp_held")
        self.assertEqual(self.events.count("close_gripper"), 1)
        # One opening before approach only; no opening after the object is grasped.
        self.assertEqual(self.events.count("open_gripper"), 1)
        self.assertEqual(self.events.count("movel:1"), 1)
        self.assertIn("movel:2", self.events)

    def test_release_grasps_puts_back_opens_and_retreats(self):
        result = g.main([*self.argv, "--action", "release"])
        self.assertEqual(result["result"], "grasp_released")
        self.assertEqual(self.events.count("close_gripper"), 1)
        # First open prepares the grasp; second open releases at the placement height.
        self.assertEqual(self.events.count("open_gripper"), 2)
        self.assertIn("movel:4", self.events)

    def test_place_only_opens_gripper_without_camera_or_detection(self):
        result = g.main(["--execute", "--no-display", "--action", "place"])
        self.assertEqual(result["result"], "released")
        self.assertEqual(self.events.count("open_gripper"), 1)
        self.assertNotIn("camera_construct", self.events)
        self.assertNotIn("detect", self.events)
        self.assertNotIn("close_gripper", self.events)

    def test_external_arm_is_not_disconnected_by_action(self):
        arm = self.Arm()
        args = g.build_parser().parse_args([*self.argv, "--action", "carry"])
        prepared = g.prepare(args)
        g.run_prepared(args, prepared, arm=arm)
        self.assertTrue(arm.connected)
        self.assertNotIn("arm_disconnect", self.events)
        arm.disconnect()

    def test_preflight_does_not_open_camera_or_arm(self):
        result = g.main([*self.argv, "--check-only", "--action", "carry"])
        self.assertEqual(result["result"], "preflight_passed")
        self.assertEqual(self.events, [])

    def test_collected_target_timeout_no_gripper(self):
        self.camera_frames = False
        self.config["grasp_test"]["timeout_seconds"] = 0.01
        with self.assertRaises(TimeoutError):
            g.main([*self.argv, "--action", "carry"])
        self.assertNotIn("open_gripper", self.events)
        self.assertIn("camera_stop", self.events)
        self.assertIn("arm_stop", self.events)
        self.assertIn("arm_disconnect", self.events)

    def test_camera_start_failure_disconnects_arm(self):
        self.camera_error = RuntimeError("USB unavailable")
        with self.assertRaisesRegex(RuntimeError, "USB unavailable"):
            g.main([*self.argv, "--action", "carry"])
        self.assertNotIn("open_gripper", self.events)
        self.assertIn("arm_disconnect", self.events)

    def test_cleanup_does_not_mask_original_failure(self):
        self.camera_error = RuntimeError("original USB failure")
        self.stop_error = RuntimeError("cleanup failure")
        with self.assertRaisesRegex(RuntimeError, "original USB failure"):
            g.main([*self.argv, "--action", "carry"])
        self.assertIn("arm_disconnect", self.events)

    def test_cleanup_failure_after_success_is_reported(self):
        self.stop_error = RuntimeError("USB cleanup failed")
        with self.assertRaisesRegex(RuntimeError, "资源清理失败"):
            g.main([*self.argv, "--action", "carry"])
        self.assertIn("arm_disconnect", self.events)

    def test_failed_arm_approach_no_close_or_retry(self):
        self.arm_error = RuntimeError("motion failed")
        with self.assertRaisesRegex(RuntimeError, "motion failed"):
            g.main([*self.argv, "--action", "carry"])
        self.assertNotIn("close_gripper", self.events)
        self.assertIn("arm_stop", self.events)

    def test_guard_aborts_before_grasp_if_chassis_moves(self):
        def guard():
            if "detect" in self.events:
                raise RuntimeError("chassis moving")
        with self.assertRaisesRegex(RuntimeError, "chassis moving"):
            g.main([*self.argv, "--action", "carry"], guard=guard)
        self.assertNotIn("open_gripper", self.events)

    def test_no_display_never_calls_gui_cleanup(self):
        with patch.object(g.cv2, "destroyAllWindows", side_effect=AssertionError("GUI invoked")):
            result = g.main([*self.argv, "--action", "carry"])
        self.assertEqual(result["result"], "grasp_held")

    def test_ssh_show_rejected_before_motion(self):
        with patch.object(g.sys, "platform", "linux"), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "图形桌面"):
                g.main(["--execute", "--confirm-calibration", "--show", "--action", "carry"])
        self.assertEqual(self.events, [])

    def test_bad_label_fails_before_motion(self):
        with self.assertRaisesRegex(ValueError, "模型没有"):
            g.main([*self.argv, "--target", "Coke", "--action", "carry"])
        self.assertEqual(self.events, [])

    def test_missing_workspace_marker_fails_before_motion(self):
        self.config["grasp_test"]["workspace_configured"] = False
        with self.assertRaisesRegex(RuntimeError, "workspace"):
            g.main([*self.argv, "--action", "carry"])
        self.assertEqual(self.events, [])

    def test_outside_workspace_never_grasps(self):
        self.config["grasp_test"]["workspace_m"] = {key: [10, 11] for key in "xyz"}
        with self.assertRaisesRegex(RuntimeError, "工作空间"):
            g.main([*self.argv, "--action", "carry"])
        self.assertNotIn("open_gripper", self.events)

    def test_recognize_only_moves_to_observation_but_no_gripper(self):
        result = g.main([*self.argv, "--recognize-only", "--action", "carry"])
        self.assertEqual(result["result"], "recognized")
        self.assertIn("movej", self.events)
        self.assertNotIn("open_gripper", self.events)

    def test_cli_cancel_is_130_not_success(self):
        with patch.object(g, "main", side_effect=KeyboardInterrupt):
            self.assertEqual(g.cli([]), 130)

    def test_cli_failure_is_nonzero(self):
        with patch.object(g, "main", side_effect=RuntimeError("failed")):
            self.assertEqual(g.cli([]), 1)


@unittest.skipIf(g is None, "需要视觉运行依赖")
class SdkAdapterTests(unittest.TestCase):
    def test_arm_connect_is_idempotent(self):
        from modules.grasp import realman_arm
        calls = []
        sdk = SimpleNamespace(
            rm_create_robot_arm=lambda *a: calls.append("connect") or SimpleNamespace(id=1),
            rm_delete_robot_arm=lambda: calls.append("disconnect") or 0)
        module = SimpleNamespace(RoboticArm=lambda mode: sdk,
                                 rm_thread_mode_e=SimpleNamespace(RM_TRIPLE_MODE_E=2))
        cfg = {"arm": {"ip": "127.0.0.1", "port": 8080}}
        with patch.object(realman_arm, "check_sdk", return_value=module):
            arm = realman_arm.RealManArm(cfg, execute=True)
            arm.connect()
            arm.connect()
            arm.disconnect()
            arm.disconnect()
        self.assertEqual(calls, ["connect", "disconnect"])

    def test_nonfinite_pose_does_not_reach_sdk(self):
        from modules.grasp import realman_arm
        arm = realman_arm.RealManArm({"arm": {"ip": "127.0.0.1"}}, execute=True)
        arm.arm = SimpleNamespace(rm_movel=lambda *a: self.fail("不得发送 NaN"))
        with self.assertRaises(ValueError):
            arm.movel([float("nan")] + [0] * 5)

    def test_frame_timeout_is_forwarded_without_starting_camera(self):
        from modules.perception import camera_manager
        calls = []
        camera = camera_manager.RealSenseCamera.__new__(camera_manager.RealSenseCamera)
        camera.started = True
        camera.pipeline = SimpleNamespace(try_wait_for_frames=lambda ms: calls.append(ms) or (False, None))
        self.assertIsNone(camera.get_aligned_frames(timeout_ms=123))
        self.assertEqual(calls, [123])


if __name__ == "__main__":
    unittest.main(verbosity=2)
