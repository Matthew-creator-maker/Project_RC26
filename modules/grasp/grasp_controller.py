"""相机识别稳定目标并执行一次抓取的受保护测试程序。

默认只做视觉检测和 dry-run。真实运动必须显式传入：
    --execute --confirm-calibration
并且 config/perception.yaml 中 grasp_test.workspace_configured 必须设为 true。
"""

import argparse
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

import cv2
import numpy as np

from modules.perception.camera_manager import RealSenseCamera, load_config
from modules.perception.handeye_transform import (
    camera_point_to_base,
    camera_to_end_matrix,
    matrix_to_realman_pose,
    median_point,
    offset_pose_in_tool,
    realman_pose_to_matrix,
    validate_workspace,
)
from modules.perception.perception_pipeline import PerceptionPipeline
from .realman_arm import RealManArm, check_sdk


def find_stable_target(
    results: List[Dict[str, Any]],
    target_label: str,
) -> Optional[Dict[str, Any]]:
    """选择指定类别中已稳定且三维定位有效的最高置信度目标。"""
    candidates = [
        result
        for result in results
        if result.get("label") == target_label
        and result.get("stable") is True
        and result.get("localization_ok") is True
    ]
    return max(candidates, key=lambda item: item["confidence"], default=None)


def collect_target_points(
    camera: RealSenseCamera,
    pipeline: PerceptionPipeline,
    target_label: str,
    sample_count: int,
    timeout_seconds: float,
    show_window: bool,
    guard=None,
) -> np.ndarray:
    """等待稳定目标，收集多帧 xyz_camera 后逐轴取中值。"""
    guard = guard or (lambda: None)
    points: List[Sequence[float]] = []
    start_time = time.monotonic()

    while len(points) < sample_count:
        if time.monotonic() - start_time > timeout_seconds:
            raise TimeoutError(
                f"在 {timeout_seconds:.1f} 秒内没有收集到 "
                f"{sample_count} 个稳定目标点"
            )

        guard()
        remaining = timeout_seconds - (time.monotonic() - start_time)
        frame_data = camera.get_aligned_frames(timeout_ms=max(1, min(1000, int(remaining * 1000))))
        guard()
        if frame_data is None:
            continue
        color_image = frame_data["color_image"]
        results = pipeline.process_frame(
            color_image,
            frame_data["depth_frame"],
            frame_data["intrinsics"],
        )
        guard()
        if time.monotonic() - start_time > timeout_seconds:
            raise TimeoutError("目标推理超过本次识别时间限制")
        target = find_stable_target(results, target_label)
        if target is not None:
            points.append(target["xyz_camera"])
            print(
                f"已采集稳定点 {len(points)}/{sample_count}: "
                f"{target['xyz_camera']} m"
            )
        elif points:
            # 要求采样点连续稳定，目标中断后不混用旧的一组点。
            print("稳定目标中断，清空本轮已采集的三维点。")
            points.clear()

        if show_window:
            display = pipeline.draw_results(color_image, results)
            cv2.putText(
                display,
                f"target={target_label} samples={len(points)}/{sample_count}",
                (10, 25),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (0, 255, 255),
                2,
                cv2.LINE_AA,
            )
            cv2.imshow("detect_and_grasp_test", display)
            if (cv2.waitKey(1) & 0xFF) in (ord("q"), 27):
                raise KeyboardInterrupt("用户取消测试")

    return median_point(points)


def build_grasp_poses(
    xyz_base_m: Sequence[float],
    config: Dict[str, Any],
) -> Dict[str, List[float]]:
    """按照 test_0914.py 的姿态和工具轴偏移生成抓取路径。"""
    arm_cfg = config.get("arm", {})
    orientation = arm_cfg.get("grasp_orientation_rad")
    if orientation is None or len(orientation) != 3:
        raise ValueError("arm.grasp_orientation_rad 必须包含 3 个弧度值")

    grasp_pose = realman_pose_to_matrix(
        [*xyz_base_m, *orientation],
        degrees=False,
    )
    final_pose = offset_pose_in_tool(
        grasp_pose,
        arm_cfg.get("final_tool_offset_m", [0.0, 0.0, 0.04]),
    )
    transition_pose = offset_pose_in_tool(
        final_pose,
        arm_cfg.get("transition_tool_offset_m", [0.0, 0.0, -0.15]),
    )
    return {
        "final": matrix_to_realman_pose(final_pose),
        "transition": matrix_to_realman_pose(transition_pose),
    }



