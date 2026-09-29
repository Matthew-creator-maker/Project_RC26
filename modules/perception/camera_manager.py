"""RealSense 彩色/深度采集。

本文件只负责相机，不负责 YOLO、机械臂或底盘。
导入本文件不会自动启动相机。
"""

from pathlib import Path
from typing import Any, Dict, Optional

import cv2
import numpy as np
import pyrealsense2 as rs
import yaml


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """读取 YAML 配置。"""
    if config_path is None:
        config_path = str(Path(__file__).resolve().parent / "config.yaml")
    path = Path(config_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"找不到配置文件: {path}")
    with path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}
    config["_config_dir"] = str(path.parent)
    return config


class RealSenseCamera:
    """简单的 D435i 相机管理类。"""

    def __init__(self, config: Dict[str, Any]):
        camera_cfg = config.get("camera", {})
        self.serial = str(camera_cfg.get("serial", "")).strip()
        self.width = int(camera_cfg.get("width", 640))
        self.height = int(camera_cfg.get("height", 480))
        self.fps = int(camera_cfg.get("fps", 30))
        self.pipeline = rs.pipeline()
        self.align = rs.align(rs.stream.color)
        self.started = False

    def start(self) -> None:
        """启动彩色和深度流。"""
        if self.started:
            return

        camera_config = rs.config()
        if self.serial:
            # [硬件调试] 需要确认序列号与当前实际相机一致。
            camera_config.enable_device(self.serial)

        # [硬件调试] 分辨率和帧率必须与后续内参、手眼标定保持一致。
        camera_config.enable_stream(
            rs.stream.color,
            self.width,
            self.height,
            rs.format.bgr8,
            self.fps,
        )
        camera_config.enable_stream(
            rs.stream.depth,
            self.width,
            self.height,
            rs.format.z16,
            self.fps,
        )
        self.pipeline.start(camera_config)
        self.started = True

    def get_aligned_frames(self, timeout_ms: int = 1000) -> Optional[Dict[str, Any]]:
        """获取一组对齐到彩色图坐标系的帧。"""
        if not self.started:
            raise RuntimeError("相机尚未启动，请先调用 start()")

        # 超时返回 None，让上层总检测超时生效；不无限等相机。
        received, frames = self.pipeline.try_wait_for_frames(timeout_ms)
        if not received:
            return None
        aligned_frames = self.align.process(frames)
        depth_frame = aligned_frames.get_depth_frame()
        color_frame = aligned_frames.get_color_frame()
        if depth_frame is None or color_frame is None:
            return None

        color_image = np.asanyarray(color_frame.get_data())
        if color_image is None or color_image.size == 0:
            return None

        # 对齐后，彩色内参用于解释彩色图上的像素位置。
        color_intrinsics = (
            color_frame.profile.as_video_stream_profile().intrinsics
        )
        depth_intrinsics = (
            depth_frame.profile.as_video_stream_profile().intrinsics
        )

        return {
            "color_image": color_image,
            "depth_frame": depth_frame,
            "intrinsics": color_intrinsics,
            "depth_intrinsics": depth_intrinsics,
            "timestamp_ms": frames.get_timestamp(),
        }

    def get_intrinsics_info(self) -> Dict[str, Any]:
        """返回当前彩色内参的普通 Python 字典。"""
        if not self.started:
            raise RuntimeError("相机尚未启动，请先调用 start()")

        frame_data = self.get_aligned_frames()
        if frame_data is None:
            raise RuntimeError("无法取得有效帧，不能读取内参")

        intr = frame_data["intrinsics"]
        return {
            "width": int(intr.width),
            "height": int(intr.height),
            "fx": float(intr.fx),
            "fy": float(intr.fy),
            "ppx": float(intr.ppx),
            "ppy": float(intr.ppy),
            "model": str(intr.model),
            "coeffs": [float(value) for value in intr.coeffs],
        }

    def stop(self) -> None:
        """安全停止相机，可重复调用。"""
        if self.started:
            self.pipeline.stop()
            self.started = False


def draw_camera_help(image: np.ndarray) -> np.ndarray:
    """给测试画面添加简单的退出提示。"""
    output = image.copy()
    cv2.putText(
        output,
        "Press q or Esc to quit",
        (10, 25),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (0, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return output
