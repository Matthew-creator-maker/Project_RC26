"""RealMan 机械臂的最小测试适配器。

默认 dry-run，不导入 SDK、不连接硬件。只有 execute=True 时才真实执行。
"""

import ctypes
import importlib
import math
import sys
import time
from typing import Any, Dict, List, Optional, Sequence


def check_sdk():
    """导入并校验 API2 本地库；不会创建机械臂连接。"""
    try:
        module = importlib.import_module("Robotic_Arm.rm_robot_interface")
    except (ImportError, OSError) as exc:
        raise RuntimeError("无法加载 RealMan API2；请安装 Robotic_Arm 并检查 Linux .so 架构及依赖") from exc
    cls = module.RoboticArm
    for name in ("rm_create_robot_arm", "rm_delete_robot_arm", "rm_movej", "rm_movej_p",
                 "rm_movel", "rm_get_current_arm_state", "rm_set_gripper_release",
                 "rm_set_gripper_pick_on", "rm_set_arm_stop"):
        if not callable(getattr(cls, name, None)):
            raise RuntimeError(f"当前 RealMan SDK 缺少 {name}，需要 API2 兼容版本")
    return module


class RealManArm:
    def __init__(self, config: Dict[str, Any], execute: bool = False):
        arm_cfg = config.get("arm", {})
        self.ip = str(arm_cfg.get("ip", ""))
        self.port = int(arm_cfg.get("port", 8080))
        self.joint_speed = int(arm_cfg.get("joint_speed", 5))
        self.cartesian_speed = int(arm_cfg.get("cartesian_speed", 5))
        if not self.ip or not 1 <= self.port <= 65535:
            raise ValueError("arm.ip/port 无效")
        for name, value in (("joint_speed", self.joint_speed), ("cartesian_speed", self.cartesian_speed)):
            if not 1 <= value <= 100:
                raise ValueError(f"arm.{name} 必须是 1~100 的速度百分比")
        self.execute = execute
        self.arm: Optional[Any] = None
        self._sdk_module: Optional[Any] = None

    def connect(self) -> None:
        if not self.execute:
            print("[DRY-RUN] 不连接机械臂。")
            return
        if self.arm is not None:
            return
        module = check_sdk()
        self._sdk_module = module
        robotic_arm_class = getattr(module, "RoboticArm")
        thread_mode = getattr(module, "rm_thread_mode_e").RM_TRIPLE_MODE_E
        self.arm = robotic_arm_class(thread_mode)
        handle = self.arm.rm_create_robot_arm(self.ip, self.port)
        handle_id = getattr(handle, "id", None)
        if handle_id is None or int(handle_id) < 0:
            self.disconnect()
            raise RuntimeError(f"机械臂连接失败，handle={handle!r}")
        print(f"机械臂已连接，handle id={handle_id}")

    def _require_connected(self) -> Any:
        if not self.execute:
            return None
        if self.arm is None:
            raise RuntimeError("机械臂尚未连接")
        return self.arm

    @staticmethod
    def _check_result(action: str, result: Any) -> None:
        if result != 0:
            raise RuntimeError(f"{action}失败，SDK 返回: {result}")

    def movej(self, joints_deg: Sequence[float]) -> None:
        command = [float(value) for value in joints_deg]
        if len(command) != 6 or not all(math.isfinite(x) for x in command):
            raise ValueError("关节角必须包含 6 个数")
        if not self.execute:
            print(f"[DRY-RUN] movej: {command}")
            return
        arm = self._require_connected()
        result = arm.rm_movej(command, self.joint_speed, 0, 0, 1)
        self._check_result("movej", result)

    def get_current_pose(self) -> List[float]:
        if not self.execute:
            raise RuntimeError("dry-run 模式必须通过 --end-pose 提供末端位姿")
        arm = self._require_connected()
        result, current_state = arm.rm_get_current_arm_state()
        self._check_result("读取机械臂状态", result)
        pose = current_state.get("pose")
        if pose is None or len(pose) != 6:
            raise RuntimeError(f"机械臂状态中没有有效 pose: {current_state!r}")
        result_pose = [float(value) for value in pose]
        if not all(math.isfinite(x) for x in result_pose):
            raise RuntimeError("实时机械臂位姿含 NaN/Inf")
        return result_pose


    def get_current_joints(self) -> List[float]:
        """读取当前 6 轴关节角（度），供逆解使用当前构型作为参考。"""
        if not self.execute:
            raise RuntimeError("dry-run 模式没有实时关节角")
        arm = self._require_connected()
        result, current_state = arm.rm_get_current_arm_state()
        self._check_result("读取机械臂状态", result)
        joints = current_state.get("joint")
        if joints is None:
            joints = current_state.get("joints")
        if joints is None or len(joints) != 6:
            raise RuntimeError(f"机械臂状态中没有有效 joint: {current_state!r}")
        values = [float(value) for value in joints]
        if not all(math.isfinite(x) for x in values):
            raise RuntimeError("实时关节角含 NaN/Inf")
        return values

    def validate_pose_ik(self, pose: Sequence[float], config: Dict[str, Any], name: str) -> List[float]:
        """运动前逆解并检查肘部软限位；失败时绝不发送运动指令。"""
        command = [float(value) for value in pose]
        if len(command) != 6 or not all(math.isfinite(x) for x in command):
            raise ValueError("笛卡尔位姿必须包含 6 个有限数值")
        safety = config.get("grasp_test", {}).get("motion_safety", {})
        if not safety.get("ik_precheck", True):
            return []
        if not self.execute:
            return []

        arm = self._require_connected()
        module = self._sdk_module
        solve = getattr(arm, "rm_algo_inverse_kinematics", None)
        params_type = getattr(module, "rm_inverse_kinematics_params_t", None) if module else None
        if not callable(solve) or params_type is None:
            if safety.get("require_ik_precheck", True):
                raise RuntimeError(
                    f"{name}未通过运动安全检查: 当前 RealMan SDK 缺少逆解预检接口；"
                    "为避免盲目运动已拒绝执行。可升级 SDK，或在确认风险后将 "
                    "grasp_test.motion_safety.require_ik_precheck 设为 false"
                )
            print(f"[运动安全] {name}: SDK 无逆解接口，跳过 IK 预检。", flush=True)
            return []

        q_ref = self.get_current_joints()
        try:
            result, q_out = solve(params_type(q_ref, command, 1))
        except Exception as exc:
            raise RuntimeError(f"{name}未通过运动安全检查: IK 调用异常: {exc}") from exc
        if result != 0 or q_out is None or len(q_out) != 6:
            raise RuntimeError(f"{name}未通过运动安全检查: IK 无有效解，返回={result}")
        joints = [float(value) for value in q_out]
        if not all(math.isfinite(x) for x in joints):
            raise RuntimeError(f"{name}未通过运动安全检查: IK 解含 NaN/Inf")

        j3_min_abs = float(safety.get("j3_min_abs_deg", 8.0))
        if abs(joints[2]) < j3_min_abs:
            raise RuntimeError(
                f"{name}未通过运动安全检查: IK 预测 J3={joints[2]:.2f}°，"
                f"小于软限位 |J3|>={j3_min_abs:.2f}°，拒绝接近完全伸直姿态"
            )

        limit_check = getattr(arm, "rm_algo_ikine_check_joint_position_limit", None)
        if callable(limit_check):
            try:
                # 新版 RealMan Python SDK 按官方接口可直接接收 list[float]。
                exceeded = limit_check(joints)
            except ctypes.ArgumentError:
                # 兼容部分旧版/异常绑定：底层 C 接口要求 const float*，
                # 但 Python 包装层未自动把 list 转成 LP_c_float。
                joint_buffer = (ctypes.c_float * 6)(*joints)
                exceeded = limit_check(joint_buffer)

            if exceeded not in (0, -1):
                raise RuntimeError(
                    f"{name}未通过运动安全检查: IK 解触发厂家关节位置限位，关节={exceeded}"
                )
        print(f"[运动安全] {name}: IK={joints}，J3 裕量检查通过。", flush=True)
        return joints

    def movej_p(self, pose: Sequence[float]) -> None:
        command = [float(value) for value in pose]
        if len(command) != 6 or not all(math.isfinite(x) for x in command):
            raise ValueError("笛卡尔位姿必须包含 6 个数")
        if not self.execute:
            print(f"[DRY-RUN] movej_p: {command}")
            return
        arm = self._require_connected()
        result = arm.rm_movej_p(command, self.cartesian_speed, 0, 0, 1)
        self._check_result("movej_p", result)

    def movel(self, pose: Sequence[float]) -> None:
        command = [float(value) for value in pose]
        if len(command) != 6 or not all(math.isfinite(x) for x in command):
            raise ValueError("笛卡尔位姿必须包含 6 个数")
        if not self.execute:
            print(f"[DRY-RUN] movel: {command}")
            return
        arm = self._require_connected()
        result = arm.rm_movel(command, self.cartesian_speed, 0, 0, 1)
        self._check_result("movel", result)

    def close_gripper(self, config: Dict[str, Any]) -> None:
        arm_cfg = config.get("arm", {})
        speed = int(arm_cfg.get("gripper_speed", 200))
        force = int(arm_cfg.get("gripper_force", 1000))
        timeout = int(arm_cfg.get("gripper_timeout_seconds", 10))
        if not self.execute:
            print(
                f"[DRY-RUN] close gripper: speed={speed}, "
                f"force={force}, timeout={timeout}"
            )
            return
        arm = self._require_connected()
        result = arm.rm_set_gripper_pick_on(speed, force, True, timeout)
        # 空夹也可能让 SDK 返回 0；必须看到力控停止才视为夹到了东西。
        # API2 对 -4 的定义随版本变化，故只依据夹爪反馈决定是否继续。
        if result not in (0, -4):
            self._check_result("夹爪闭合", result)
        read_state = getattr(arm, "rm_get_gripper_state", None)
        if not callable(read_state):
            raise RuntimeError("SDK 不支持读取夹爪状态，无法确认夹持；停止自动撤回")

        # 允许到位事件稍晚于阻塞指令的返回，读取状态不会再次发送夹爪动作。
        deadline = time.monotonic() + 2.0
        while True:
            try:
                state_result, state = read_state()
            except Exception as exc:
                raise RuntimeError("无法读取夹爪状态，停止自动撤回") from exc
            print(f"[夹爪确认] SDK={result}，状态读取={state_result}，状态={state}", flush=True)
            if (state_result == 0 and isinstance(state, dict)
                    and state.get("status") == 1
                    and state.get("error") == 0
                    and state.get("mode") == 6):
                print("[夹爪确认] 夹爪因力控接触停止。", flush=True)
                return
            if (state_result != 0 or not isinstance(state, dict)
                    or state.get("status") != 1 or state.get("error") != 0
                    or state.get("mode") != 4 or time.monotonic() >= deadline):
                break
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        raise RuntimeError(
            f"夹爪未确认力控接触停止（SDK={result}，状态={state}）；"
            "停止自动撤回，请检查夹爪是否正夹着目标或碰到其他结构"
        )

    def open_gripper(self, config: Dict[str, Any]) -> None:
        arm_cfg = config.get("arm", {})
        speed = int(arm_cfg.get("gripper_release_speed", 500))
        timeout = int(arm_cfg.get("gripper_timeout_seconds", 10))
        if not self.execute:
            print(f"[DRY-RUN] open gripper: speed={speed}, timeout={timeout}")
            return
        arm = self._require_connected()
        result = arm.rm_set_gripper_release(speed, True, timeout)
        self._check_result("夹爪松开", result)

    def stop_best_effort(self) -> None:
        if self.execute and self.arm is not None:
            try:
                self._check_result("机械臂停止", self.arm.rm_set_arm_stop())
            except Exception as exc:
                print(f"未能确认机械臂停止: {exc}", file=sys.stderr)

    def disconnect(self) -> None:
        if self.arm is not None:
            try:
                self.arm.rm_delete_robot_arm()
            finally:
                self.arm = None