def validate_motion_safety(
    arm: RealManArm,
    poses: Dict[str, List[float]],
    config: Dict[str, Any],
) -> None:
    """在任何笛卡尔运动前预检 final/transition，避免边界奇异后才由控制器报错。"""
    safety = config.get("grasp_test", {}).get("motion_safety", {})
    if not safety.get("enabled", True):
        return

    arm.validate_pose_ik(poses["transition"], config, "过渡点")
    arm.validate_pose_ik(poses["final"], config, "最终抓取点")


def approach_waypoints(config: Dict[str, Any]) -> List[List[float]]:
    """识别位到预抓取位之间的关节过渡点，单位为度，按配置顺序执行。"""
    values = config.get("arm", {}).get("approach_waypoints_joints_deg", [])
    if not isinstance(values, list):
        raise ValueError("arm.approach_waypoints_joints_deg 必须是关节角列表")
    return [
        finite_vector(joints, 6, f"arm.approach_waypoints_joints_deg[{index}]")
        for index, joints in enumerate(values, 1)
    ]


def move_to_pregrasp(arm, poses, config, action):
    """先绕行关节过渡点，再由当前构型预检并进入目标相关的预抓取位。"""
    for index, joints in enumerate(approach_waypoints(config), 1):
        print(f"[抓取绕行] 关节过渡点 {index}: {joints}°", flush=True)
        action(lambda joints=joints: arm.movej(joints))
    # 过渡后重新以当前关节构型为 IK 参考，防止沿用识别位的逆解分支。
    validate_motion_safety(arm, poses, config)
    action(lambda: arm.movej_p(poses["transition"]))


def require_execution_safety(
    args: argparse.Namespace,
    config: Dict[str, Any],
) -> None:
    """真实运动前检查显式确认和工作空间配置。"""
    if not args.execute:
        return
    if not args.confirm_calibration:
        raise RuntimeError(
            "真实执行需要同时添加 --confirm-calibration，表示已确认相机安装未改变。"
        )
    if config.get("grasp_test", {}).get("workspace_configured") is not True:
        raise RuntimeError(
            "请先实测 config/perception.yaml 的 grasp_test.workspace_m，"
            "再将 workspace_configured 改为 true。"
        )


def finite_vector(value, length, name):
    if (not isinstance(value, (list, tuple)) or len(value) != length
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in value)):
        raise ValueError(f"{name} 必须包含 {length} 个有限数值")
    return list(value)


def validate_config(config, execute=False, recognize_only=False, action="carry"):
    """在任何运动前检查静态参数；place 动作不依赖相机/手眼标定。"""
    if action not in {"carry", "release", "place"}:
        raise ValueError(f"未知 action: {action!r}")

    # 任何动作都需要一个有效的机械臂连接配置。
    RealManArm(config, execute=False)
    if action == "place":
        return

    arm_cfg, test_cfg = config.get("arm", {}), config.get("grasp_test", {})
    samples = test_cfg.get("stable_samples", 10)
    if type(samples) is not int or samples < 1:
        raise ValueError("grasp_test.stable_samples 必须是正整数")
    for name, value, allow_zero in (
        ("timeout_seconds", test_cfg.get("timeout_seconds", 30), False),
        ("observation_settle_seconds", arm_cfg.get("observation_settle_seconds", 1.5), True),
    ):
        if (type(value) not in (int, float) or not math.isfinite(value)
                or (value < 0 if allow_zero else value <= 0)):
            raise ValueError(f"{name} 不是有效时间")
    finite_vector(arm_cfg.get("observation_joints_deg"), 6, "arm.observation_joints_deg")
    approach_waypoints(config)
    for key in ("grasp_orientation_rad", "final_tool_offset_m", "transition_tool_offset_m"):
        finite_vector(arm_cfg.get(key), 3, "arm." + key)
    camera_to_end_matrix(config)
    if execute and not recognize_only:
        workspace = test_cfg.get("workspace_m", {})
        for axis in ("x", "y", "z"):
            low, high = finite_vector(workspace.get(axis), 2, "workspace_m." + axis)
            if low >= high:
                raise ValueError(f"workspace_m.{axis} 下限必须小于上限")
        safety = test_cfg.get("motion_safety", {})
        if not isinstance(safety, dict):
            raise ValueError("grasp_test.motion_safety 必须是字典")
        j3_margin = safety.get("j3_min_abs_deg", 8.0)
        if type(j3_margin) not in (int, float) or not math.isfinite(j3_margin) or not 0 < j3_margin < 90:
            raise ValueError("grasp_test.motion_safety.j3_min_abs_deg 必须在 0~90 度之间")


