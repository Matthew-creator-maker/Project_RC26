"""第一阶段的相机和点位配置；本文件不会连接任何硬件。

给初学者的说明：dict（字典）保存“名称 → 参数”，list（列表）保存一组 LM 点。
我们把配置检查放在这里，让主状态机只负责先扫描、再抓取，而不用知道相机细节。
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any


def read_yaml(path: str | Path) -> dict[str, Any]:
    """只读配置。延迟导入 YAML 库，查看 --help 时不需要相机/YOLO 环境。"""
    import yaml

    path = Path(path).resolve()
    with path.open(encoding="utf-8-sig") as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 的最外层必须是 YAML 字典")
    data["_config_dir"] = str(path.parent)
    return data


def station_name(value: Any) -> str:
    """统一大小写，防止配置中的 lm9 与程序中的 LM9 被当成两个点。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("LM 点位必须是非空字符串")
    return value.strip().upper()


def positive_number(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必须是大于 0 的有限数字")
    return float(value)


@dataclass(frozen=True)
class CameraSpec:
    role: str
    serial: str
    stations: tuple[str, ...]


class RecognitionSettings:
    """把配置转换成可靠的查询接口：给一个 LM 点，返回胸部或头部相机。"""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        groups = config.get("cameras")
        if not isinstance(groups, dict) or set(groups) != {"chest", "head"}:
            raise ValueError("recognition.yaml 的 cameras 必须包含 chest 和 head 两个分组")
        self.cameras: dict[str, CameraSpec] = {}
        self.station_to_role: dict[str, str] = {}
        for role, group in groups.items():
            if not isinstance(group, dict) or not isinstance(group.get("stations"), list):
                raise ValueError(f"cameras.{role}.stations 必须是列表，例如 [LM2, LM3]")
            serial = group.get("serial", "")
            if not isinstance(serial, str):
                raise ValueError(f"cameras.{role}.serial 必须用引号括起来")
            stations = tuple(station_name(item) for item in group["stations"])
            for station in stations:
                if station in self.station_to_role:
                    raise ValueError(f"{station} 重复出现在相机分组中；每个点只能分配一次")
                self.station_to_role[station] = role
            self.cameras[role] = CameraSpec(role, serial.strip(), stations)
        serials = [spec.serial for spec in self.cameras.values() if spec.serial]
        if len(serials) != len(set(serials)):
            raise ValueError("胸部、头部不能填写同一个相机序列号")
        self.width = self._integer(config.get("width", 640), "width", minimum=1)
        self.height = self._integer(config.get("height", 480), "height", minimum=1)
        self.fps = self._integer(config.get("fps", 30), "fps", minimum=1)
        self.required_frames = self._integer(config.get("required_frames", 3), "required_frames", minimum=1)
        self.warmup_frames = self._integer(config.get("warmup_frames", 5), "warmup_frames", minimum=0)
        self.startup_timeout_seconds = positive_number(config.get("startup_timeout_seconds", 10), "startup_timeout_seconds")
        self.frame_timeout_ms = self._integer(config.get("frame_timeout_ms", 500), "frame_timeout_ms", minimum=1)
        show = config.get("show_window", True)
        if type(show) is not bool:
            raise ValueError("show_window 必须是 true 或 false")
        self.show_window = show
        directory = Path(config.get("_config_dir", Path.cwd()))
        self.capture_dir = (directory / str(config.get("capture_dir", "../captures/recognition"))).resolve()

    @staticmethod
    def _integer(value: Any, name: str, minimum: int) -> int:
        # bool 是 int 的子类；使用 type(...) 明确拒绝把 true 当成帧数。
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} 必须是 >= {minimum} 的整数")
        return value

    def camera_for_station(self, station: str) -> CameraSpec:
        key = station_name(station)
        if key not in self.station_to_role:
            raise ValueError(f"{key} 未分配相机，请加入 recognition.yaml 的 chest.stations 或 head.stations")
        return self.cameras[self.station_to_role[key]]

    def validate_route(self, stations, *, require_serial: bool = True, left_serial: str = "") -> None:
        """赛前检查整条识别路线，不等到机器人走到 LM9 才发现没有头部相机。"""
        for station in stations:
            spec = self.camera_for_station(station)
            if require_serial and not spec.serial:
                raise ValueError(f"{spec.role} 相机序列号为空，请先运行 --list-cameras 并填写实际序列号")
            if spec.serial and spec.serial == left_serial:
                raise ValueError(f"{spec.role} 序列号与左臂相机相同，请核对物理安装位置")


def load_recognition_settings(task_config: dict[str, Any]) -> RecognitionSettings:
    """从任务配置找到识别配置；路径以 perception.yaml 所在目录为基准。"""
    path = task_config.get("recognition_config")
    if path is None:
        path = Path(task_config["perception_config"]).parent / "recognition.yaml"
    return RecognitionSettings(read_yaml(path))


def display_enabled(cli_show: bool | None, settings: RecognitionSettings) -> bool:
    # CLI 没有指定时采用配置；--show / --no-show 则显式覆盖配置。
    return settings.show_window if cli_show is None else bool(cli_show)


def validate_display(show: bool) -> None:
    """无桌面的 Linux 上提前报错，避免 OpenCV 开窗导致整个进程退出。"""
    import os
    import sys

    if show and sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("实时画面需要图形桌面；在机器人桌面运行，或使用 --no-show")
