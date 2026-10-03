"""队友当前比赛流程与配套配置的多相机合并版本。

task_config.json 管路线；recognition.yaml 管胸部/头部分组；perception.yaml 管模型与左臂抓取。
识别播报完成后才抓取；保留当前 6 秒默认时间与 SDK 故障即终止的抓取策略。
当前 JSON 的导航映射为空；填写说明见 04_队友配置合并说明.md。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from modules.navigation.navigation_adapter import Navigator
from modules.grasp.grasp_entry import GraspProgram, load_grasp_module, transport_joints
from modules.perception.recognition_config import (
    display_enabled, load_recognition_settings, station_name,
)


BASE = Path(__file__).resolve().parent
PROJECT_ROOT = BASE.parent
CONFIG_DIR = PROJECT_ROOT / "config"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from common.utils.voice_speak import speak_blocking
except Exception:  # 语音模块在无硬件/测试环境中可能无法导入。
    speak_blocking = None


# ============================================================================
# 赛前建图打点后，主要修改这里即可；不需要修改文件夹结构。
#
# 约定：
# - START_STATION：比赛起点。
# - SCAN_ROUTE：第一阶段固定巡检路线，都是提前打好的 LM 点。
# - HOME_STATION：出口内侧点；第一轮识别完先回这里，但此时不真正出场。
# - SCORE_STATION：己方 1m*1m 得分区附近的放置点。
# - EXIT_STATION：门外真正自主离场点。当前示例预留 LM8，正式比赛前必须实测。
# - GRASP_PRIORITY：第一轮抓取顺序；失败点后续按这个顺序组成重试队列。
#
# 当前值只沿用现有 LM1~LM7 工程并预留 LM8；比赛地图建好后请按实测点位改名/排序。
# ============================================================================
START_STATION = "LM1"
SCAN_ROUTE = ["LM2", "LM3", "LM4", "LM5", "LM6"]
# 播报阶段单独使用的导航点：键是物品所属任务点，值是较远的播报观察点。
# 例如在 RoboShop 中新增远处 LM15 后，改为 {"LM5": "LM15"}。
# 未配置的任务点在播报和抓取阶段使用同一个 LM 点。
SCAN_NAV_STATIONS: dict[str, str] = {"LM6": "LM15"}  # 与当前 JSON 一致；新增映射请在 JSON 中填写。
HOME_STATION = "LM6"
SCORE_STATION = "LM7"
EXIT_STATION = "LM8"
GRASP_PRIORITY = ["LM6", "LM5", "LM4", "LM3", "LM2"]

MATCH_SECONDS = 600.0
SCAN_SECONDS_PER_STATION = 6.0 #修改识别时间
EXIT_RESERVE_SECONDS = 45.0
GRASP_REDETECT_TIMEOUT_SECONDS = 8.0
GRASP_SAMPLE_TIMEOUT_SECONDS = 20.0


# ---------------------------------------------------------------------------
# 旧 LM1 -> LM7 演示任务保留，便于回归测试；CLI 使用 --legacy-demo 时才执行。
# ---------------------------------------------------------------------------
MISSION_ROUTE = ["LM1", "LM2", "LM3", "LM4", "LM5", "LM6", "LM7"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="预建图 LM 点导航 + 先识别播报 + 再抓取运返")
    parser.add_argument("--config", default=str(CONFIG_DIR / "task_config.json"), help="任务 JSON 配置文件")
    parser.add_argument("--execute", action="store_true", help="真实执行底盘/机械臂动作")
    parser.add_argument(
        "--confirm-calibration",
        action="store_true",
        help="确认当前手眼标定和机械臂安装未改变",
    )
    display = parser.add_mutually_exclusive_group()
    display.add_argument("--show", dest="show", action="store_true", help="强制显示识别与抓取画面")
    display.add_argument("--no-show", dest="show", action="store_false", help="关闭画面，适合无桌面 SSH")
    parser.set_defaults(show=None)  # 未指定时采用 recognition.yaml 的 show_window。
    parser.add_argument(
        "--navigation-only",
        action="store_true",
        help="只测试第一阶段固定 LM 路线并停在 HOME，不执行视觉/机械臂，也不真正离场",
    )
    parser.add_argument(
        "--legacy-demo",
        action="store_true",
        help="运行原 LM1->LM7 演示任务；默认运行新的比赛策略",
    )
    parser.add_argument("--scan-seconds", type=float, default=SCAN_SECONDS_PER_STATION,
                        help="每个扫描点识别时长（秒）")
    parser.add_argument("--match-seconds", type=float, default=MATCH_SECONDS,
                        help="任务总时长（秒）；默认 480 秒，到时停止新增抓取并前往 LM8")
    parser.add_argument(
        "--exit-reserve-seconds",
        type=float,
        default=EXIT_RESERVE_SECONDS,
        help="兼容旧启动参数；当前策略按 --match-seconds 到时停止新增抓取，不再提前预留离场时间",
    )
    parser.add_argument(
        "--grasp-timeout-seconds",
        type=float,
        default=GRASP_REDETECT_TIMEOUT_SECONDS,
        help="抓取阶段寻找第一个稳定目标的超时（秒）；默认 8 秒",
    )
    parser.add_argument(
        "--grasp-sample-timeout-seconds",
        type=float,
        default=GRASP_SAMPLE_TIMEOUT_SECONDS,
        help="锁定目标后收集稳定三维点的超时（秒）；默认 20 秒，stable_samples 仍为 10",
    )
    return parser


# 保留旧入口名，避免已有脚本依赖 parse_args()。
def parse_args(argv=None):
    return build_parser().parse_args(argv)


def _resolve_path(base: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def load_settings(path: str | Path = CONFIG_DIR / "task_config.json", args=None) -> dict[str, Any]:
    """读取任务配置，并把项目内相对路径固定到 task_config.json 所在目录。"""
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    config["perception_config"] = _resolve_path(path.parent, config["perception_config"])
    if "recognition_config" in config:
        config["recognition_config"] = _resolve_path(path.parent, config["recognition_config"])
    return config


def load_config():
    return load_settings(CONFIG_DIR / "task_config.json")


@dataclass(frozen=True)
class CompetitionPlan:
    start_station: str
    scan_route: tuple[str, ...]
    home_station: str
    score_station: str
    exit_station: str
    grasp_priority: tuple[str, ...]
    scan_nav_stations: dict[str, str] = field(default_factory=dict)

    @property
    def grasp_retry_route(self) -> tuple[str, ...]:
        """后续抓取轮保留原扫描方向，但只包含显式配置的抓取点。

        扫描有 LM9（头部识别高柜）、抓取列表没有 LM9 时，永远不会抓 LM9。
        只出现在抓取列表中的点补到末尾，避免漏掉这些站点的重试。
        """
        return tuple(station for station in self.scan_route if station in self.grasp_priority) + tuple(
            station for station in self.grasp_priority if station not in self.scan_route
        )

    def validate(self) -> None:
        values = [self.start_station, self.home_station, self.score_station, self.exit_station]
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("比赛点位名称必须是非空字符串")
        if not self.scan_route:
            raise ValueError("SCAN_ROUTE 不能为空")
        if not self.grasp_priority:
            raise ValueError("GRASP_PRIORITY 不能为空")
        for route in (self.scan_route, self.grasp_priority):
            if any(not isinstance(item, str) or not item.strip() for item in route):
                raise ValueError("路线中的点位必须是非空字符串")
            if len(route) != len(set(route)):
                raise ValueError("同一条路线不能重复填写点位")
        if self.exit_station in self.scan_route or self.exit_station in self.grasp_priority:
            raise ValueError("门外 exit_station 不能放进识别/抓取路线；扩展 LM1~LM9 时请重新设置离场点")
        for task_station, scan_station in self.scan_nav_stations.items():
            if task_station not in self.scan_route:
                raise ValueError(f"播报导航映射的任务点 {task_station} 不在 SCAN_ROUTE 中")
            if not isinstance(scan_station, str) or not scan_station.strip():
                raise ValueError(f"{task_station} 的播报导航点必须是非空 LM 名称")
        if self.home_station != self.scan_route[-1]:
            print(
                f"[提示] SCAN_ROUTE 最后一点是 {self.scan_route[-1]}，识别结束后将额外导航到 HOME={self.home_station}",
                flush=True,
            )
        missing = [station for station in self.grasp_priority if station not in set(self.scan_route)]
        if missing:
            print(f"[提示] 抓取优先级包含未在扫描路线出现的点: {missing}", flush=True)
        if self.exit_station == self.home_station:
            raise ValueError("EXIT_STATION 必须是门外离场点，不能与 HOME_STATION 相同")
        if self.exit_station == self.score_station:
            raise ValueError("EXIT_STATION 不能与 SCORE_STATION 相同，请单独打一个门外离场点")


def competition_plan(config: dict[str, Any] | None = None) -> CompetitionPlan:
    """JSON 是新路线入口；旧任务/旧测试未提供 competition 时沿用原常量。"""
    cfg = (config or {}).get("competition", {})
    if not isinstance(cfg, dict):
        raise ValueError("task_config.json 的 competition 必须是字典")
    for key in ("scan_route", "grasp_priority"):
        if key in cfg and not isinstance(cfg[key], list):
            raise ValueError(f"competition.{key} 必须是列表")
    nav_map = cfg.get("scan_nav_stations", SCAN_NAV_STATIONS)
    if not isinstance(nav_map, dict):
        raise ValueError("competition.scan_nav_stations 必须是字典")
    plan = CompetitionPlan(
        start_station=station_name(cfg.get("start_station", START_STATION)),
        scan_route=tuple(station_name(item) for item in cfg.get("scan_route", SCAN_ROUTE)),
        home_station=station_name(cfg.get("home_station", HOME_STATION)),
        score_station=station_name(cfg.get("score_station", SCORE_STATION)),
        exit_station=station_name(cfg.get("exit_station", EXIT_STATION)),
        grasp_priority=tuple(station_name(item) for item in cfg.get("grasp_priority", GRASP_PRIORITY)),
        scan_nav_stations={station_name(key): station_name(value) for key, value in nav_map.items()},
    )
    plan.validate()
    return plan


def _remaining(started_at: float, match_seconds: float) -> float:
    return match_seconds - (time.monotonic() - started_at)


def _safe_close(program) -> None:
    if program is not None:
        program.close()


def _speak_label(label: str, attempts: int = 2) -> bool:
    """阻塞播报一个类别；失败时有限重试，只有真正成功才算“已播报”."""
    text = f"识别到{label}"
    print(f"[播报] {text}", flush=True)
    if speak_blocking is None:
        print("[播报警告] voice_speak 导入失败，本次不把该目标记为已播报。", flush=True)
        return False
    attempts = max(1, int(attempts))
    for index in range(attempts):
        if bool(speak_blocking(text)):
            return True
        if index + 1 < attempts:
            print(f"[播报] 第 {index + 1} 次失败，立即重试。", flush=True)
            time.sleep(0.15)
    print(f"[播报警告] {label} 播报未确认成功；后续再次看到时会继续尝试。", flush=True)
    return False


class CompetitionScanner:
    """先按点位分组用胸部/头部识别，再用原左臂相机重新定位并抓取。

    第一阶段保持运输姿态；第二阶段保留当前队友的高度档和抓取异常处理。
    三份配置的填写方法见 04_队友配置合并说明.md。
    """

    def __init__(self, config: dict[str, Any], args: argparse.Namespace):
        self.config = config
        self.args = args
        # 全图识别点必须在出发前完成分组检查；不等走到高柜才发现漏填相机。
        self.recognition_settings = load_recognition_settings(config)
        plan = competition_plan(config)
        self.recognition_settings.validate_route(plan.scan_route)
        self.module = load_grasp_module()
        self.vision_config = self.module.load_config(str(config["perception_config"]))
        self.recognition_settings.validate_route(
            plan.scan_route, left_serial=str(self.vision_config.get("camera", {}).get("serial", ""))
        )
        self.pipeline = self.module.PerceptionPipeline(self.vision_config)
        self.arm = self.module.RealManArm(self.vision_config, execute=True)
        self.camera = self.module.RealSenseCamera(self.vision_config)
        self.transport_pose = transport_joints(config)
        self.observation_pose = list(self.vision_config["arm"]["observation_joints_deg"])
        self.settle_seconds = float(
            self.vision_config.get("arm", {}).get("observation_settle_seconds", 1.5)
        )
        self.show = display_enabled(args.show, self.recognition_settings)
        # 两阶段共用 YOLO 模型；识别服务不控制机械臂、不产生抓取坐标。
        from modules.perception.recognition_service import RecognitionService
        self.recognition_service = RecognitionService(
            self.recognition_settings, self.pipeline.detector, show=self.show
        )
        self._arm_connected = False
        self._camera_started = False

        # 让模型在出发前加载；不启相机、不运动底盘。
        self.pipeline.detector.detect(self.module.np.zeros((480, 640, 3), dtype=self.module.np.uint8))

    def _guarded(self, guard, call):
        guard()
        result = call()
        guard()
        return result

    def _ensure_arm(self, guard) -> None:
        if not self._arm_connected:
            self._guarded(guard, self.arm.connect)
            self._arm_connected = True

    def _ensure_camera(self, guard) -> None:
        if not self._camera_started:
            self.finish_recognition()  # 先释放胸部/头部设备，再启用原左臂相机。
            self._guarded(guard, self.camera.start)
            self._camera_started = True

    def transport(self, guard) -> None:
        self._ensure_arm(guard)
        self._guarded(guard, lambda: self.arm.movej(self.transport_pose))

    def _height_profile_for_station(self, station: str) -> tuple[str, dict[str, Any]]:
        """按 LM 站点反查所属高度档；LM 分组统一维护在 height_profiles.*.stations。"""
        profiles = self.vision_config.get("height_profiles", {})
        if not isinstance(profiles, dict) or not profiles:
            # 兼容旧单元测试/旧配置：没有 height_profiles 时按原 0.8m 全局参数运行。
            arm = self.vision_config.get("arm", {})
            return "legacy", {
                "enabled": True,
                "rail_position_inc": 0,
                "observation_points": [arm.get("observation_joints_deg", getattr(self, "observation_pose", [0]*6))],
            }

        station_key = str(station).strip().upper()
        matches: list[tuple[str, dict[str, Any]]] = []
        for profile_name, profile in profiles.items():
            if not isinstance(profile, dict):
                continue
            stations = profile.get("stations", [])
            if stations is None:
                stations = []
            if not isinstance(stations, list):
                raise ValueError(f"height_profiles.{profile_name}.stations 必须是 LM 点列表")
            normalized = [str(item).strip().upper() for item in stations]
            if station_key in normalized:
                matches.append((str(profile_name), profile))

        if len(matches) > 1:
            names = ", ".join(name for name, _ in matches)
            raise RuntimeError(
                f"{station} 被重复分配到多个高度档: {names}；"
                "请确保同一个 LM 只出现在一个 height_profiles.*.stations 中"
            )
        if not matches:
            raise RuntimeError(
                f"{station} 尚未分配抓取高度；请在 config/perception.yaml 的某个 "
                f"height_profiles.<高度>.stations 中加入 {station}"
            )

        profile_name, profile = matches[0]
        if not bool(profile.get("enabled", False)):
            raise RuntimeError(
                f"{station} 使用的高度档 {profile_name} 尚未实机标定/启用；"
                "请先填写 rail_position_inc 和 observation_points，再设 enabled: true"
            )
        return profile_name, profile

    def rail_position_for_station(self, station: str) -> int:
        profile_name, profile = self._height_profile_for_station(station)
        value = profile.get("rail_position_inc")
        if type(value) is not int:
            raise ValueError(f"height_profiles.{profile_name}.rail_position_inc 必须是整数")
        return value

    def _vision_config_for_station(self, station: str) -> dict[str, Any]:
        """把当前高度档的抓取姿态覆盖到通用视觉配置。"""
        profile_name, profile = self._height_profile_for_station(station)
        arm_cfg = self.vision_config.get("arm", {})
        station_cfg = dict(self.vision_config)
        station_arm = dict(arm_cfg)
        for key, length in (
            ("grasp_orientation_rad", 3),
            ("final_tool_offset_m", 3),
            ("transition_tool_offset_m", 3),
        ):
            if key in profile:
                station_arm[key] = self.module.finite_vector(
                    profile[key], length, f"height_profiles.{profile_name}.{key}"
                )
        station_cfg["arm"] = station_arm
        retreat = profile.get("retreat_after_grasp", False)
        if type(retreat) is not bool:
            raise ValueError(f"height_profiles.{profile_name}.retreat_after_grasp 必须是布尔值")
        station_cfg["grasp_test"] = {**self.vision_config.get("grasp_test", {}),
                                     "retreat_after_grasp": retreat}
        return station_cfg

    def _observation_poses_for_station(self, station: str) -> list[list[float]]:
        """一个高度可配置任意数量识别点；按列表顺序依次尝试。"""
        profile_name, profile = self._height_profile_for_station(station)
        points = profile.get("observation_points")
        if not isinstance(points, list) or not points:
            raise ValueError(f"height_profiles.{profile_name}.observation_points 至少需要 1 个识别点")
        poses = []
        for index, point in enumerate(points, 1):
            if isinstance(point, dict):
                point = point.get("joints_deg")
            poses.append(self.module.finite_vector(
                point, 6, f"height_profiles.{profile_name}.observation_points[{index}]"
            ))
        return poses

    def _observation_pose_for_station(self, station: str) -> list[float]:
        return self._observation_poses_for_station(station)[0]

    def _wait_with_guard(self, seconds: float, guard) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            guard()
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def scan_station(self, station: str, seconds: float, guard) -> list[dict[str, Any]]:
        """第一阶段按 LM 选胸部/头部相机，不移动左臂到观察姿态。"""
        return self.recognition_service.scan_station(station, seconds, guard)

    def finish_recognition(self) -> None:
        """阶段边界释放识别设备与窗口，后面使用左臂相机重新定位。"""
        service = getattr(self, "recognition_service", None)
        if service is not None:
            service.close()


    def _grasp_station_from_observation(
        self,
        station: str,
        search_timeout_seconds: float,
        sample_timeout_seconds: float,
        guard,
        expected_labels=None,
        observation_pose=None,
        observation_index=1,
    ) -> dict[str, Any]:
        """一次连续视觉会话完成：到观测位 -> 找目标 -> 采样 -> 抓取。

        与旧流程不同，这里不会先 scan_station()、回运输位，再由 GraspProgram
        第二次回观测位重新识别。YOLO 模型、RealSense 和机械臂连接全部复用。
        """
        if search_timeout_seconds <= 0 or sample_timeout_seconds <= 0:
            raise ValueError("抓取搜索/采样超时必须大于 0")

        # 抓取阶段必须以“当前站点现场识别”为准。
        # 扫描阶段记录的 expected_labels 只作为可选过滤条件：
        # - 有值：优先/限定抓这些类别；
        # - None 或空列表：允许识别并抓取画面中的任意稳定、可定位目标。
        expected_labels = {
            str(label).strip() for label in (expected_labels or []) if str(label).strip()
        }
        allow_any_label = not expected_labels

        test_cfg = self.vision_config.get("grasp_test", {})
        sample_count = int(test_cfg.get("stable_samples", 10))
        if sample_count < 1:
            raise ValueError("grasp_test.stable_samples 必须是正整数")

        self._ensure_arm(guard)

        # 所有 LOW_STATIONS 共用同一套地面识别/抓取参数；其他站点继续使用原参数。
        station_vision_config = self._vision_config_for_station(station)
        if observation_pose is None:
            observation_pose = self._observation_pose_for_station(station)

        print(f"[抓取单次识别] {station}: 进入识别点 {observation_index}。", flush=True)
        self._guarded(guard, lambda: self.arm.movej(observation_pose))
        self._wait_with_guard(self.settle_seconds, guard)
        end_pose = self._guarded(guard, self.arm.get_current_pose)
        self.module.finite_vector(end_pose, 6, "机械臂实时末端位姿")

        self._ensure_camera(guard)
        self.pipeline.reset_stability()

        target_label = None
        target_confidence = 0.0
        points = []
        search_started = time.monotonic()
        sample_started = None
        processed_frames = 0
        inference_seconds = []

        while len(points) < sample_count:
            guard()
            now = time.monotonic()

            if target_label is None:
                remaining = search_timeout_seconds - (now - search_started)
                if remaining <= 0:
                    raise TimeoutError(
                        f"{station}: {search_timeout_seconds:.1f} 秒内未找到稳定目标"
                    )
            else:
                remaining = sample_timeout_seconds - (now - sample_started)
                if remaining <= 0:
                    raise TimeoutError(
                        f"{target_label}@{station}: 已锁定目标，但 "
                        f"{sample_timeout_seconds:.1f} 秒内只采集到 "
                        f"{len(points)}/{sample_count} 个稳定点"
                    )

            frame = self.camera.get_aligned_frames(
                timeout_ms=max(1, min(1000, int(max(0.001, remaining) * 1000)))
            )
            guard()
            if frame is None:
                continue

            infer_started = time.monotonic()
            results = self.pipeline.process_frame(
                frame["color_image"],
                frame["depth_frame"],
                frame["intrinsics"],
            )
            infer_elapsed = time.monotonic() - infer_started
            inference_seconds.append(infer_elapsed)
            processed_frames += 1
            guard()

            stable_candidates = [
                item
                for item in results
                if item.get("stable") is True
                and item.get("localization_ok") is True
                and str(item.get("label", "")).strip()
                and (
                    allow_any_label
                    or str(item.get("label", "")).strip() in expected_labels
                )
            ]

            # 第一次出现稳定目标时锁定类别。后面的 10 点采样继续使用同一个
            # pipeline / camera / 观测位，不进行第二轮识别和第二次机械臂转位。
            if target_label is None and stable_candidates:
                target = max(
                    stable_candidates,
                    key=lambda item: float(item.get("confidence", 0.0)),
                )
                target_label = str(target["label"]).strip()
                target_confidence = float(target.get("confidence", 0.0))
                sample_started = time.monotonic()
                print(
                    f"[抓取锁定] {station}: {target_label} "
                    f"(confidence={target_confidence:.3f})；"
                    f"开始收集 {sample_count} 个稳定点，采样窗口 "
                    f"{sample_timeout_seconds:.1f}s。",
                    flush=True,
                )

            if target_label is not None:
                target = self.module.find_stable_target(results, target_label)
                if target is not None:
                    new_point = self.module.np.asarray(target["xyz_camera"], dtype=float)
                    if points:
                        current_center = self.module.median_point(points)
                        jump_m = float(
                            self.module.np.linalg.norm(new_point - current_center)
                        )
                        # 8 cm 与 perception_pipeline 的深度稳定阈值同量级。
                        # 短暂漏检后若重新出现的位置明显变化，不能把两个位置混在一起。
                        if jump_m > 0.08:
                            print(
                                f"[抓取采样] {target_label}@{station}: "
                                f"目标位置跳变 {jump_m:.3f}m，重新开始采样。",
                                flush=True,
                            )
                            points.clear()
                    points.append(new_point.tolist())
                    avg_infer = sum(inference_seconds) / len(inference_seconds)
                    print(
                        f"[抓取采样] {target_label}@{station}: "
                        f"{len(points)}/{sample_count}, "
                        f"xyz={target['xyz_camera']} m, "
                        f"YOLO平均={avg_infer:.2f}s/帧",
                        flush=True,
                    )
                elif points:
                    # 不因偶发一帧漏检就把已经得到的全部点清零。
                    # pipeline 自己仍要求目标重新达到 stable=true 才会继续采样。
                    print(
                        f"[抓取采样] {target_label}@{station}: "
                        f"本帧未稳定，保留已有 {len(points)}/{sample_count} 个点。",
                        flush=True,
                    )

            if self.show:
                display = self.pipeline.draw_results(frame["color_image"], results)
                self.module.cv2.putText(
                    display,
                    (
                        f"station={station} target={target_label or '-'} "
                        f"samples={len(points)}/{sample_count}"
                    ),
                    (10, 25),
                    self.module.cv2.FONT_HERSHEY_SIMPLEX,
                    0.65,
                    (0, 255, 255),
                    2,
                    self.module.cv2.LINE_AA,
                )
                self.module.cv2.imshow("competition_grasp_once", display)
                if (self.module.cv2.waitKey(1) & 0xFF) in (ord("q"), 27):
                    raise KeyboardInterrupt("用户取消抓取识别")

        xyz_camera = self.module.median_point(points)
        avg_infer = (
            sum(inference_seconds) / len(inference_seconds)
            if inference_seconds
            else 0.0
        )
        print(
            f"[抓取采样完成] {target_label}@{station}: "
            f"{sample_count}/{sample_count}, "
            f"中值={xyz_camera.tolist()} m, "
            f"处理帧={processed_frames}, YOLO平均={avg_infer:.2f}s/帧",
            flush=True,
        )

        # 直接使用刚才同一次识别得到的坐标执行抓取。
        # 不回运输位、不重新加载 YOLO、不重启相机、不再次进入观测位。
        return self.module.execute_prelocalized_grasp(
            station_vision_config,
            target_label,
            xyz_camera,
            end_pose,
            self.arm,
            guard=guard,
        )

    def grasp_station_once(
        self, station: str, search_timeout_seconds: float, sample_timeout_seconds: float,
        guard, expected_labels=None,
    ) -> dict[str, Any]:
        """依次尝试该高度的所有识别点；任一点发现目标即直接抓取。"""
        poses = self._observation_poses_for_station(station)
        per_point_timeout = max(1.0, float(search_timeout_seconds) / len(poses))
        last_timeout = None
        for index, pose in enumerate(poses, 1):
            try:
                return self._grasp_station_from_observation(
                    station, per_point_timeout, sample_timeout_seconds, guard,
                    expected_labels=expected_labels, observation_pose=pose, observation_index=index,
                )
            except TimeoutError as exc:
                last_timeout = exc
                print(f"[抓取换识别点] {station}: 识别点 {index}/{len(poses)} 未找到目标。", flush=True)
        raise TimeoutError(
            f"{station}: 已尝试 {len(poses)} 个识别点，仍未找到稳定目标"
        ) from last_timeout

    def release_gripper(self, guard) -> dict[str, Any]:
        """LM7 直接复用当前机械臂连接松爪，不再创建第二个 GraspProgram。"""
        self._ensure_arm(guard)
        self._guarded(
            guard,
            lambda: self.arm.open_gripper(self.vision_config),
        )
        return {
            "action": "place",
            "result": "released",
            "holding_verified": False,
        }


    def close(self) -> None:
        errors = []
        try:
            self.finish_recognition()
        except Exception as exc:
            errors.append(f"识别相机/窗口: {exc}")
        if self._camera_started:
            try:
                self.camera.stop()
            except Exception as exc:
                errors.append(f"相机: {exc}")
            self._camera_started = False
        if self.show:
            try:
                self.module.cv2.destroyAllWindows()
            except Exception as exc:
                errors.append(f"窗口: {exc}")
        if self._arm_connected:
            try:
                self.arm.disconnect()
            except Exception as exc:
                errors.append(f"机械臂: {exc}")
            self._arm_connected = False
        if errors:
            raise RuntimeError("扫描器资源清理失败: " + "; ".join(errors))


def _navigate_or_skip(navigator: Any, destination: str, phase: str) -> bool:
    """比赛中某个点临时不可达时允许跳过，但必须确认机器人仍停在已知站点。"""
    try:
        navigator.go_to_station(destination)
        return True
    except (TimeoutError, RuntimeError, ConnectionError) as exc:
        print(f"[{phase}] 前往 {destination} 失败: {exc}", flush=True)
        try:
            station = navigator.current_station()
        except Exception:
            raise
        print(f"[{phase}] 取消后机器人仍位于已知站点 {station}，跳过 {destination}", flush=True)
        return False


def _set_program_timeout(program: Any, seconds: float) -> None:
    """不改 config/perception.yaml，只给本次比赛抓取程序缩短二次识别超时。"""
    if seconds <= 0:
        raise ValueError("--grasp-timeout-seconds 必须大于 0")
    prepared = getattr(program, "prepared", None)
    if isinstance(prepared, dict):
        cfg = prepared.get("config")
        if isinstance(cfg, dict):
            cfg.setdefault("grasp_test", {})["timeout_seconds"] = float(seconds)


def _prepare_rail_for_station(rail, scanner, station: str) -> None:
    """新代码传高度档导轨位置；旧测试替身仍兼容 prepare(station)。"""
    getter = getattr(scanner, "rail_position_for_station", None)
    if not callable(getter):
        rail.prepare(station)
        return
    position = getter(station)
    try:
        rail.prepare(station, position)
    except TypeError:
        rail.prepare(station)


def run_competition(
    config: dict[str, Any],
    args: argparse.Namespace,
    navigator_factory: Callable[..., Any] = Navigator,
    grasp_factory: Callable[..., Any] = GraspProgram,
    scanner_factory: Callable[..., Any] = CompetitionScanner,
    rail_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """运行比赛策略：LM2->LM6 播报 -> LM6->LM2 抓取 -> 失败点重试 -> LM7 得分 -> LM8 离场。"""
    plan = competition_plan(config)

    if args.scan_seconds <= 0 or args.match_seconds <= 0:
        raise ValueError("扫描时间和任务总时长必须大于 0")
    if args.grasp_timeout_seconds <= 0:
        raise ValueError("抓取搜索超时必须大于 0")
    if float(getattr(args, "grasp_sample_timeout_seconds", GRASP_SAMPLE_TIMEOUT_SECONDS)) <= 0:
        raise ValueError("抓取采样超时必须大于 0")

    preview = {
        "mode": "preview",
        "start": plan.start_station,
        "scan_route": list(plan.scan_route),
        "home": plan.home_station,
        "grasp_priority": list(plan.grasp_priority),
        "score": plan.score_station,
        "exit": plan.exit_station,
    }
    if not args.execute:
        if "recognition_config" in config:
            settings = load_recognition_settings(config)
            settings.validate_route(plan.scan_route, require_serial=False)
            preview["recognition_cameras"] = {
                station: {"role": settings.camera_for_station(station).role,
                          "serial": settings.camera_for_station(station).serial}
                for station in plan.scan_route
            }
        return preview

    if not args.navigation_only and not args.confirm_calibration:
        raise RuntimeError("比赛实机任务需要添加 --confirm-calibration")

    navigator = None
    scanner = None
    rail = None
    active_program = None
    delivered_count = 0
    announced_labels: set[str] = set()
    object_station: dict[str, str] = {}
    results: list[dict[str, Any]] = []
    started_at = time.monotonic()

    try:
        navigator = navigator_factory(config["navigation"])
        navigator.connect()
        actual_start = navigator.current_station()
        if actual_start != plan.start_station:
            raise RuntimeError(
                f"实际起点是 {actual_start!r}，比赛配置要求从 {plan.start_station!r} 出发"
            )

        print("=== Competition Mission Start ===", flush=True)
        print(f"[策略] 固定点扫描: {list(plan.scan_route)}", flush=True)
        print(f"[策略] 抓取优先级: {list(plan.grasp_priority)}", flush=True)

        if args.navigation_only:
            for station in plan.scan_route:
                scan_nav_station = plan.scan_nav_stations.get(station, station)
                if not _navigate_or_skip(navigator, scan_nav_station, "导航测试"):
                    continue
            if navigator.current_station() != plan.home_station:
                _navigate_or_skip(navigator, plan.home_station, "导航测试")
            return {
                "mode": "navigation_only",
                "station": navigator.current_station(),
                "scan_route": list(plan.scan_route),
            }

        # ------------------------- 阶段 1：固定点识别 + 播报 -------------------------
        if rail_factory is None:
            from modules.hardware.mission_slide.controller import MissionSlide
            rail_factory = MissionSlide
        scanner = scanner_factory(config, args)
        rail = rail_factory()  # 相机分组/模型预检完成后再创建导轨驱动。

        # 播报阶段开始前，机械臂先进入初始/运输位姿。
        # 胸部/头部真实识别时保持运输位姿，不让左臂切换到 observation_pose。
        scanner.transport(lambda: navigator.assert_at(plan.start_station))

        for station in plan.scan_route:
            # 总任务时间到达上限时停止新增动作；后面统一前往 LM8。
            if _remaining(started_at, args.match_seconds) <= 0.0:
                print("[时间] 已达到任务总时长，停止继续扫描。", flush=True)
                break
            rail.travel()
            scan_nav_station = plan.scan_nav_stations.get(station, station)
            if not _navigate_or_skip(navigator, scan_nav_station, "扫描"):
                continue
            if scan_nav_station != station:
                print(f"[扫描] {station} 的播报观察点为 {scan_nav_station}", flush=True)
            # _prepare_rail_for_station(rail, scanner, station)  # 播报阶段识别高度切换暂时停用

            detections = scanner.scan_station(
                station,
                args.scan_seconds,
                guard=lambda s=scan_nav_station: navigator.assert_at(s),
            )
            for detected in detections:
                label = str(detected.get("label", "")).strip()
                if not label:
                    continue

                # [多相机接入] 真实检测结果按类别去重；不再每到一点固定播报 cola。
                if label not in object_station:
                    object_station[label] = station
                if label not in announced_labels:
                    if _speak_label(label):
                        announced_labels.add(label)

        # [多相机接入] 识别阶段完成，释放胸部/头部设备，抓取阶段再启用左臂。
        finish_recognition = getattr(scanner, "finish_recognition", None)
        if callable(finish_recognition):
            finish_recognition()

        # 正常情况下固定播报路线结束时已经位于 LM6；如果因为临时导航失败
        # 没到 LM6，且总时间还没到，则补到 LM6 后再开始 LM6 -> LM2 抓取。
        rail.travel()
        if (
            _remaining(started_at, args.match_seconds) > 0.0
            and navigator.current_station() != plan.home_station
        ):
            if not _navigate_or_skip(navigator, plan.home_station, "抓取起点"):
                raise RuntimeError("扫描结束后无法到达 LM6，不能开始反向抓取")

        print(
            f"[识别汇总] 共记录 {len(object_station)} 个类别: {object_station}",
            flush=True,
        )

        # ------------------------- 阶段 2：逐点现场识别 + 循环抓取 -------------------------
        #
        # 核心规则：
        #   1. 首轮按 LM6 -> LM5 -> LM4 -> LM3 -> LM2；
        #   2. 后续按 LM2 -> LM3 -> LM4 -> LM5 -> LM6 循环；
        #   3. 到达每一个抓取点后都调用 grasp_station_once()：
        #      机械臂先进入 observation_pose，再进行一次现场识别；
        #   4. 当前点识别到目标并成功抓取后，立即送 LM7 放置，然后继续下一个点；
        #   5. 当前点搜索超时表示“本次没有识别到可抓目标”，继续下一个点；
        #   6. 完整一轮所有点都没有识别到目标，才认为所有物品已经抓完；
        #   7. 若有导航失败、工作空间越界等未确认情况，不能误判为空，继续下一轮；
        #   8. 任意时刻达到 match_seconds（默认 480 秒/8 分钟）立即停止新增抓取。
        #
        # 注意：扫描阶段 object_station/labels_by_station 仅用于播报和首轮类别提示，
        # 不再决定某个 LM 点是否允许进入识别位姿。
        labels_by_station = {
            station: sorted(
                label for label, owner in object_station.items() if owner == station
            )
            for station in plan.grasp_priority
        }

        retry_round = 0
        stop_for_time = False
        all_objects_cleared = False
        last_unconfirmed_stations: list[str] = []

        # 某个抓取点一旦“成功抓取并在 LM7 完成释放”，就永久标记为已完成。
        # 后续循环不再导航到该点，也不再进入识别位姿/执行抓取。
        completed_stations: set[str] = set()

        while not stop_for_time and not all_objects_cleared:
            retry_round += 1

            if retry_round == 1:
                current_round = [
                    station for station in plan.grasp_priority
                    if station not in completed_stations
                ]
                print(
                    f"[抓取轮次] 第 1 轮逐点现场识别: {current_round}",
                    flush=True,
                )
            else:
                current_round = [
                    station for station in plan.grasp_retry_route
                    if station not in completed_stations
                ]
                print(
                    f"[抓取循环] 第 {retry_round} 轮仅检查未完成点: {current_round}；"
                    f"已完成点={sorted(completed_stations)}",
                    flush=True,
                )

            # 所有 LM2~LM6 都已经成功抓取过，则无需再做空轮识别。
            if not current_round:
                all_objects_cleared = True
                print(
                    "[任务完成] 所有抓取点都已成功抓取并送至 LM7，"
                    "后续不再重复识别这些点。",
                    flush=True,
                )
                break

            # 一整轮没有识别到任何目标，且所有站点都完成了有效识别，
            # 才能判定“物品全部抓完”。任何导航/工作空间异常都会阻止误判。
            round_detected_object = False
            round_unconfirmed: list[str] = []

            for station in current_round:
                # 双保险：已成功抓取过的点绝不再次识别/抓取。
                if station in completed_stations:
                    print(
                        f"[抓取跳过] {station}: 此点已成功抓取过，后续循环永久跳过。",
                        flush=True,
                    )
                    continue

                if _remaining(started_at, args.match_seconds) <= 0.0:
                    stop_for_time = True
                    print("[时间] 已达到 8 分钟任务上限，停止新增抓取。", flush=True)
                    break

                rail.travel()
                if navigator.current_station() != station:
                    if not _navigate_or_skip(navigator, station, "抓取循环"):
                        round_unconfirmed.append(station)
                        continue

                _prepare_rail_for_station(rail, scanner, station)
                # 每到一个抓取点都必须进入识别位姿并现场识别一次。
                # 不再因为扫描阶段没有记录到该点而跳过。
                recorded_labels = labels_by_station.get(station, [])
                print(
                    f"[抓取现场识别] {station}: 到站，进入识别位姿并搜索目标"
                    + (
                        f"；扫描阶段提示类别={recorded_labels}"
                        if recorded_labels
                        else "；扫描阶段无记录，本次仍执行现场识别"
                    ),
                    flush=True,
                )

                sample_timeout = float(
                    getattr(
                        args,
                        "grasp_sample_timeout_seconds",
                        GRASP_SAMPLE_TIMEOUT_SECONDS,
                    )
                )

                try:
                    # 这里故意传 expected_labels=None：
                    # 抓取阶段必须能发现扫描阶段漏检、位置变化或同类新增的物品。
                    result = scanner.grasp_station_once(
                        station,
                        float(args.grasp_timeout_seconds),
                        sample_timeout,
                        guard=lambda s=station: navigator.assert_at(s),
                        expected_labels=None,
                    )
                    label = str(result.get("target", "")).strip() or "unknown"
                    round_detected_object = True

                    # 柜子已撤回预抓取位；其他高度从抓取点直接回运输位。
                    if result.get("retreated_to_transition", False):
                        print(f"[抓取回位] {station}: 已撤回预抓取位，返回初始/运输位姿。", flush=True)
                    else:
                        print(f"[抓取回位] {station}: 夹紧完成，直接返回初始/运输位姿。", flush=True)
                    scanner.transport(lambda s=station: navigator.assert_at(s))

                except TimeoutError as exc:
                    # 搜索超时代表这个站点本轮完成了一次有效识别，但没看到目标。
                    print(
                        f"[抓取现场识别] {station}: {exc}；"
                        "本轮该点未发现可抓目标，继续下一个抓取点。",
                        flush=True,
                    )
                    try:
                        scanner.transport(lambda s=station: navigator.assert_at(s))
                    except Exception as retreat_exc:
                        raise RuntimeError(
                            f"{station} 识别结束后机械臂无法安全收回，禁止移动底盘"
                        ) from retreat_exc
                    continue

                except RuntimeError as exc:
                    message = str(exc)
                    is_workspace_outside = (
                        "未通过工作空间检查" in message
                        and any(
                            token in message
                            for token in (
                                "outside_workspace_x",
                                "outside_workspace_y",
                                "outside_workspace_z",
                            )
                        )
                    )
                    is_motion_safety_reject = "未通过运动安全检查" in message
                    if not (is_workspace_outside or is_motion_safety_reject):
                        # 已经发出运动指令后的 SDK 故障仍然必须终止，不能冒险自动恢复。
                        raise

                    # 能走到工作空间检查，说明已经识别并定位到了物品；
                    # 因此这一轮不能判定“全部抓完”。
                    round_detected_object = True
                    round_unconfirmed.append(station)
                    print(
                        f"[抓取未完成] {station}: {message}；"
                        "已识别到物品但当前不可抓，保留到下一轮继续识别。",
                        flush=True,
                    )
                    try:
                        scanner.transport(lambda s=station: navigator.assert_at(s))
                    except Exception as retreat_exc:
                        raise RuntimeError(
                            f"{station} 抓取安全检查失败后机械臂无法安全收回，禁止移动底盘"
                        ) from retreat_exc
                    continue

                rail.travel()
                # 每次成功抓取都必须先送到 LM7。
                if not _navigate_or_skip(navigator, plan.score_station, "运返"):
                    raise RuntimeError("已经抓住物品但无法到达 LM7 得分区，停止后续任务")

                place_result = scanner.release_gripper(
                    guard=lambda: navigator.assert_at(plan.score_station),
                )

                delivered_count += 1

                # 必须等 LM7 释放动作成功返回后，才认为这个抓取点真正完成。
                # 之后所有轮次都会从路线中排除该点。
                completed_stations.add(station)

                results.append(
                    {
                        "round": retry_round,
                        "target": label,
                        "from_station": station,
                        "grasp": result,
                        "place": place_result,
                    }
                )
                print(
                    f"[得分区] 已完成第 {delivered_count} 件: {label}@{station}；"
                    f"{station} 标记为已完成，后续循环不再识别/抓取该点。",
                    flush=True,
                )

            if stop_for_time:
                break

            last_unconfirmed_stations = list(round_unconfirmed)

            if not round_detected_object and not round_unconfirmed:
                all_objects_cleared = True
                print(
                    "[任务完成] LM2~LM6 已完整巡检一轮，所有抓取点均未再识别到可抓目标，"
                    "判定物品已全部抓取完。",
                    flush=True,
                )
            else:
                print(
                    f"[抓取循环] 第 {retry_round} 轮结束："
                    f"本轮{'识别到过物品' if round_detected_object else '未识别到物品'}"
                    + (
                        f"，未确认点={round_unconfirmed}"
                        if round_unconfirmed
                        else ""
                    )
                    + f"。继续下一轮，仅检查尚未成功抓取的点；"
                    f"已完成点={sorted(completed_stations)}。",
                    flush=True,
                )

        # ------------------------- 阶段 3：物品抓完或满 8 分钟 -> LM8 -------------------------
        # 扫描器可能处于打开或关闭状态；离场前统一释放。
        if scanner is not None:
            scanner.close()
            scanner = None

        pending_stations = list(last_unconfirmed_stations)
        finish_reason = "time_limit" if stop_for_time else "all_objects_cleared"
        if finish_reason == "time_limit":
            print(
                f"[任务结束] 8 分钟到时，停止继续识别抓取。"
                f"最后一轮未确认点位: {pending_stations}",
                flush=True,
            )

        rail.travel()
        if navigator.current_station() != plan.exit_station:
            if not _navigate_or_skip(navigator, plan.exit_station, "离场"):
                raise RuntimeError("无法到达 LM8 离场点")

        print(f"=== Mission Finished: exited via {plan.exit_station} ===", flush=True)
        return {
            "mode": "execute",
            "station": plan.exit_station,
            "announced": sorted(announced_labels),
            "object_station": object_station,
            "delivered_count": delivered_count,
            "completed_stations": sorted(completed_stations),
            "pending_stations": list(pending_stations),
            "finish_reason": finish_reason,
            "results": results,
        }

    finally:
        close_error = None
        if rail is not None and rail.lowered:
            print("[导轨] 异常终止且导轨仍处低位，请确认机械臂姿态后人工处理。", flush=True)
        try:
            _safe_close(active_program)
        except Exception as exc:
            close_error = close_error or exc
        if scanner is not None:
            try:
                scanner.close()
            except Exception as exc:
                close_error = close_error or exc
        if navigator is not None:
            try:
                navigator.close()
            except Exception as exc:
                close_error = close_error or exc
        if close_error is not None and sys.exc_info()[0] is None:
            raise close_error


# ---------------------------------------------------------------------------
# 原演示任务：完整保留，作为 --legacy-demo 和已有测试脚本的兼容入口。
# ---------------------------------------------------------------------------
def validate_mission_config(config: dict[str, Any]) -> None:
    route = config.get("route")
    if route != MISSION_ROUTE:
        raise ValueError(f"task_config.json 的 route 必须为 {MISSION_ROUTE}，当前为 {route!r}")

    task = config.get("task", {})
    if task.get("start_station") != "LM1" or task.get("end_station") != "LM7":
        raise ValueError("task.start_station/end_station 必须为 LM1 / LM7")

    for key, action in (("task_lm3", "release"), ("task_lm5", "carry"), ("task_lm7", "place")):
        point = config.get(key)
        if not isinstance(point, dict):
            raise ValueError(f"缺少 {key} 配置")
        if point.get("action") != action:
            raise ValueError(f"{key}.action 必须是 {action!r}")
        if action != "place" and not isinstance(point.get("target"), str):
            raise ValueError(f"{key}.target 必须是非空字符串")


def _point(config: dict[str, Any], key: str) -> dict[str, str]:
    value = config[key]
    point = {"action": value["action"]}
    if "target" in value:
        point["target"] = value["target"]
    return point


def run_task(
    config: dict[str, Any],
    args: argparse.Namespace,
    navigator_factory: Callable[..., Any] = Navigator,
    grasp_factory: Callable[..., Any] = GraspProgram,
) -> dict[str, Any]:
    """原 LM1-LM7 演示流程，保留用于兼容已有测试与 --legacy-demo。"""
    validate_mission_config(config)

    if not args.execute:
        return {
            "mode": "preview",
            "route": list(MISSION_ROUTE),
            "actions": {
                "LM3": "detect_grasp_release",
                "LM5": "detect_grasp_hold",
                "LM7": "release",
            },
        }

    if not args.navigation_only and not args.confirm_calibration:
        raise RuntimeError("完整实机任务需要添加 --confirm-calibration")

    navigator = None
    lm3_program = None
    lm5_program = None
    lm7_program = None
    results: dict[str, Any] = {}

    try:
        if not args.navigation_only:
            lm3_program = grasp_factory(config, _point(config, "task_lm3"), args)

        navigator = navigator_factory(config["navigation"])
        navigator.connect()
        actual_start = navigator.current_station()
        if actual_start != "LM1":
            raise RuntimeError(f"实际起点是 {actual_start!r}，比赛任务要求从 'LM1' 出发")

        print("=== LM1-LM7 Legacy Mission Start ===", flush=True)

        if args.navigation_only:
            for source, destination in zip(MISSION_ROUTE, MISSION_ROUTE[1:]):
                navigator.go_to(destination, source)
            return {"mode": "navigation_only", "route": list(MISSION_ROUTE), "station": "LM7"}

        lm3_program.transport(lambda: navigator.assert_at("LM1"))
        navigator.go_to("LM2", "LM1")
        navigator.go_to("LM3", "LM2")
        results["LM3"] = lm3_program.run(
            _point(config, "task_lm3"), guard=lambda: navigator.assert_at("LM3")
        )
        lm3_program.transport(lambda: navigator.assert_at("LM3"))
        _safe_close(lm3_program)
        lm3_program = None

        lm5_program = grasp_factory(config, _point(config, "task_lm5"), args)
        navigator.go_to("LM4", "LM3")
        navigator.go_to("LM5", "LM4")
        results["LM5"] = lm5_program.run(
            _point(config, "task_lm5"), guard=lambda: navigator.assert_at("LM5")
        )
        print("[抓取回位] LM5: 夹紧完成，直接返回初始/运输位姿。", flush=True)
        lm5_program.transport(lambda: navigator.assert_at("LM5"))
        _safe_close(lm5_program)
        lm5_program = None

        lm7_program = grasp_factory(config, _point(config, "task_lm7"), args)
        navigator.go_to("LM6", "LM5")
        navigator.go_to("LM7", "LM6")
        results["LM7"] = lm7_program.run(
            _point(config, "task_lm7"), guard=lambda: navigator.assert_at("LM7")
        )
        _safe_close(lm7_program)
        lm7_program = None

        navigator.assert_at("LM7")
        return {"mode": "execute", "route": list(MISSION_ROUTE), "station": "LM7", "results": results}

    finally:
        close_error = None
        for program in (lm7_program, lm5_program, lm3_program):
            try:
                _safe_close(program)
            except Exception as exc:
                close_error = close_error or exc
        if navigator is not None:
            try:
                navigator.close()
            except Exception as exc:
                close_error = close_error or exc
        if close_error is not None and sys.exc_info()[0] is None:
            raise close_error


def _print_competition_preview(result: dict[str, Any]) -> None:
    print("Preview mode. Competition plan:")
    print(f"START: {result['start']}")
    print("SCAN : " + " -> ".join(result["scan_route"]))
    for station, camera in result.get("recognition_cameras", {}).items():
        print(f"  {station}: {camera['role']} / {camera['serial'] or '序列号待填写'}")
    print(f"HOME : {result['home']} (出口内侧，不真正离场)")
    print("GRASP: " + " -> ".join(result["grasp_priority"]))
    print(f"SCORE: {result['score']}")
    print(f"EXIT : {result['exit']} (目标完成、无目标或比赛超时后离场)")


def main(argv=None):
    args = build_parser().parse_args(argv)
    config = load_settings(args.config, args)
    if args.legacy_demo:
        result = run_task(config, args)
        if not args.execute:
            print("Preview mode. Legacy mission:")
            print("LM1 -> LM2 -> LM3[抓取后放下] -> LM4 -> LM5[抓取并保持] -> LM6 -> LM7[松爪]")
        return result

    result = run_competition(config, args)
    if not args.execute:
        _print_competition_preview(result)
    return result


if __name__ == "__main__":
    main()
