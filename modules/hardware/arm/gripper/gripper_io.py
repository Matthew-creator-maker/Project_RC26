"""
睿尔曼机械臂 - IO电磁阀夹爪封装
基于 RM_API2 Python SDK，对末端工具IO进行高层封装

IO夹爪为二值控制（全开/全闭），通过两路IO电平差分驱动：
  IO1 = close 信号线
  IO2 = open  信号线

使用示例:
    from io_gripper import IOGripper
    from Robotic_Arm.rm_robot_interface import *

    arm = RoboticArm(rm_thread_mode_e.RM_TRIPLE_MODE_E)
    handle = arm.rm_create_robot_arm("192.168.192.19", 8080)

    gripper = IOGripper(arm)
    gripper.open()
    gripper.close()
    print(gripper.state)
"""

from dataclasses import dataclass
from typing import Optional


# ─────────────────────────────────────────────
#  数据类：夹爪状态快照
# ─────────────────────────────────────────────

@dataclass
class IOGripperState:
    io1: int             # IO1(close信号) 电平: 0=低, 1=高
    io2: int             # IO2(open信号)  电平: 0=低, 1=高
    io1_mode: int        # IO1 模式: 0=输入, 1=输出
    io2_mode: int        # IO2 模式: 0=输入, 1=输出
    status: str          # 语义状态: 'open' / 'close' / 'idle'


    @property
    def is_output_mode(self) -> bool:
        """两路是否都处于输出模式（正常工作前提）"""
        return self.io1_mode == 1 and self.io2_mode == 1

    def __str__(self) -> str:
        return (
            f"IOGripperState("
            f"status={self.status}, "
            f"IO1={self.io1}, IO2={self.io2}, "
            f"mode=[{self.io1_mode},{self.io2_mode}])"
        )


# ─────────────────────────────────────────────
#  异常类
# ─────────────────────────────────────────────

class IOGripperError(Exception):
    """IO夹爪操作失败时抛出"""

    _CODE_MSG = {
        1:  "控制器拒绝指令（参数错误或机械臂状态异常）",
        -1: "数据发送失败（网络通信异常）",
        -2: "数据接收失败（通信异常或控制器超时）",
        -3: "返回值解析失败（数据格式异常）",
        -4: "当前型号/控制器不支持此功能",
    }

    def __init__(self, code: int, context: str = ""):
        self.code = code
        msg = self._CODE_MSG.get(code, f"未知错误码 {code}")
        super().__init__(f"[{context}] {msg}" if context else msg)


# ─────────────────────────────────────────────
#  核心封装类
# ─────────────────────────────────────────────

class IOGripper:
    """
    IO电磁阀夹爪高层封装。

    通过末端工具IO端口的电平信号直接控制，仅支持全开/全闭二值操作。
    IO1 = close（夹闭）信号线
    IO2 = open （张开）信号线
    两路不可同时置高。

    Parameters
    ----------
    arm         : RoboticArm 实例（已建立连接）
    delay       : 动作后等待时间(秒)，留给气缸/电磁阀完成动作，默认 0.5
    raise_on_error : True 时操作失败抛出 IOGripperError，False 时仅返回错误码
    """

    def __init__(
        self,
        arm,
        delay: float = 0.0,
        raise_on_error: bool = True,
    ):
        self._arm = arm
        self.delay = delay
        self.raise_on_error = raise_on_error
        self.is_open = None # 规定返回0 表示已闭合，1 表示已张开

        # 初始化：两路设为输出模式
        self._call("rm_set_tool_IO_mode", 1, 1)  # IO1 → 输出
        self._call("rm_set_tool_IO_mode", 2, 1)  # IO2 → 输出
        # 初始归零（保持当前状态）
        self._call("rm_set_tool_do_state", 1, 0)
        self._call("rm_set_tool_do_state", 2, 0)

    # ── 公开动作接口 ──────────────────────────────

    def open(self, delay: Optional[float] = None) -> int:
        """
        张开夹爪。

        Parameters
        ----------
        delay : 动作后等待时间(秒)，None则使用默认值
        """
        self._call("rm_set_tool_do_state", 1, 0)  # close 信号 LOW
        code = self._call("rm_set_tool_do_state", 2, 1)  # open  信号 HIGH
        self.is_open = 1
        # time.sleep(delay if delay is not None else self.delay) # 是否阻塞等待
        return code

    def close(self, delay: Optional[float] = None) -> int:
        """
        闭合夹爪。

        Parameters
        ----------
        delay : 动作后等待时间(秒)，None则使用默认值
        """
        self._call("rm_set_tool_do_state", 2, 0)  # open  信号 LOW
        code = self._call("rm_set_tool_do_state", 1, 1)  # close 信号 HIGH

        self.is_open = 0
        # time.sleep(delay if delay is not None else self.delay)
        return code

    def stop(self) -> int:
        """停止动作（两路归零，保持当前机械状态）。"""
        self._call("rm_set_tool_do_state", 1, 0)
        return self._call("rm_set_tool_do_state", 2, 0)

    # ── 状态查询 ──────────────────────────────────

    @property
    def state(self) -> IOGripperState:
        """查询并返回夹爪当前状态快照（IOGripperState）。"""
        result = self._arm.rm_get_tool_io_state()
        if isinstance(result, dict):
            code = result.get('return_code', 0)
            data = result
        else:
            code = result[0] if isinstance(result, tuple) else result
            data = result[1] if isinstance(result, tuple) else {}
        if code != 0:
            self._handle_error(code, "rm_get_tool_io_state")

        io_mode = data.get('IO_Mode', [0, 0])
        io_state = data.get('IO_state', [0, 0])
        io1, io2 = io_state[0], io_state[1]

        if io1 == 0 and io2 == 1:
            status = 'open'
        elif io1 == 1 and io2 == 0:
            status = 'close'
        else:
            status = 'idle'

        return IOGripperState(
            io1=io1,
            io2=io2,
            io1_mode=io_mode[0],
            io2_mode=io_mode[1],
            status=status,
        )



    # ── 上下文管理器（with 语句）──────────────────

    def __enter__(self):
        return self

    def __exit__(self, *_):
        """离开 with 块时自动停止并张开夹爪。"""
        try:
            self.open()
            self.stop()
        except IOGripperError:
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
            raise IOGripperError(code, context)
