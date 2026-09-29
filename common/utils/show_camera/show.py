"""Simple OpenCV viewer for one or more cameras."""

from __future__ import annotations

import threading
import time
from typing import Any

import cv2
import numpy as np

from common.config import CAMERA_HEAD
from modules.hardware.camera import camera_manager


class ColorViewer:
    """Display one or more camera streams in separate OpenCV windows.

    Usage:
        ColorViewer(head_camera, window_name="Head Camera")
        ColorViewer(head_camera, chest_camera, window_names=["Head", "Chest"])
    """

    def __init__(
        self,
        *cameras: Any,
        window_name: str = "Color",
        window_names: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        if not cameras:
            raise ValueError("ColorViewer 至少需要一个相机对象")

        if window_names is not None:
            if len(window_names) != len(cameras):
                raise ValueError("window_names 数量必须与相机数量一致")
            names = list(window_names)
        elif len(cameras) == 1:
            names = [window_name]
        else:
            names = [f"{window_name} {index + 1}" for index in range(len(cameras))]

        self._streams = list(zip(names, cameras))
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the shared display loop once."""

        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._show_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the display loop and close all windows."""

        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        for window_name, _camera in self._streams:
            try:
                cv2.destroyWindow(window_name)
            except Exception:
                pass

    def _show_loop(self) -> None:
        """Refresh every window from a single OpenCV event loop."""

        while not self._stop_event.is_set():
            showed_frame = False

            for window_name, camera in self._streams:
                try:
                    frames = camera.get_frames()
                except Exception as exc:
                    print(f"{window_name} get_frames() failed: {exc}")
                    continue

                if isinstance(frames, (tuple, list)):
                    color_frame = frames[0] if frames else None
                else:
                    color_frame = frames

                if color_frame is None:
                    continue

                try:
                    cv2.imshow(window_name, np.asarray(color_frame))
                    showed_frame = True
                except Exception as exc:
                    print(f"{window_name} imshow failed: {exc}")

            if showed_frame:
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q") or key == 27:
                    self._stop_event.set()
                    break
            else:
                time.sleep(0.01)


def main() -> None:
    """Run a simple single-camera demo for local debugging."""

    viewer = ColorViewer(camera_manager.get(CAMERA_HEAD), window_name="Head Camera")
    viewer.start()

    try:
        input("按回车退出...\n")
    except KeyboardInterrupt:
        pass
    finally:
        viewer.stop()


if __name__ == "__main__":
    main()
