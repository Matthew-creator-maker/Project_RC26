"""王负责的基础感知流水线：YOLO + 区域深度中值 + 多帧稳定检测。"""

import math
from typing import Any, Dict, List, Optional

import cv2

from .camera_manager import load_config
from .depth_localizer import localize_detection
from .object_detector import ObjectDetector


class PerceptionPipeline:
    """把检测器和深度定位器连接起来。"""

    def __init__(self, config: Dict[str, Any]):
        self.detector = ObjectDetector(config)
        depth_cfg = config.get("depth", {})
        self.region_size = int(depth_cfg.get("region_size", 5))
        self.min_valid_samples = int(depth_cfg.get("min_valid_samples", 3))

        stability_cfg = config.get("stability", {})
        self.stability_enabled = bool(stability_cfg.get("enabled", True))
        self.required_frames = max(
            1,
            int(stability_cfg.get("required_frames", 3)),
        )
        self.center_tolerance_px = float(
            stability_cfg.get("center_tolerance_px", 25)
        )
        self.depth_tolerance_m = float(
            stability_cfg.get("depth_tolerance_m", 0.08)
        )
        self._history: Dict[str, Dict[str, Any]] = {}

    def reset_stability(self) -> None:
        """清空连续帧状态，例如机器人换到新的搜索点时调用。"""
        self._history.clear()

    def process_frame(
        self,
        color_image: Any,
        depth_frame: Any,
        intrinsics: Any,
    ) -> List[Dict[str, Any]]:
        if color_image is None or getattr(color_image, "size", 0) == 0:
            self.reset_stability()
            return []

        detections = self.detector.detect(color_image)
        image_shape = color_image.shape[:2]
        results = [
            localize_detection(
                detection,
                depth_frame,
                intrinsics,
                image_shape=image_shape,
                region_size=self.region_size,
                min_valid_samples=self.min_valid_samples,
            )
            for detection in detections
        ]
        return self._update_stability(results)

    def _update_stability(
        self,
        results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """按类别、中心位置和深度做简单连续帧判断。"""
        if not self.stability_enabled:
            for result in results:
                result["stable"] = True
                result["stable_count"] = 1
                result["stable_required_frames"] = 1
            return results

        # 第一阶段每个类别只跟踪置信度最高的一个目标。
        best_by_label: Dict[str, Dict[str, Any]] = {}
        for result in results:
            label = str(result.get("label", ""))
            previous = best_by_label.get(label)
            if previous is None or result["confidence"] > previous["confidence"]:
                best_by_label[label] = result

        current_labels = set(best_by_label)
        for old_label in list(self._history):
            if old_label not in current_labels:
                del self._history[old_label]

        selected_ids = {id(result) for result in best_by_label.values()}
        for label, result in best_by_label.items():
            center_u, center_v = result["pixel_center"]
            previous = self._history.get(label)
            stable_count = 1

            if previous is not None:
                old_u, old_v = previous["pixel_center"]
                center_distance = math.hypot(
                    center_u - old_u,
                    center_v - old_v,
                )
                depth_changed_too_much = False
                old_depth = previous.get("depth_m")
                new_depth = result.get("depth_m")
                both_localized = (
                    bool(previous.get("localization_ok"))
                    and bool(result.get("localization_ok"))
                )
                if both_localized:
                    depth_changed_too_much = (
                        abs(float(new_depth) - float(old_depth))
                        > self.depth_tolerance_m
                    )

                if (
                    both_localized
                    and
                    center_distance <= self.center_tolerance_px
                    and not depth_changed_too_much
                ):
                    stable_count = int(previous["stable_count"]) + 1

            result["stable_count"] = stable_count
            result["stable_required_frames"] = self.required_frames
            result["stable"] = stable_count >= self.required_frames
            self._history[label] = {
                "pixel_center": [center_u, center_v],
                "depth_m": result.get("depth_m"),
                "localization_ok": result.get("localization_ok", False),
                "stable_count": stable_count,
            }

        # 同类别的其他候选仍保留在输出中，但不参与本轮稳定计数。
        for result in results:
            if id(result) not in selected_ids:
                result["stable"] = False
                result["stable_count"] = 0
                result["stable_required_frames"] = self.required_frames
        return results

    def draw_results(self, color_image: Any, results: List[Dict[str, Any]]) -> Any:
        output = self.detector.draw_detections(color_image, results)
        for result in results:
            u, v = result["pixel_center"]
            if result.get("localization_ok"):
                text = (
                    f"d={result['depth_m']:.2f}m "
                    f"stable={result.get('stable_count', 0)}/"
                    f"{result.get('stable_required_frames', 1)}"
                )
                color = (0, 255, 0) if result.get("stable") else (255, 255, 0)
            else:
                text = (
                    f"depth invalid "
                    f"stable={result.get('stable_count', 0)}/"
                    f"{result.get('stable_required_frames', 1)}"
                )
                color = (0, 0, 255)
            # [硬件调试] 这里显示的是相机坐标相关信息，不是机械臂坐标。
            cv2.putText(
                output,
                text,
                (int(u) + 8, int(v) + 20),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                color,
                1,
                cv2.LINE_AA,
            )
        return output


def create_pipeline(config_path: Optional[str] = None) -> PerceptionPipeline:
    return PerceptionPipeline(load_config(config_path))
