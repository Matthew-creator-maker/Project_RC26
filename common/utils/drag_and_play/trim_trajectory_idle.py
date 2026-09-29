#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
裁剪轨迹文件开头和结尾的静止段，只保留各 keep_points 个静止点。

输入文件格式（每行一个 JSON）:
{"point":[j1,j2,j3,j4,j5,j6]}

示例:
python trim_trajectory_idle.py leave.txt leave_trimmed.txt
python trim_trajectory_idle.py leave.txt leave_trimmed.txt --keep 30 --threshold 5
"""

import json
import argparse
from typing import List, Dict


def load_trajectory(path: str) -> List[Dict]:
    data = []
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"第 {line_no} 行不是合法 JSON: {e}") from e

            if "point" not in obj:
                raise ValueError(f"第 {line_no} 行缺少 'point' 字段")

            point = obj["point"]
            if not isinstance(point, list) or len(point) != 6:
                raise ValueError(f"第 {line_no} 行的 point 不是 6 关节数组: {point}")

            data.append(obj)

    if not data:
        raise ValueError("输入文件为空或没有有效轨迹点")

    return data


def point_diff(a: List[float], b: List[float]) -> float:
    """
    计算两个 6 关节点的最大绝对差值。
    用 max(abs(ai-bi)) 判断是否基本静止，比较稳妥。
    """
    return max(abs(x - y) for x, y in zip(a, b))


def find_leading_idle_end(points: List[List[float]], threshold: float) -> int:
    """
    找到开头静止段的结束位置（包含）。
    返回静止段最后一个点的索引。
    若开头几乎没有静止，则返回 0。
    """
    end_idx = 0
    for i in range(1, len(points)):
        diff = point_diff(points[i - 1], points[i])
        if diff <= threshold:
            end_idx = i
        else:
            break
    return end_idx


def find_trailing_idle_start(points: List[List[float]], threshold: float) -> int:
    """
    找到结尾静止段的起始位置（包含）。
    返回静止段第一个点的索引。
    若结尾几乎没有静止，则返回最后一个点索引。
    """
    start_idx = len(points) - 1
    for i in range(len(points) - 1, 0, -1):
        diff = point_diff(points[i], points[i - 1])
        if diff <= threshold:
            start_idx = i - 1
        else:
            break
    return start_idx


def trim_idle_segments(
    traj: List[Dict],
    keep_points: int = 30,
    threshold: float = 5.0,
) -> List[Dict]:
    points = [item["point"] for item in traj]
    n = len(points)

    if n <= keep_points * 2:
        return traj[:]

    lead_end = find_leading_idle_end(points, threshold)
    trail_start = find_trailing_idle_start(points, threshold)

    # 如果整个轨迹几乎都是静止的，避免切坏
    if lead_end >= trail_start:
        if n <= keep_points * 2:
            return traj[:]
        return traj[:keep_points] + traj[-keep_points:]

    # 开头静止段: [0, lead_end]
    # 结尾静止段: [trail_start, n-1]
    lead_len = lead_end + 1
    trail_len = n - trail_start

    # 保留开头静止段最后 keep_points 个
    if lead_len > keep_points:
        new_start = lead_len - keep_points
    else:
        new_start = 0

    # 保留结尾静止段前 keep_points 个（即整个尾静止段的起点附近开始保留）
    if trail_len > keep_points:
        new_end = trail_start + keep_points
    else:
        new_end = n

    # 中间运动段完整保留
    trimmed = traj[new_start:new_end]

    return trimmed


def save_trajectory(path: str, traj: List[Dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for item in traj:
            f.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")


def main():
    parser = argparse.ArgumentParser(description="裁剪轨迹开头和结尾的静止段，只保留各 30 个点")
    parser.add_argument("input", help="输入轨迹文件")
    parser.add_argument("output", help="输出轨迹文件")
    parser.add_argument("--keep", type=int, default=30, help="开头/结尾各保留多少个静止点，默认 30")
    parser.add_argument(
        "--threshold",
        type=float,
        default=30.0,
        help="相邻点最大关节差小于等于该值视为静止，默认 30",
    )
    args = parser.parse_args()

    traj = load_trajectory(args.input)
    trimmed = trim_idle_segments(
        traj,
        keep_points=args.keep,
        threshold=args.threshold,
    )
    save_trajectory(args.output, trimmed)

    print(f"原始点数: {len(traj)}")
    print(f"裁剪后点数: {len(trimmed)}")
    print(f"输出文件: {args.output}")
    print(f"参数: keep={args.keep}, threshold={args.threshold}")


if __name__ == "__main__":
    main()