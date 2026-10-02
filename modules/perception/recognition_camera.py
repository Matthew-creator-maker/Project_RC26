"""第一阶段专用的相机切换器：同一时刻只打开一台胸部/头部相机。

复用原有 RGB-D 接口，保证与抓取接口一致，不引入第二套深度单位。
切换遵循“关闭旧设备 → 打开新设备 → 丢弃预热帧”，避免 USB 占用与旧帧串台。
本模块导入时不会导入 RealSense SDK，更不会启动设备。
"""
from __future__ import annotations

import time
from typing import Callable

from .recognition_config import CameraSpec, RecognitionSettings


def default_camera_factory(config):
    from .camera_manager import RealSenseCamera
    return RealSenseCamera(config)


class RecognitionCameraSwitcher:
    def __init__(self, settings: RecognitionSettings, camera_factory: Callable = default_camera_factory, clock=time.monotonic):
        self.settings = settings
        self.camera_factory = camera_factory
        self.clock = clock
        self.active_camera = None
        self.active_spec: CameraSpec | None = None
        self._last_timestamp = None

    def select(self, spec: CameraSpec, guard=lambda: None) -> None:
        """相邻点使用同一相机时复用设备；更换角色时才重新启动。"""
        if not spec.serial:
            raise ValueError(f"{spec.role} 相机序列号未填写")
        guard()
        if self.active_spec == spec and self.active_camera is not None:
            return
        self.close()
        guard()
        camera = self.camera_factory({"camera": {
            "serial": spec.serial, "width": self.settings.width,
            "height": self.settings.height, "fps": self.settings.fps,
        }})
        # 启动中途出错也保留对象，交给 close() 释放可能已申请的资源。
        self.active_camera = camera
        self.active_spec = spec
        try:
            camera.start()
            guard()
            deadline = self.clock() + self.settings.startup_timeout_seconds
            for _ in range(self.settings.warmup_frames):
                while True:
                    guard()
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise RuntimeError(f"{spec.role} 相机预热超时，没有收到足够的新帧")
                    if self.read(min(self.settings.frame_timeout_ms, max(1, int(remaining * 1000)))) is not None:
                        break
        except BaseException:
            self.close()
            raise
        print(f"[相机切换] 当前为 {spec.role}，序列号 {spec.serial}", flush=True)

    def read(self, timeout_ms: int):
        if self.active_camera is None:
            raise RuntimeError("请先选择识别相机")
        frame = self.active_camera.get_aligned_frames(timeout_ms=timeout_ms)
        if frame is None:
            return None
        # 同一时间戳只计一次，不能把重复的缓存帧当作连续三帧识别。
        timestamp = frame.get("timestamp_ms")
        if timestamp is not None:
            if self._last_timestamp is not None and timestamp <= self._last_timestamp:
                return None
            self._last_timestamp = timestamp
        return frame

    def close(self) -> None:
        if self.active_camera is not None:
            # stop 失败时保留引用并上抛，禁止在旧设备尚未释放时打开下一台。
            self.active_camera.stop()
        self.active_camera = None
        self.active_spec = None
        self._last_timestamp = None
