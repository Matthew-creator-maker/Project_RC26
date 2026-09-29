"""眼在手上的手眼坐标转换和 RealMan 位姿工具。

最终转换链：p_base = T_END_TO_BASE @ T_CAM_TO_END @ p_cam。
长度单位统一使用米，旋转默认使用弧度。
"""

import math
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import numpy as np


def camera_to_end_matrix(config: Dict[str, Any]) -> np.ndarray:
    """从配置读取最终的相机→机械臂末端标定矩阵。"""
    handeye = config.get("handeye", {})
    rotation = np.asarray(
        handeye.get("rotation_camera_to_end"),
        dtype=float,
    )
    translation = np.asarray(
        handeye.get("translation_camera_to_end_m"),
        dtype=float,
    )
    if rotation.shape != (3, 3):
        raise ValueError("handeye.rotation_camera_to_end 必须是 3×3 矩阵")
    if translation.shape != (3,):
        raise ValueError("handeye.translation_camera_to_end_m 必须包含 3 个数")
    if not np.all(np.isfinite(rotation)) or not np.all(np.isfinite(translation)):
        raise ValueError("手眼标定包含 NaN/Inf")

    determinant = float(np.linalg.det(rotation))
    orthogonality_error = float(
        np.max(np.abs(rotation.T @ rotation - np.eye(3)))
    )
    if abs(determinant - 1.0) > 1e-3 or orthogonality_error > 1e-3:
        raise ValueError(
            "手眼旋转矩阵不是有效旋转矩阵："
            f"det={determinant:.6f}, error={orthogonality_error:.6f}"
        )

    transform = np.eye(4, dtype=float)
    transform[:3, :3] = rotation
    transform[:3, 3] = translation
    return transform


def realman_pose_to_matrix(
    pose: Sequence[float],
    degrees: bool = False,
) -> np.ndarray:
    """RealMan [x,y,z,rx,ry,rz] → 4×4 矩阵，采用 ZYX 内旋。"""
    if len(pose) != 6:
        raise ValueError("RealMan 位姿必须包含 [x,y,z,rx,ry,rz] 六个数")
    x, y, z, rx, ry, rz = [float(value) for value in pose]
    if not all(math.isfinite(value) for value in (x, y, z, rx, ry, rz)):
        raise ValueError("RealMan 位姿包含 NaN/Inf")
    if degrees:
        rx, ry, rz = [math.radians(value) for value in (rx, ry, rz)]
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    # 与 test_0914.py 的 ZYX 内旋约定一致：R = Rz @ Ry @ Rx。
    rotation = np.array(
        [
            [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
            [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
            [-sy, cy * sx, cy * cx],
        ],
        dtype=float,
    )
    transform = np.eye(4, dtype=float)
    transform[:3, :3] = rotation
    transform[:3, 3] = [x, y, z]
    return transform


def matrix_to_realman_pose(
    transform: np.ndarray,
    degrees: bool = False,
) -> List[float]:
    """4×4 矩阵 → RealMan [x,y,z,rx,ry,rz]。"""
    transform = np.asarray(transform, dtype=float)
    if transform.shape != (4, 4):
        raise ValueError("位姿矩阵必须为 4×4")
    rotation = transform[:3, :3]
    # 普通情况的 ZYX 欧拉角反解；接近万向节锁时固定 rz=0。
    cos_ry = math.hypot(float(rotation[0, 0]), float(rotation[1, 0]))
    if cos_ry > 1e-8:
        rx = math.atan2(float(rotation[2, 1]), float(rotation[2, 2]))
        ry = math.atan2(-float(rotation[2, 0]), cos_ry)
        rz = math.atan2(float(rotation[1, 0]), float(rotation[0, 0]))
    else:
        rx = math.atan2(-float(rotation[1, 2]), float(rotation[1, 1]))
        ry = math.atan2(-float(rotation[2, 0]), cos_ry)
        rz = 0.0
    if degrees:
        rx, ry, rz = [math.degrees(value) for value in (rx, ry, rz)]
    x, y, z = transform[:3, 3]
    return [
        float(x),
        float(y),
        float(z),
        float(rx),
        float(ry),
        float(rz),
    ]


def camera_point_to_base(
    xyz_camera_m: Sequence[float],
    end_pose_in_base: Sequence[float],
    transform_camera_to_end: np.ndarray,
) -> np.ndarray:
    """相机点经过相机→末端→基座，得到米制基座坐标。"""
    point = np.asarray(xyz_camera_m, dtype=float)
    if point.shape != (3,) or not np.all(np.isfinite(point)):
        raise ValueError("xyz_camera_m 必须包含 3 个有限数值")
    point_homogeneous = np.append(point, 1.0)
    transform_end_to_base = realman_pose_to_matrix(end_pose_in_base)
    point_base = (
        transform_end_to_base
        @ transform_camera_to_end
        @ point_homogeneous
    )
    return point_base[:3]


def offset_pose_in_tool(
    current_pose: np.ndarray,
    offset_xyz_m: Sequence[float],
) -> np.ndarray:
    """在工具自身坐标系沿 x/y/z 平移，使用右乘。"""
    current_pose = np.asarray(current_pose, dtype=float)
    offset = np.asarray(offset_xyz_m, dtype=float)
    if current_pose.shape != (4, 4):
        raise ValueError("current_pose 必须为 4×4")
    if offset.shape != (3,):
        raise ValueError("offset_xyz_m 必须包含 3 个数")
    offset_matrix = np.eye(4, dtype=float)
    offset_matrix[:3, 3] = offset
    return current_pose @ offset_matrix


def validate_workspace(
    xyz_base_m: Sequence[float],
    workspace: Dict[str, Sequence[float]],
) -> Tuple[bool, str]:
    """按配置的 x/y/z 范围做基础工作空间检查。"""
    point = np.asarray(xyz_base_m, dtype=float)
    if point.shape != (3,) or not np.all(np.isfinite(point)):
        return False, "invalid_base_point"
    for index, axis in enumerate(("x", "y", "z")):
        limits = workspace.get(axis)
        if limits is None or len(limits) != 2:
            return False, f"invalid_workspace_{axis}"
        lower, upper = [float(value) for value in limits]
        if not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
            return False, f"invalid_workspace_order_{axis}"
        if not lower <= point[index] <= upper:
            return False, f"outside_workspace_{axis}"
    return True, "ok"


def median_point(points: Iterable[Sequence[float]]) -> np.ndarray:
    """对多帧三维点逐轴取中值，减少偶发跳点。"""
    array = np.asarray(list(points), dtype=float)
    if array.ndim != 2 or array.shape[1] != 3 or len(array) == 0:
        raise ValueError("至少需要一个三维点")
    if not np.all(np.isfinite(array)):
        raise ValueError("三维点中存在无效数值")
    return np.median(array, axis=0)
