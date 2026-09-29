"""
睿尔曼机械臂 - 夹爪封装
基于 RM_API2 Python SDK，对 gripperControl 进行高层封装

使用示例:
    from gripper import Gripper
    from Robotic_Arm.rm_robot_interface import *

    arm = RoboticArm(rm_thread_mode_e.RM_TRIPLE_MODE_E)
    handle = arm.rm_create_robot_arm("192.168.1.18", 8080)

    gripper = Gripper(arm)
    gripper.open()
    gripper.close()
    gripper.grab(force=200)
    print(gripper.state)
"""

from dataclasses import dataclass
from typing import Optional


# ─────────────────────────────────────────────
#  数据类：夹爪状态快照
# ─────────────────────────────────────────────

@dataclass
class GripperState:
    enabled: bool        # 夹爪是否已激活
    moving: bool         # 是否正在运动（status=1 表示运动中）
    error: int           # 错误码，0 为正常
    mode: int            # 当前模式编号
    position: int        # 当前开口量 0~1000
    force: int           # 当前力值（含背压偏置，约 -27 为空载基准）
    temperature: int     # 电机温度（℃）

    @property
    def is_grasping(self) -> bool:
        """粗判断：夹爪处于夹取模式且未完全到达目标位（有物体阻挡）"""
        return self.mode in (2, 3) and self.moving

    def __str__(self) -> str:
        status_str = "运动中" if self.moving else "已停止"
        return (
            f"GripperState("
            f"enabled={self.enabled}, {status_str}, "
            f"pos={self.position}/1000, "
            f"force={self.force}, "
            f"temp={self.temperature}°C, "
            f"err={self.error})"
        )


# ─────────────────────────────────────────────
#  异常类
# ─────────────────────────────────────────────

class GripperError(Exception):
    """夹爪操作失败时抛出"""

    _CODE_MSG = {
        1:  "控制器拒绝指令（参数错误或机械臂状态异常）",
        -1: "数据发送失败（网络通信异常）",
        -2: "数据接收失败（通信异常或控制器超时）",
        -3: "返回值解析失败（数据格式异常）",
        -4: "阻塞等待超时（夹爪未在规定时间内到位）",
        -5: "到位设备校验失败（末端设备不是夹爪）",
    }

    def __init__(self, code: int, context: str = ""):
        self.code = code
        msg = self._CODE_MSG.get(code, f"未知错误码 {code}")
        super().__init__(f"[{context}] {msg}" if context else msg)


# ─────────────────────────────────────────────
#  核心封装类
# ─────────────────────────────────────────────

