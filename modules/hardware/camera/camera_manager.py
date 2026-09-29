"""
相机管理器：按需获取、懒启动、统一管理生命周期。

设计目标：
- `start()` 保持阻塞语义，适合显式预热或工具脚本使用
- `start_async()` 异步启动相机，避免状态机卡在启动流程
- `get_frames()` 永远非阻塞，只返回当前缓存的最新帧；若尚无帧则返回 `(None, None)`
"""
from __future__ import annotations

import pyrealsense2 as rs
import numpy as np
import threading
import time
from typing import Optional


def _hardware_reset(serial: str) -> None:
    """启动前 reset 相机，清除残留状态"""
    ctx = rs.context()
    for dev in ctx.devices:
        if dev.get_info(rs.camera_info.serial_number) == serial:
            dev.hardware_reset()
            time.sleep(3)  # 等待设备重新枚举
            return
    raise RuntimeError(f"未找到相机 {serial}")


def _is_retryable_start_error(exc: Exception) -> bool:
    """识别 RealSense 启动时常见的临时占用错误。"""
    text = str(exc).lower()
    return (
        "device or resource busy" in text
        or "resource busy" in text
        or "vidioc_s_fmt" in text
    )


class RealSenseCamera:
    """单个相机的封装"""

    def __init__(self, serial: str):
        self.serial = serial
        self.pipeline = None
        self.config = None
        self.align = None
        self.started = False
        self.starting = False
        self.intrinsics = None  # rs.intrinsics: fx, fy, ppx, ppy
        self._last_error: str | None = None
        self._start_params = {"width": 640, "height": 480, "fps": 30}
        self._latest_color: Optional[np.ndarray] = None
        self._latest_depth: Optional[np.ndarray] = None
        self._latest_timestamp: float = 0.0
        self._frame_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._starter_thread: threading.Thread | None = None

    def start(self, width=640, height=480, fps=30):
        self._start_params = {"width": width, "height": height, "fps": fps}

        launched = self._launch_start_thread()
        with self._state_lock:
            starter = self._starter_thread
            already_started = self.started

        if already_started:
            return

        if starter and (launched or starter.is_alive()):
            starter.join()

        with self._state_lock:
            if not self.started:
                msg = self._last_error or f"相机 {self.serial} 启动失败"
                raise RuntimeError(msg)

    def start_async(self, width=640, height=480, fps=30):
        self._start_params = {"width": width, "height": height, "fps": fps}
        self._launch_start_thread()

    def _launch_start_thread(self) -> bool:
        with self._state_lock:
            if self.started or self.starting:
                return False
            self.starting = True
            self._last_error = None
            self._stop_event.clear()
            self._starter_thread = threading.Thread(
                target=self._start_worker,
                name=f"RealSenseStart-{self.serial}",
                daemon=True,
            )
            self._starter_thread.start()
            return True

    def _start_worker(self):
        params = dict(self._start_params)
        last_error = None
        try:
            for attempt in range(1, 4):
                pipeline = None
                try:
                    _hardware_reset(self.serial)
                    if self._stop_event.is_set():
                        return

                    pipeline = rs.pipeline()
                    config = rs.config()
                    config.enable_device(self.serial)
                    config.enable_stream(
                        rs.stream.color,
                        params["width"],
                        params["height"],
                        rs.format.bgr8,
                        params["fps"],
                    )
                    config.enable_stream(
                        rs.stream.depth,
                        params["width"],
                        params["height"],
                        rs.format.z16,
                        params["fps"],
                    )
                    profile = pipeline.start(config)
                    align = rs.align(rs.stream.color)
                    color_stream = profile.get_stream(rs.stream.color).as_video_stream_profile()
                    intrinsics = color_stream.get_intrinsics()

                    if self._stop_event.is_set():
                        pipeline.stop()
                        return

                    with self._frame_lock:
                        self._latest_color = None
                        self._latest_depth = None
                        self._latest_timestamp = 0.0

                    with self._state_lock:
                        self.pipeline = pipeline
                        self.config = config
                        self.align = align
                        self.intrinsics = intrinsics
                        self.started = True
                        self._last_error = None
                        self._reader_thread = threading.Thread(
                            target=self._reader_loop,
                            name=f"RealSenseReader-{self.serial}",
                            daemon=True,
                        )
                        self._reader_thread.start()
                    return
                except Exception as e:
                    last_error = e
                    if pipeline is not None:
                        try:
                            pipeline.stop()
                        except Exception:
                            pass

                    if self._stop_event.is_set():
                        return

                    if attempt < 3 and _is_retryable_start_error(e):
                        time.sleep(1.0)
                        continue
                    break

            with self._state_lock:
                self.pipeline = None
                self.config = None
                self.align = None
                self.intrinsics = None
                self.started = False
                self._last_error = f"相机 {self.serial} 启动失败: {last_error}"
        finally:
            with self._state_lock:
                self.starting = False

    def _reader_loop(self):
        while not self._stop_event.is_set():
            with self._state_lock:
                pipeline = self.pipeline
                align = self.align

            if pipeline is None or align is None:
                break

            try:
                frames = pipeline.wait_for_frames(timeout_ms=500)
                aligned = align.process(frames)
                color_frame = aligned.get_color_frame()
                depth_frame = aligned.get_depth_frame()
                if not color_frame or not depth_frame:
                    continue

                color = np.asanyarray(color_frame.get_data()).copy()
                depth = np.asanyarray(depth_frame.get_data()).copy()

                with self._frame_lock:
                    self._latest_color = color
                    self._latest_depth = depth
                    self._latest_timestamp = time.time()
            except RuntimeError:
                if self._stop_event.is_set():
                    break
            except Exception as e:
                with self._state_lock:
                    self._last_error = f"相机 {self.serial} 读帧失败: {e}"
                time.sleep(0.05)

    def get_frames(self):
        with self._state_lock:
            started = self.started
            starting = self.starting

        if not started and not starting:
            self._launch_start_thread()
            return None, None

        with self._frame_lock:
            return self._latest_color, self._latest_depth

    @property
    def latest_timestamp(self) -> float:
        with self._frame_lock:
            return self._latest_timestamp

    def stop(self):
        self._stop_event.set()

        with self._state_lock:
            starter = self._starter_thread
            pipeline = self.pipeline
            reader = self._reader_thread

        if starter and starter.is_alive():
            starter.join(timeout=5)

        if pipeline is not None:
            try:
                pipeline.stop()
            except Exception:
                pass

        if reader and reader.is_alive():
            reader.join(timeout=5)

        # 给驱动一点时间释放 /dev/video* 相关节点，降低下一次启动碰到 busy 的概率。
        time.sleep(0.2)

        with self._state_lock:
            self.pipeline = None
            self.config = None
            self.align = None
            self.intrinsics = None
            self.started = False
            self.starting = False
            self._reader_thread = None
            self._starter_thread = None

        with self._frame_lock:
            self._latest_color = None
            self._latest_depth = None
            self._latest_timestamp = 0.0


