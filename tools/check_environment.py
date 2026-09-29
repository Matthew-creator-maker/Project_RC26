"""Ubuntu 部署自检。默认不连接硬件；--hardware 只查询/采图，不发送运动指令。"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import platform
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import main as task_main
from modules.grasp.grasp_entry import load_grasp_module, transport_joints


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(task_main.CONFIG_DIR / "task_config.json"))
    parser.add_argument("--hardware", action="store_true", help="检查底盘、相机取流、机械臂只读状态")
    args = parser.parse_args(argv)
    failures = []

    def check(name, action):
        try:
            result = action()
            detail = f": {result}" if isinstance(result, (str, int, float, list)) else ""
            print(f"[通过] {name}" + detail, flush=True)
            return result
        except Exception as exc:
            failures.append(name)
            print(f"[失败] {name}: {type(exc).__name__}: {exc}", flush=True)
            return None

    print(f"系统: {platform.platform()} / {platform.machine()}")
    print(f"Python: {sys.version.split()[0]}，解释器: {sys.executable}")
    if sys.version_info < (3, 10):
        print("需要 Python 3.10 或以上。")
        return 1
    if sys.platform != "linux":
        print("[说明] 当前不是 Linux，本次不能代替 Ubuntu 上的本地库与驱动验证。")
    for module, package in (("numpy", "numpy"), ("cv2", "opencv-python"), ("yaml", "PyYAML"),
                            ("PIL", "Pillow"), ("torch", "torch"), ("torchvision", "torchvision"),
                            ("ultralytics", "ultralytics"), ("pyrealsense2", "pyrealsense2")):
        def inspect(module=module, package=package):
            importlib.import_module(module)
            return importlib.metadata.version(package)
        check(module, inspect)
    task_args = task_main.build_parser().parse_args(["--config", args.config])
    config = check("任务文件与路径", lambda: task_main.load_settings(args.config, task_args))
    if config is None:
        return 1
    check("LM1-LM7 任务配置", lambda: task_main.validate_mission_config(config))
    check("机械臂收拢位配置", lambda: transport_joints(config))
    grasp = check("加载集成抓取控制器", lambda: load_grasp_module())
    if grasp is not None:
        check("RealMan API2 及本机动态库", lambda: grasp.check_sdk().__file__)
        vision_args = grasp.build_parser().parse_args([
            "--config", str(config["perception_config"]), "--target", config["task_lm3"]["target"],
            "--action", "release", "--no-display", "--check-only"])
        prepared = check("模型标签、空白图推理与基础参数", lambda: grasp.prepare(vision_args))
        if prepared is not None:
            vision_args.execute = True
            vision_args.confirm_calibration = True  # 自检不是替用户确认标定，也不执行运动。
            check("工作空间已配置标记", lambda: grasp.require_execution_safety(vision_args, prepared["config"]))
            check("实机参数数值", lambda: grasp.validate_config(
                prepared["config"], execute=True, action="release"))
            if args.hardware:
                def camera_check():
                    camera = grasp.RealSenseCamera(prepared["config"])
                    try:
                        camera.start()
                        frame = camera.get_aligned_frames(timeout_ms=5000)
                        if frame is None:
                            raise RuntimeError("5 秒内没有对齐的彩色/深度帧")
                        return f"相机 {camera.serial}，彩色图 {frame['color_image'].shape}"
                    finally:
                        camera.stop()
                check("RealSense 实际取流", camera_check)

                def arm_check():
                    arm = grasp.RealManArm(prepared["config"], execute=True)
                    try:
                        arm.connect()
                        return arm.get_current_pose()
                    finally:
                        arm.disconnect()
                check("机械臂连接及只读位姿", arm_check)
    if args.hardware:
        def navigation_check():
            nav = task_main.Navigator(config["navigation"])
            try:
                nav.connect()
                current = nav.current_station()
                expected = config["task"].get("start_station")
                if expected and current != expected:
                    raise RuntimeError(f"当前站点 {current}，任务起点要求 {expected}")
                return current
            finally:
                nav.close()
        check("底盘查询、推送及起点", navigation_check)
    print(f"检查完成，失败项目 {len(failures)} 项。")
    print("本程序未发送底盘导航、机械臂运动或夹爪指令。")
    if not args.hardware:
        print("设备连接、相机 USB 权限与取流尚未检查；在机器人上加 --hardware 进行只读检查。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