class Gripper:
    """
    睿尔曼夹爪高层封装。

    Parameters
    ----------
    arm         : RoboticArm 实例（已建立连接）
    default_speed : 默认运动速度，1~1000，默认 500
    default_force : 默认力控阈值，50~1000，默认 200
    timeout     : 阻塞模式超时时间（秒），默认 10
    raise_on_error : True 时操作失败抛出 GripperError，False 时仅返回错误码
    """

    # 位置语义常量
    POS_OPEN  = 1000   # 全开
    POS_CLOSE = 1      # 全关
    POS_HALF  = 500    # 半开

    def __init__(
        self,
        arm,
        default_speed: int = 500,
        default_force: int = 200,
        timeout: int = 10,
        raise_on_error: bool = True,
    ):
        self._arm = arm
        self.default_speed = default_speed
        self.default_force = default_force
        self.timeout = timeout
        self.raise_on_error = raise_on_error
        self.is_open = None # 1 为已打开，0 为已关闭

        # 初始化时设置全量程行程
        # self._call("rm_set_gripper_route", 0, 1000)

    # ── 公开动作接口 ──────────────────────────────

    def open(
        self,
        speed: Optional[int] = None,
        block: bool = False,
    ) -> int:
        """
        松开夹爪（运动到最大开口）。

        Parameters
        ----------
        speed : 松开速度，默认使用 default_speed
        block : True 等待到位后返回，False 立即返回
        """
        self.is_open = 1
        return self._call(
            "rm_set_gripper_release",
            speed or self.default_speed,
            block,
            self.timeout if block else 0,
        )

    def close(
        self,
        speed: Optional[int] = None,
        block: bool = False,
    ) -> int:
        """
        关闭夹爪（运动到最小开口）。

        Parameters
        ----------
        speed : 关闭速度，默认使用 default_speed
        block : True 等待到位后返回，False 立即返回
        """
        self.is_open = 0
        return self.move(self.POS_CLOSE, speed=speed, block=block)

    def move(
        self,
        position: int,
        speed: Optional[int] = None,
        block: bool = True,
    ) -> int:
        """
        移动到指定开口量。

        Parameters
        ----------
        position : 目标开口量，范围 1~1000
        speed    : 运动速度（本接口不支持单独传速度，此参数预留备用）
        block    : True 等待到位后返回，False 立即返回
        """
        position = self._clamp(position, 1, 1000, "position")
        return self._call(
            "rm_set_gripper_position",
            position,
            block,
            self.timeout if block else 0,
        )

    def grab(
        self,
        speed: Optional[int] = None,
        force: Optional[int] = None,
        block: bool = True,
    ) -> int:
        """
        力控夹取：以设定速度闭合，力超过阈值时停止。
        适用于抓取已知位置的物体（单次夹取）。

        Parameters
        ----------
        speed : 夹取速度，默认使用 default_speed
        force : 力控阈值 50~1000，默认使用 default_force
        block : True 等待夹取完成后返回，False 立即返回
        """
        speed = self._clamp(speed or self.default_speed, 1, 1000, "speed")
        force = self._clamp(force or self.default_force, 50, 1000, "force")
        self.is_open = 0
        return self._call(
            "rm_set_gripper_pick",
            speed, force,
            block,
            self.timeout if block else 0,
        )

    def grab_hold(
        self,
        speed: Optional[int] = None,
        force: Optional[int] = None,
        block: bool = True,
    ) -> int:
        """
        持续力控夹取：夹紧后持续施力，防止物体滑脱。
        适用于需要长时间保持夹持力的场景。

        Parameters
        ----------
        speed : 夹取速度，默认使用 default_speed
        force : 持续力阈值 50~1000，默认使用 default_force
        block : True 等待夹取完成后返回，False 立即返回
        """
        self.is_open = 0
        speed = self._clamp(speed or self.default_speed, 1, 1000, "speed")
        force = self._clamp(force or self.default_force, 50, 1000, "force")
        return self._call(
            "rm_set_gripper_pick_on",
            speed, force,
            block,
            self.timeout if block else 0,
        )

    def set_route(self, min_pos: int = 0, max_pos: int = 1000) -> int:
        """
        重新设置夹爪行程范围，断电后保留。

        Parameters
        ----------
        min_pos : 最小开口量，范围 0~1000
        max_pos : 最大开口量，范围 0~1000，须 > min_pos
        """
        if max_pos <= min_pos:
            raise ValueError(f"max_pos({max_pos}) 必须大于 min_pos({min_pos})")
        return self._call("rm_set_gripper_route", min_pos, max_pos)

    # ── 状态查询 ──────────────────────────────────

    @property
    def state(self) -> GripperState:
        """查询并返回夹爪当前状态快照（GripperState）。"""
        code, data = self._arm.rm_get_gripper_state()
        if code != 0:
            self._handle_error(code, "rm_get_gripper_state")
        return GripperState(
            enabled     = bool(data.get("enable_state", 0)),
            moving      = bool(data.get("status", 0)),
            error       = data.get("error", 0),
            mode        = data.get("mode", 0),
            position    = data.get("actpos", 0),
            force       = data.get("current_force", 0),
            temperature = data.get("temperature", 0),
        )

    @property
    def position(self) -> int:
        """当前开口量 0~1000。"""
        return self.state.position

    @property
    def is_enabled(self) -> bool:
        """夹爪是否已激活。"""
        return self.state.enabled

    # ── 上下文管理器（with 语句）──────────────────

    def __enter__(self):
        return self

    def __exit__(self, *_):
        """离开 with 块时自动松开夹爪。"""
        try:
            self.open()
        except GripperError:
            pass

    # ── 内部工具方法 ──────────────────────────────

    def _call(self, method: str, *args) -> int:
        """调用 arm 上的方法，统一处理错误。"""
        result = getattr(self._arm, method)(*args)
        code = result[0] if isinstance(result, tuple) else result
        if code != 0:
            self._handle_error(code, method)
        return code

    def _handle_error(self, code: int, context: str):
        if self.raise_on_error:
            raise GripperError(code, context)

    @staticmethod
    def _clamp(value: int, lo: int, hi: int, name: str) -> int:
        if not (lo <= value <= hi):
            raise ValueError(f"参数 {name}={value} 超出范围 [{lo}, {hi}]")
        return value