def build_parser():
    parser = argparse.ArgumentParser(description="识别目标并执行抓取/放置动作")
    parser.add_argument("--target", default="Cola", help="模型类别，默认 Cola")
    parser.add_argument("--config", default=None, help="YAML 配置文件")
    parser.add_argument("--execute", action="store_true", help="允许真实机械臂运动")
    parser.add_argument("--confirm-calibration", action="store_true", help="确认当前手眼标定有效")
    parser.add_argument("--recognize-only", action="store_true", help="到观测位后只识别，不抓取")
    parser.add_argument("--check-only", action="store_true", help="只检查配置、标签和模型推理，不连接硬件")
    parser.add_argument(
        "--action",
        choices=("carry", "release", "place"),
        default="carry",
        help="carry=抓取保持；release=抓取后放回并松爪；place=不识别，直接松爪",
    )
    display = parser.add_mutually_exclusive_group()
    display.add_argument("--show", action="store_true", help="显示识别窗口，需要可用图形桌面")
    display.add_argument("--no-display", action="store_true", help="不显示图像窗口，可在 SSH 下运行")
    parser.add_argument("--end-pose", nargs=6, type=float,
                        metavar=("X", "Y", "Z", "RX", "RY", "RZ"),
                        help="仅 dry-run 使用：给定末端位姿以预览抓取坐标")
    return parser


def _signature(args):
    return (args.target, args.execute, args.recognize_only,
            args.confirm_calibration, args.action)


def prepare(args):
    """预加载可在导航前调用；不创建相机，不连接或移动机械臂。"""
    config = load_config(args.config)
    if args.recognize_only and args.action == "place":
        raise ValueError("--recognize-only 不能与 --action place 同时使用")

    if args.action != "place" and not args.recognize_only:
        require_execution_safety(args, config)
    validate_config(config, args.execute, args.recognize_only, args.action)

    show = not args.no_display and (args.show or config.get("display", {}).get("show_window", False))
    if args.action != "place" and show and sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        raise RuntimeError("没有可用图形桌面；请移除 --show 或添加 --no-display")
    if args.end_pose is not None:
        finite_vector(args.end_pose, 6, "--end-pose")
    if args.execute:
        check_sdk()

    # LM7 的 place 只松爪，不需要加载模型、相机或手眼标定。
    if args.action == "place":
        return {"config": config, "pipeline": None, "show": False, "signature": _signature(args)}

    pipeline = PerceptionPipeline(config)
    labels = set(pipeline.detector.class_names.values())
    if args.target not in labels:
        raise ValueError(f"模型没有 {args.target!r}；可用标签: {sorted(labels)}")
    # 空白图推理检查 torch/torchvision 与权重兼容性；无相机输入。
    pipeline.detector.detect(np.zeros((480, 640, 3), dtype=np.uint8))
    return {"config": config, "pipeline": pipeline, "show": show,
            "signature": _signature(args)}