class CameraManager:
    """相机管理器：按需获取、懒启动、统一管理生命周期"""

    def __init__(self):
        self._cameras: dict[str, RealSenseCamera] = {}

    @staticmethod
    def list_devices() -> list[str]:
        """列出所有已连接相机的序列号"""
        ctx = rs.context()
        return [d.get_info(rs.camera_info.serial_number) for d in ctx.devices]

    def get(self, serial: str) -> RealSenseCamera:
        """获取相机对象，不存在则创建（但不自动启动）"""
        if serial not in self._cameras:
            self._cameras[serial] = RealSenseCamera(serial)
        return self._cameras[serial]

    def start(self, serial: str, **kwargs) -> RealSenseCamera:
        """获取并启动相机，一步到位"""
        cam = self.get(serial)
        cam.start(**kwargs)
        return cam

    def start_async(self, serial: str, **kwargs) -> RealSenseCamera:
        """获取并异步启动相机，不阻塞调用线程"""
        cam = self.get(serial)
        cam.start_async(**kwargs)
        return cam

    def stop(self, serial: str):
        """停止指定相机"""
        if serial in self._cameras:
            self._cameras[serial].stop()

    def stop_all(self):
        """停止所有相机"""
        for cam in list(self._cameras.values()):
            cam.stop()


# 管理器本身是模块级单例
camera_manager = CameraManager()
