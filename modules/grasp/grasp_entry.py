"""Bridge from the competition mission to the integrated grasp controller."""
from __future__ import annotations

import importlib
import math


def load_grasp_module(directory=None):
    """Lazy-load the integrated grasp controller.

    ``directory`` is accepted only for backward compatibility with older callers;
    runtime code no longer depends on a version-named folder or ``sys.path`` injection.
    """
    return importlib.import_module("modules.grasp.grasp_controller")


def transport_joints(config):
    value = config.get("arm_transport", {})
    joints = value.get("joints_deg")
    if value.get("configured") is not True:
        raise ValueError("请先在 task_config.json 填写实测 arm_transport 收拢位并设 configured=true")
    if (not isinstance(joints, list) or len(joints) != 6
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in joints)):
        raise ValueError("arm_transport.joints_deg 必须为 6 个有限实测角度（度）")
    return list(joints)


class GraspProgram:
    """Prepared action program for one LM station.

    release: detect + grasp + put back + open gripper (LM3)
    carry:   detect + grasp + keep gripper closed (LM5)
    place:   no vision, just open the gripper at the arrival pose (LM7)
    """

    def __init__(self, config, point, args):
        self.action = point["action"]
        if self.action not in {"release", "carry", "place"}:
            raise ValueError(f"不支持的抓取动作: {self.action!r}")
        self.joints = transport_joints(config)
        self.module = load_grasp_module()

        self.argv = ["--config", str(config["perception_config"]), "--execute", "--action", self.action]
        target = point.get("target")
        if target:
            self.argv.extend(["--target", target])
        if args.confirm_calibration:
            self.argv.append("--confirm-calibration")
        self.argv.append("--show" if args.show else "--no-display")

        self.args = self.module.build_parser().parse_args(self.argv)
        # 静态检查和模型预加载；不会启动相机或连接机械臂。
        self.prepared = self.module.prepare(self.args)
        self.arm = None

    def _ensure_arm(self, guard):
        guard()
        if self.arm is None:
            self.arm = self.module.RealManArm(self.prepared["config"], execute=True)
        self.arm.connect()
        guard()
        return self.arm

    def transport(self, guard):
        """Move the arm to the measured transport pose without changing gripper state."""
        arm = self._ensure_arm(guard)
        try:
            arm.movej(self.joints)
            guard()
        except BaseException:
            arm.stop_best_effort()
            raise

    def run(self, point, guard):
        if point.get("action") != self.action:
            raise ValueError("GraspProgram 动作与调用动作不一致")
        arm = self._ensure_arm(guard)
        # test_detect_and_grasp.py owns the action sequence; this object owns the arm connection.
        return self.module.main(
            self.argv,
            guard=guard,
            prepared=self.prepared,
            arm=arm,
        )

    def close(self):
        if self.arm is not None:
            self.arm.disconnect()
            self.arm = None
