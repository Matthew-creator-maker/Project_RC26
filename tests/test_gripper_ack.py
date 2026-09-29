"""Check that arm withdrawal requires actual gripper force-stop feedback."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.grasp.realman_arm import RealManArm


class GripperAckTests(unittest.TestCase):
    def setUp(self):
        self.config = {"arm": {"ip": "127.0.0.1", "gripper_force": 200}}
        self.state = {"enable_state": 1, "status": 1, "error": 0,
                      "mode": 6, "actpos": 412}
        self.sdk_result = -4
        self.states = []
        self.reads = 0
        self.arm = RealManArm(self.config, execute=True)
        self.arm.arm = SimpleNamespace(
            rm_set_gripper_pick_on=lambda *args: self.sdk_result,
            rm_get_gripper_state=self._read_state,
        )

    def _read_state(self):
        self.reads += 1
        return 0, (self.states.pop(0) if self.states else self.state)

    def test_force_stop_after_ambiguous_sdk_result_needs_no_opening_range(self):
        self.arm.close_gripper(self.config)
        self.assertEqual(self.reads, 1)

    def test_force_stop_after_successful_sdk_result(self):
        self.sdk_result = 0
        self.arm.close_gripper(self.config)
        self.assertEqual(self.reads, 1)

    def test_transition_from_closing_to_force_stop(self):
        self.states = [{**self.state, "mode": 4}, self.state]
        with patch("modules.grasp.realman_arm.time.sleep", return_value=None):
            self.arm.close_gripper(self.config)
        self.assertEqual(self.reads, 2)

    def test_still_closing_must_not_withdraw(self):
        self.state["mode"] = 4
        with patch("modules.grasp.realman_arm.time.monotonic", side_effect=[0.0, 3.0]):
            with self.assertRaisesRegex(RuntimeError, "停止自动撤回"):
                self.arm.close_gripper(self.config)

    def test_empty_full_closure_is_not_a_grasp_even_if_sdk_returns_success(self):
        self.sdk_result = 0
        self.state["mode"] = 2
        with self.assertRaisesRegex(RuntimeError, "未确认力控接触停止"):
            self.arm.close_gripper(self.config)

    def test_offline_and_gripper_fault_cannot_be_acknowledged(self):
        for state_change in ({"status": 0}, {"error": 1}):
            with self.subTest(state_change=state_change):
                self.state.update(state_change)
                with self.assertRaisesRegex(RuntimeError, "停止自动撤回"):
                    self.arm.close_gripper(self.config)
                self.state.update({"status": 1, "error": 0})

    def test_other_sdk_errors_remain_failures(self):
        self.sdk_result = -2
        with self.assertRaisesRegex(RuntimeError, "SDK 返回: -2"):
            self.arm.close_gripper(self.config)
        self.assertEqual(self.reads, 0)


if __name__ == "__main__":
    unittest.main()
