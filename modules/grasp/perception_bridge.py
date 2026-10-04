"""集成眼在手上视觉模块；不调用旧 object_nav_grab.run()。

没有底盘连接。每次动作前检查底盘仍在当前站点，动作结果只表示 SDK
序列完成，不表示已检测到物品被夹住。
"""
from __future__ import annotations

import argparse
import math
import time
from pathlib import Path


class PerceptionBridge:
    def __init__(self, directory: Path, config_path: Path | None, navigation_config: dict,
                 points: list[dict], confirm_calibration=False, show=False):
        from . import grasp_controller as grasp
        self.g = grasp
        default_config = Path(__file__).resolve().parents[2] / "config" / "perception.yaml"
        self.config = grasp.load_config(str(config_path or default_config))
        self.show = show
        self.arm = None
        self.camera = None
        needs_grab = any(point["action"] == "grab" for point in points)
        if needs_grab:
            grasp.require_execution_safety(argparse.Namespace(
                execute=True, confirm_calibration=confirm_calibration), self.config)
            workspace = self.config.get("grasp_test", {}).get("workspace_m", {})
            for axis in ("x", "y", "z"):
                limits = workspace.get(axis)
                if (not isinstance(limits, list) or len(limits) != 2
                        or any(type(x) not in (int, float) or not math.isfinite(x) for x in limits)
                        or limits[0] >= limits[1]):
                    raise ValueError(f"workspace_m.{axis} 必须是有效的 [最小值, 最大值]")
            for key in ("grasp_orientation_rad", "final_tool_offset_m", "transition_tool_offset_m"):
                values = self.config.get("arm", {}).get(key)
                if (not isinstance(values, list) or len(values) != 3
                        or any(type(x) not in (int, float) or not math.isfinite(x) for x in values)):
                    raise ValueError(f"arm.{key} 必须是 3 个有限数值")
            grasp.finite_vector(
                self.config.get("arm", {}).get("target_offset_base_m", [0.0, 0.0, 0.0]),
                3, "arm.target_offset_base_m",
            )
        transport = navigation_config.get("arm_transport", {})
        self.transport_joints = self._joints(transport.get("joints_deg"), "arm_transport.joints_deg")
        if transport.get("configured") is not True:
            raise ValueError("请实测并配置 config/task_config.json 的机械臂收拢位 arm_transport")
        self.observation_joints = self._joints(
            self.config.get("arm", {}).get("observation_joints_deg"), "observation_joints_deg")
        test_cfg = self.config.get("grasp_test", {})
        self.sample_count = test_cfg.get("stable_samples", 10)
        if type(self.sample_count) is not int or self.sample_count < 1:
            raise ValueError("stable_samples 必须是正整数")
        self.timeout = float(test_cfg.get("timeout_seconds", 30))
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("视觉超时必须为有限正数")
        self.settle = float(self.config.get("arm", {}).get("observation_settle_seconds", 1.5))
        if not math.isfinite(self.settle) or self.settle < 0:
            raise ValueError("观测位等待时间无效")
        self.transform = grasp.camera_to_end_matrix(self.config)
        # 在任何真实导航之前检查模型、标签和依赖。
        self.pipeline = grasp.PerceptionPipeline(self.config)
        labels = set(self.pipeline.detector.class_names.values())
        for point in points:
            if point["action"] != "none" and point["target"] not in labels:
                raise ValueError(f"目标 {point['target']!r} 不在模型中；可用标签: {sorted(labels)}")

    @staticmethod
    def _joints(values, name):
        if not isinstance(values, list) or len(values) != 6:
            raise ValueError(f"{name} 必须填入实测的 6 个关节角（度）")
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in values):
            raise ValueError(f"{name} 含无效数值")
        return values

    def transport(self, guard):
        guard()
        if self.arm is None:
            self.arm = self.g.RealManArm(self.config, execute=True)
            self.arm.connect()
        guard()
        self.arm.movej(self.transport_joints)
        guard()

    def run(self, point: dict, guard):
        guard()
        self.arm.movej(self.observation_joints)
        time.sleep(self.settle)
        guard()
        end_pose = self.arm.get_current_pose()
        camera = self.g.RealSenseCamera(self.config)
        self.camera = camera
        try:
            camera.start()
            self.pipeline.reset_stability()

            class GuardedCamera:
                def get_aligned_frames(inner, timeout_ms=1000):
                    guard()
                    frames = camera.get_aligned_frames(timeout_ms=timeout_ms)
                    guard()
                    return frames

            xyz_camera = self.g.collect_target_points(
                GuardedCamera(), self.pipeline, point["target"], self.sample_count,
                self.timeout, self.show)
            guard()
            print(f"{point['id']} {point['target']} 相机坐标: {xyz_camera.tolist()} m")
            if point["action"] == "recognize":
                return {"result": "recognized", "xyz_camera_m": xyz_camera.tolist()}
            xyz_base = self.g.camera_point_to_base(xyz_camera, end_pose, self.transform)
            poses = self.g.build_grasp_poses(xyz_base, self.config)
            workspace = self.config["grasp_test"]["workspace_m"]
            for name, xyz in (("目标", xyz_base), ("过渡点", poses["transition"][:3]),
                              ("抓取点", poses["final"][:3])):
                ok, reason = self.g.validate_workspace(xyz, workspace)
                if not ok:
                    raise RuntimeError(f"{name}越出工作空间: {reason}")
            for action in (
                lambda: self.arm.movej_p(poses["transition"]),
                lambda: self.arm.open_gripper(self.config),
                lambda: self.arm.movel(poses["final"]),
                lambda: self.arm.close_gripper(self.config),
                lambda: self.arm.movel(poses["transition"]),
            ):
                guard()
                action()
                guard()
            print("抓取动作序列完成；物品持有状态未检测。")
            return {"result": "grasp_sequence_completed", "holding_verified": False,
                    "xyz_base_m": xyz_base.tolist()}
        finally:
            try:
                camera.stop()
            finally:
                self.camera = None
                if self.show:
                    self.g.cv2.destroyAllWindows()

    def close(self):
        if self.arm is not None:
            self.arm.disconnect()
            self.arm = None
