"""单独验证比赛里的胸部/头部识别服务；不连接底盘、机械臂、滑轨、语音。

运行示例：python -m tools.recognition_viewer --camera chest --seconds 60
也可以 --station LM9，让工具按点位分组选择相机。按 s 截图，q/Esc 结束。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from modules.perception.recognition_config import (
    display_enabled, load_recognition_settings, read_yaml, validate_display,
)

ROOT = Path(__file__).resolve().parents[1]


def load_task(path):
    path = Path(path).resolve()
    task = json.loads(path.read_text(encoding="utf-8-sig"))
    for name in ("perception_config", "recognition_config"):
        if name in task:
            task[name] = (path.parent / task[name]).resolve()
    return task


def list_cameras():
    import pyrealsense2 as rs
    devices = list(rs.context().query_devices())
    if not devices:
        print("未找到 RealSense 相机")
    for device in devices:
        print(device.get_info(rs.camera_info.name), device.get_info(rs.camera_info.serial_number))
    print("请逐台遮挡镜头，确认哪台是胸部/头部相机，再填写 recognition.yaml。")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config/task_config.json"))
    parser.add_argument("--list-cameras", action="store_true")
    parser.add_argument("--check-config", action="store_true", help="检查整条比赛识别路线，不启动设备")
    select = parser.add_mutually_exclusive_group()
    select.add_argument("--camera", choices=["chest", "head"])
    select.add_argument("--station", help="按识别点位选相机，例如 LM9")
    display = parser.add_mutually_exclusive_group()
    display.add_argument("--show", dest="show", action="store_true")
    display.add_argument("--no-show", dest="show", action="store_false")
    parser.set_defaults(show=None)
    parser.add_argument("--seconds", type=float, default=60)
    args = parser.parse_args(argv)
    if args.list_cameras:
        list_cameras()
        return
    task = load_task(args.config)
    settings = load_recognition_settings(task)
    vision = read_yaml(task["perception_config"])
    weights = (Path(vision["_config_dir"]) / vision["model"]["weights"]).resolve()
    if not weights.is_file():
        raise FileNotFoundError(f"找不到识别权重：{weights}")
    if args.check_config:
        from app.main import competition_plan
        plan = competition_plan(task)
        settings.validate_route(plan.scan_route, left_serial=str(vision.get("camera", {}).get("serial", "")))
        for station in plan.scan_route:
            spec = settings.camera_for_station(station)
            print(f"{station}: {spec.role} / {spec.serial}")
        print(f"配置检查通过；模型 {weights}；未启动任何设备。")
        return
    role = args.camera or ("chest" if not args.station else None)
    station = args.station or "MANUAL"
    spec = settings.cameras[role] if role else settings.camera_for_station(station)
    if not spec.serial:
        raise ValueError(f"{spec.role} 序列号未填写")
    if spec.serial == str(vision.get("camera", {}).get("serial", "")):
        raise ValueError("所选胸部/头部相机与左臂序列号相同，请核对")
    show = display_enabled(args.show, settings)
    validate_display(show)
    from modules.perception.object_detector import ObjectDetector
    from modules.perception.recognition_service import RecognitionService
    service = RecognitionService(settings, ObjectDetector(vision), show=show)
    try:
        return service.scan_station(station, args.seconds, camera_role=role)
    finally:
        service.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("已停止相机识别测试。")
    except Exception as exc:
        import sys
        print(f"[识别测试失败] {exc}", file=sys.stderr)
        raise SystemExit(1)