def execute_prelocalized_grasp(
    config,
    target_label,
    xyz_camera,
    end_pose,
    arm,
    guard=None,
):
    """Use an already collected camera-space target point to execute one carry grasp.

    This function intentionally does NOT:
    - move the arm to the observation pose;
    - start/stop the camera;
    - load/run YOLO;
    - collect a second set of stable samples.

    The competition controller can therefore perform one continuous
    "observation pose -> detect -> collect samples -> grasp" sequence.
    """
    guard = guard or (lambda: None)
    xyz_camera = np.asarray(xyz_camera, dtype=float)
    if xyz_camera.shape != (3,) or not np.all(np.isfinite(xyz_camera)):
        raise ValueError("xyz_camera 必须是 3 个有限数值")
    finite_vector(end_pose, 6, "机械臂实时末端位姿")

    def action(call):
        guard()
        result = call()
        guard()
        return result

    xyz_base = camera_point_to_base(
        xyz_camera,
        end_pose,
        camera_to_end_matrix(config),
    )
    poses = build_grasp_poses(xyz_base, config)
    workspace = config.get("grasp_test", {}).get("workspace_m", {})

    print(
        "[单次识别抓取] "
        + json.dumps(
            {
                "target": target_label,
                "xyz_camera_m": xyz_camera.tolist(),
                "xyz_base_m": xyz_base.tolist(),
                "final_xyz_m": list(poses["final"][:3]),
                "transition_xyz_m": list(poses["transition"][:3]),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    for name, point in (
        ("目标中心", xyz_base),
        ("最终抓取点", poses["final"][:3]),
        ("过渡点", poses["transition"][:3]),
    ):
        ok, reason = validate_workspace(point, workspace)
        if not ok:
            raise RuntimeError(f"{name}未通过工作空间检查: {reason}")

    retreat = config.get("grasp_test", {}).get("retreat_after_grasp", False)
    if type(retreat) is not bool:
        raise ValueError("grasp_test.retreat_after_grasp 必须是布尔值")
    approach_waypoints(config)  # 在发送任何抓取运动指令前校验整条关节过渡路径。
    if retreat:
        print("执行：过渡点 → 松爪 → 接近 → 夹紧 → 撤回预抓取位 → 初始/运输位姿。", flush=True)
    else:
        print("执行：过渡点 → 松爪 → 接近 → 夹紧 → 初始/运输位姿。", flush=True)
    try:
        move_to_pregrasp(arm, poses, config, action)
        action(lambda: arm.open_gripper(config))
        action(lambda: arm.movel(poses["final"]))
        action(lambda: arm.close_gripper(config))

        for _ in range(10):
            guard()
            time.sleep(0.1)

        if retreat:
            action(lambda: arm.movel(poses["transition"]))

    except BaseException:
        arm.stop_best_effort()
        raise

    return {
        "target": target_label,
        "action": "carry",
        "xyz_camera_m": xyz_camera.tolist(),
        "xyz_base_m": xyz_base.tolist(),
        "result": "grasp_held",
        "holding_verified": False,
        "retreated_to_transition": retreat,
    }


def run_prepared(args, prepared, guard=None, arm=None):
    """执行 carry/release/place；总主程序和独立命令行共用同一套动作。"""
    if _signature(args) != prepared["signature"]:
        raise ValueError("预检查参数与执行参数不一致，请重新预检查")

    config, pipeline = prepared["config"], prepared["pipeline"]
    arm_cfg, test_cfg = config.get("arm", {}), config.get("grasp_test", {})
    guard = guard or (lambda: None)
    owns_arm = arm is None
    arm = arm if arm is not None else RealManArm(config, execute=args.execute)
    camera = None

    def action(call):
        guard()
        result = call()
        guard()
        return result

    try:
        action(arm.connect)

        # LM7：物体已经由 LM5 抓住并带到这里，只需要松开夹爪。
        if args.action == "place":
            print("执行 place：到站后松开夹爪。", flush=True)
            action(lambda: arm.open_gripper(config))
            return {"action": "place", "result": "released", "holding_verified": False}

        if args.execute:
            print("机械臂将移动到识别观测位。", flush=True)
            action(lambda: arm.movej(arm_cfg["observation_joints_deg"]))
            deadline = time.monotonic() + float(arm_cfg.get("observation_settle_seconds", 1.5))
            while time.monotonic() < deadline:
                guard()
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            end_pose = action(arm.get_current_pose)
            finite_vector(end_pose, 6, "机械臂实时末端位姿")
        else:
            end_pose = args.end_pose

        guard()
        camera = RealSenseCamera(config)
        action(camera.start)
        pipeline.reset_stability()
        xyz_camera = collect_target_points(
            camera, pipeline, args.target, test_cfg.get("stable_samples", 10),
            float(test_cfg.get("timeout_seconds", 30)), prepared["show"], guard=guard)
        guard()
        print(f"多帧相机坐标中值: {xyz_camera.tolist()} m", flush=True)
        result = {"target": args.target, "action": args.action,
                  "xyz_camera_m": xyz_camera.tolist()}
        if args.recognize_only or end_pose is None:
            return {**result, "result": "recognized"}

        xyz_base = camera_point_to_base(xyz_camera, end_pose, camera_to_end_matrix(config))
        poses = build_grasp_poses(xyz_base, config)
        workspace = test_cfg.get("workspace_m", {})
        for name, point in (("目标中心", xyz_base), ("最终抓取点", poses["final"][:3]),
                            ("过渡点", poses["transition"][:3])):
            ok, reason = validate_workspace(point, workspace)
            if not ok:
                raise RuntimeError(f"{name}未通过工作空间检查: {reason}")
        print(json.dumps({"xyz_base_m": xyz_base.tolist(), "poses": poses}, ensure_ascii=False, indent=2))
        if not args.execute:
            return {**result, "result": "grasp_preview", "poses": poses}

        approach_waypoints(config)
        if args.action == "carry" and not owns_arm:
            print("执行：过渡点 → 松爪 → 接近 → 夹紧；随后由任务主流程直接回初始/运输位姿。", flush=True)
        else:
            print("执行：过渡点 → 松爪 → 接近 → 夹紧 → 撤回。", flush=True)
        move_to_pregrasp(arm, poses, config, action)
        action(lambda: arm.open_gripper(config))
        action(lambda: arm.movel(poses["final"]))
        action(lambda: arm.close_gripper(config))
        # SDK 夹爪接口已使用阻塞到位，仍保留原程序的夹紧后停顿。
        for _ in range(10):
            guard()
            time.sleep(0.1)
        # 任务主流程在 carry 返回后直接 movej 到运输位；独立执行和 release 保留原撤回。
        if args.action == "release" or owns_arm:
            action(lambda: arm.movel(poses["transition"]))

        if args.action == "release":
            # LM3：为了“放下”而不是从撤回高度直接掉落，再回到原抓取高度松爪后撤回。
            print("执行 release：回到放置高度 → 松爪 → 撤回。", flush=True)
            action(lambda: arm.movel(poses["final"]))
            action(lambda: arm.open_gripper(config))
            action(lambda: arm.movel(poses["transition"]))
            return {**result, "result": "grasp_released", "holding_verified": False,
                    "xyz_base_m": xyz_base.tolist()}

        # carry：LM5 抓住后不再发送松爪指令，由总主程序收拢机械臂并导航到 LM7。
        return {**result, "result": "grasp_held", "holding_verified": False,
                "xyz_base_m": xyz_base.tolist()}

    except BaseException:
        # 尽力停止，失败时不自动再次抓取、撤回或松爪。
        arm.stop_best_effort()
        raise
    finally:
        # 外部传入的机械臂由 GraspProgram.close() 统一管理，便于动作后继续回运输位。
        original_failure = sys.exc_info()[0] is not None
        cleanup_errors = []
        cleanups = [
            ("相机", camera.stop if camera is not None else lambda: None),
            ("窗口", cv2.destroyAllWindows if prepared["show"] else lambda: None),
        ]
        if owns_arm:
            cleanups.append(("机械臂", arm.disconnect))
        for name, cleanup in cleanups:
            try:
                cleanup()
            except Exception as exc:
                cleanup_errors.append(f"{name}: {exc}")
                print(f"清理{name}失败: {exc}", file=sys.stderr)
        if cleanup_errors and not original_failure:
            raise RuntimeError("动作结束但资源清理失败: " + "; ".join(cleanup_errors))


def main(argv=None, *, guard=None, prepared=None, arm=None):
    args = build_parser().parse_args(argv)
    prepared = prepared if prepared is not None else prepare(args)
    if args.check_only:
        return {"result": "preflight_passed", "target": args.target, "action": args.action}
    return run_prepared(args, prepared, guard=guard, arm=arm)

def cli(argv=None):
    try:
        result = main(argv)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("识别抓取已取消。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"识别抓取失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
