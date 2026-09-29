"""将检测框中心的单个像素转换为相机坐标。

这是第一阶段的简单版本，允许单像素深度噪声；后续再升级为区域中值。
"""

import math
from statistics import median
from typing import Any, Dict, Optional, Sequence

import pyrealsense2 as rs


def _failure(
    detection: Dict[str, Any],
    error: str,
    region_size: int,
    sample_count: int = 0,
) -> Dict[str, Any]:
    result = dict(detection)
    result.update(
        {
            "depth_m": None,
            "xyz_camera": None,
            "localization_ok": False,
            "localization_error": error,
            "depth_method": "region_median",
            "depth_region_size": region_size,
            "depth_sample_count": sample_count,
        }
    )
    return result


def localize_detection(
    detection: Dict[str, Any],
    depth_frame: Any,
    intrinsics: Any,
    image_shape: Optional[Sequence[int]] = None,
    region_size: int = 5,
    min_valid_samples: int = 3,
) -> Dict[str, Any]:
    """读取中心附近区域的有效深度中值，并计算相机坐标 xyz_camera。"""
    if region_size < 1:
        region_size = 1
    if region_size % 2 == 0:
        region_size += 1
    min_valid_samples = max(1, int(min_valid_samples))

    if depth_frame is None or intrinsics is None:
        return _failure(detection, "missing_depth_or_intrinsics", region_size)

    try:
        u, v = [int(value) for value in detection["pixel_center"]]
    except (KeyError, TypeError, ValueError):
        return _failure(detection, "invalid_pixel_center", region_size)

    if image_shape is not None:
        height, width = int(image_shape[0]), int(image_shape[1])
        if not (0 <= u < width and 0 <= v < height):
            return _failure(detection, "pixel_out_of_range", region_size)

    if image_shape is not None:
        image_height, image_width = int(image_shape[0]), int(image_shape[1])
    else:
        image_height, image_width = None, None

    radius = region_size // 2
    valid_depths = []
    for sample_v in range(v - radius, v + radius + 1):
        for sample_u in range(u - radius, u + radius + 1):
            if image_width is not None and not (0 <= sample_u < image_width):
                continue
            if image_height is not None and not (0 <= sample_v < image_height):
                continue
            try:
                # [硬件调试] 这里假定对齐后的深度像素与彩色像素坐标一致。
                value = float(depth_frame.get_distance(sample_u, sample_v))
            except Exception:
                continue
            if value > 0 and math.isfinite(value):
                valid_depths.append(value)

    if len(valid_depths) < min_valid_samples:
        return _failure(
            detection,
            "not_enough_valid_depth_samples",
            region_size,
            len(valid_depths),
        )

    depth_m = float(median(valid_depths))

    try:
        xyz_camera = rs.rs2_deproject_pixel_to_point(
            intrinsics,
            [u, v],
            depth_m,
        )
    except Exception as error:
        return _failure(
            detection,
            f"deprojection_failed:{error}",
            region_size,
            len(valid_depths),
        )

    result = dict(detection)
    result.update(
        {
            "depth_m": depth_m,
            "xyz_camera": [float(value) for value in xyz_camera],
            "localization_ok": True,
            "localization_error": None,
            "depth_method": "region_median",
            "depth_region_size": region_size,
            "depth_sample_count": len(valid_depths),
        }
    )
    return result
