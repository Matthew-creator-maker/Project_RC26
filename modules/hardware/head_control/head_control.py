"""
头部相机运动控制模块

本模块使用 MKS SERVO42/57D_RS485 电机的厂家串口协议：
- 帧头/帧尾格式为 `FA ... checksum8`
- 不是 Modbus RTU
- 电机菜单需满足：
  - `Mode = SR_CLOSE` 或 `SR_vFOC`
  - `Mb_RTU = Disable`

坐标单位为多圈编码器坐标值，手册定义为 16384 counts / 圈。
例如：
- `0x4000` = 360°
- `0x1000` = 90°
这个并不准确

公共接口：
- `clear_fault()`
- `move_absolute(...)`
- `move_relative(...)`
- `set_current_position_as_zero(...)`

兼容接口：
- `rotate_horizontal/rotate_vertical`
- `rotate_horizontal_rel/rotate_vertical_rel`
- `move/move_rel`
- `home`
"""

from __future__ import annotations

import threading
import time

import serial


ADDR_HORIZONTAL = 0x01
ADDR_VERTICAL = 0x02

DEFAULT_PORT = "/dev/ttyS1"
DEFAULT_BAUDRATE = 38400
DEFAULT_RESPONSE_TIMEOUT = 0.3
DEFAULT_SPEED = 600
DEFAULT_ACCELERATION = 2


class _SharedSerialPort:
    """同一串口总线在进程内只打开一次，避免多实例争抢设备。"""

    _registry: dict[tuple[str, int], "_SharedSerialPort"] = {}
    _registry_lock = threading.Lock()

    def __init__(self, port: str, baudrate: int):
        self.port = port
        self.baudrate = baudrate
        self.serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=8,
            parity=serial.PARITY_NONE,
            stopbits=1,
            timeout=0.02,
            write_timeout=0.1,
        )
        self.lock = threading.Lock()
        self.refcount = 0

    @classmethod
    def acquire(cls, port: str, baudrate: int) -> tuple[tuple[str, int], "_SharedSerialPort"]:
        key = (port, baudrate)
        with cls._registry_lock:
            bus = cls._registry.get(key)
            if bus is None:
                bus = cls(port, baudrate)
                cls._registry[key] = bus
            bus.refcount += 1
            return key, bus

    @classmethod
    def release(cls, key: tuple[str, int]) -> None:
        with cls._registry_lock:
            bus = cls._registry.get(key)
            if bus is None:
                return

            bus.refcount -= 1
            if bus.refcount > 0:
                return

            try:
                if bus.serial.is_open:
                    bus.serial.close()
            finally:
                cls._registry.pop(key, None)


class HeadCameraController:
    def __init__(
        self,
        port: str = DEFAULT_PORT,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: float = DEFAULT_RESPONSE_TIMEOUT,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._bus_key: tuple[str, int] | None = None
        self._bus: _SharedSerialPort | None = None

    # ---- 公共接口 ----

    def clear_fault(self, horizontal: bool = True, vertical: bool = True, reenable: bool = True) -> bool:
        """
        清除指定轴的堵转/故障状态。

        参数:
            horizontal: 是否处理水平轴（地址 0x01）
            vertical: 是否处理垂直轴（地址 0x02）
            reenable: 清故障后是否立即重新发送使能指令

        返回:
            所有被选中的轴都清故障成功则返回 True，否则返回 False。

        说明:
            该接口对应手册中的 0x3D 指令。若电机启用了堵转保护，
            发生堵转后通常需要先清故障，再重新使能，才能继续运动。
        """
        ok = True
        for addr, enabled in self._selected_axes(horizontal, vertical):
            if not enabled:
                continue

            status = self._command_status(addr, 0x3D)
            ok = ok and (status == 0x01)

            if reenable:
                ok = ok and self._set_enabled(addr, True)

        return ok

    def move_absolute(
        self,
        horizontal=None,
        vertical=None,
        speed: int = DEFAULT_SPEED,
        acceleration: int = DEFAULT_ACCELERATION,
    ) -> None:
        """
        控制头部电机按绝对坐标运动。

        参数:
            horizontal: 水平轴目标坐标；None 表示该轴不动
            vertical: 垂直轴目标坐标；None 表示该轴不动
            speed: 目标速度，单位 RPM，范围 [0, 3000]
            acceleration: 加速度参数，范围 [0, 255]

        说明:
            坐标单位为电机多圈编码器坐标值，16384 counts = 360°。
            因此：
            - 0x4000 表示一整圈
            - 0x1000 表示 90°

            传入字符串时，沿用旧接口习惯，默认按十六进制解析：
            - "1000" -> 0x1000
            - "-0800" -> -0x0800

            该接口底层使用手册中的 0xF5 绝对坐标运动指令，
            发送前会先确保对应轴已使能。
        """
        speed = self._validate_speed(speed)
        acceleration = self._validate_acceleration(acceleration)

        if horizontal is not None:
            self._move_axis_absolute(ADDR_HORIZONTAL, self._parse_coordinate(horizontal), speed, acceleration)
        if vertical is not None:
            self._move_axis_absolute(ADDR_VERTICAL, self._parse_coordinate(vertical), speed, acceleration)

    def move_relative(
        self,
        horizontal=None,
        vertical=None,
        speed: int = DEFAULT_SPEED,
        acceleration: int = DEFAULT_ACCELERATION,
    ) -> None:
        """
        控制头部电机在当前位置基础上做相对位移。

        参数:
            horizontal: 水平轴相对位移；None 表示该轴不动
            vertical: 垂直轴相对位移；None 表示该轴不动
            speed: 目标速度，单位 RPM，范围 [0, 3000]
            acceleration: 加速度参数，范围 [0, 255]

        说明:
            位移单位与绝对坐标相同，均为多圈编码器坐标值，
            16384 counts = 360°。

            本接口不会依赖软件缓存位置，而是先读取电机当前真实坐标，
            再计算目标绝对坐标并下发运动指令。因此即使电机曾被手动转动，
            或上一条指令尚未更新到本地状态，也能得到更可靠的相对运动结果。
        """
        speed = self._validate_speed(speed)
        acceleration = self._validate_acceleration(acceleration)

        if horizontal is not None:
            current = self._read_coordinate(ADDR_HORIZONTAL)
            target = current + self._parse_coordinate(horizontal)
            self._move_axis_absolute(ADDR_HORIZONTAL, target, speed, acceleration)

        if vertical is not None:
            current = self._read_coordinate(ADDR_VERTICAL)
            target = current + self._parse_coordinate(vertical)
            self._move_axis_absolute(ADDR_VERTICAL, target, speed, acceleration)

    # ---- 兼容接口 ----

    def rotate_horizontal(self, position, speed: int = DEFAULT_SPEED, acceleration: int = DEFAULT_ACCELERATION) -> None:
        self.move_absolute(horizontal=position, speed=speed, acceleration=acceleration)

    def rotate_vertical(self, position, speed: int = DEFAULT_SPEED, acceleration: int = DEFAULT_ACCELERATION) -> None:
        self.move_absolute(vertical=position, speed=speed, acceleration=acceleration)

    def move(self, horizontal=None, vertical=None, speed: int = DEFAULT_SPEED, acceleration: int = DEFAULT_ACCELERATION) -> None:
        self.move_absolute(horizontal=horizontal, vertical=vertical, speed=speed, acceleration=acceleration)

    def rotate_horizontal_rel(self, offset, speed: int = DEFAULT_SPEED, acceleration: int = DEFAULT_ACCELERATION) -> None:
        self.move_relative(horizontal=offset, speed=speed, acceleration=acceleration)

    def rotate_vertical_rel(self, offset, speed: int = DEFAULT_SPEED, acceleration: int = DEFAULT_ACCELERATION) -> None:
        self.move_relative(vertical=offset, speed=speed, acceleration=acceleration)

    def move_rel(self, horizontal=None, vertical=None, speed: int = DEFAULT_SPEED, acceleration: int = DEFAULT_ACCELERATION) -> None:
        self.move_relative(horizontal=horizontal, vertical=vertical, speed=speed, acceleration=acceleration)

    def home(self, speed: int = DEFAULT_SPEED, acceleration: int = DEFAULT_ACCELERATION) -> None:
        """回到逻辑零点；前提是该零点已通过面板或 set_current_position_as_zero 建立。"""
        self.move_absolute(horizontal=0, vertical=0, speed=speed, acceleration=acceleration)

    def set_current_position_as_zero(self, horizontal: bool = True, vertical: bool = True) -> bool:
        """
        将当前云台位置设置为电机逻辑零点。

        参数:
            horizontal: 是否设置水平轴（地址 0x01）的当前位置为 0
            vertical: 是否设置垂直轴（地址 0x02）的当前位置为 0

        返回:
            所有被选中的轴都设置成功则返回 True，否则返回 False。

        说明:
            该接口对应 MKS 串口协议 0x92“直接归零指令”，只修改驱动板
            当前坐标零点，不主动驱动电机运动。设置完成后，home() 会回到
            新建立的逻辑零点。
        """
        ok = True
        for addr, enabled in self._selected_axes(horizontal, vertical):
            if not enabled:
                continue
            status = self._command_status(addr, 0x92)
            ok = ok and (status == 0x01)
        return ok

    def set_zero(self, horizontal: bool = True, vertical: bool = True) -> bool:
        """兼容短名称：将当前云台位置设置为电机逻辑零点。"""
        return self.set_current_position_as_zero(horizontal=horizontal, vertical=vertical)

    def get_position(self) -> tuple[int, int]:
        return self._read_coordinate(ADDR_HORIZONTAL), self._read_coordinate(ADDR_VERTICAL)

    def enable(self, horizontal: bool = True, vertical: bool = True) -> bool:
        ok = True
        for addr, enabled in self._selected_axes(horizontal, vertical):
            if enabled:
                ok = ok and self._set_enabled(addr, True)
        return ok

    def close(self) -> None:
        if self._bus_key is None:
            return

        _SharedSerialPort.release(self._bus_key)
        self._bus_key = None
        self._bus = None

    # ---- 内部实现 ----

    def _selected_axes(self, horizontal: bool, vertical: bool) -> tuple[tuple[int, bool], tuple[int, bool]]:
        return ((ADDR_HORIZONTAL, horizontal), (ADDR_VERTICAL, vertical))

    def _get_bus(self) -> _SharedSerialPort:
        if self._bus is None:
            self._bus_key, self._bus = _SharedSerialPort.acquire(self.port, self.baudrate)
        return self._bus

    def _move_axis_absolute(self, addr: int, coordinate: int, speed: int, acceleration: int) -> None:
        self._set_enabled(addr, True)

        payload = (
            speed.to_bytes(2, byteorder="big", signed=False)
            + acceleration.to_bytes(1, byteorder="big", signed=False)
            + coordinate.to_bytes(4, byteorder="big", signed=True)
        )
        self._write_command(addr, 0xF5, payload)

    def _set_enabled(self, addr: int, enabled: bool) -> bool:
        status = self._command_status(addr, 0xF3, bytes([0x01 if enabled else 0x00]))
        return status == 0x01

    def _read_coordinate(self, addr: int) -> int:
        frame = self._query(addr, 0x31, response_len=10)
        raw = int.from_bytes(frame[3:9], byteorder="big", signed=False)
        return self._twos_complement(raw, bits=48)

    def _query(self, addr: int, func: int, payload: bytes = b"", response_len: int = 5) -> bytes:
        frame = self._build_frame(addr, func, payload)
        expected_prefix = bytes((0xFB, addr, func))

        bus = self._get_bus()
        with bus.lock:
            self._reset_input_buffer_locked(bus.serial)
            bus.serial.write(frame)
            bus.serial.flush()
            return self._read_matching_frame_locked(bus.serial, expected_prefix, response_len)

    def _command_status(self, addr: int, func: int, payload: bytes = b"") -> int | None:
        try:
            frame = self._query(addr, func, payload=payload, response_len=5)
        except TimeoutError:
            return None
        return frame[3]

    def _write_command(self, addr: int, func: int, payload: bytes = b"") -> None:
        frame = self._build_frame(addr, func, payload)
        bus = self._get_bus()
        with bus.lock:
            self._reset_input_buffer_locked(bus.serial)
            bus.serial.write(frame)
            bus.serial.flush()

    def _read_matching_frame_locked(self, ser: serial.Serial, prefix: bytes, response_len: int) -> bytes:
        deadline = time.monotonic() + self.timeout
        buffer = bytearray()

        while time.monotonic() < deadline:
            waiting = ser.in_waiting
            if waiting:
                buffer.extend(ser.read(waiting))
            else:
                chunk = ser.read(1)
                if chunk:
                    buffer.extend(chunk)

            start = buffer.find(prefix)
            if start >= 0 and len(buffer) >= start + response_len:
                frame = bytes(buffer[start:start + response_len])
                if self._checksum8(frame[:-1]) == frame[-1]:
                    return frame
                del buffer[:start + 1]
                continue

            if len(buffer) > response_len * 2:
                del buffer[:-response_len]

            time.sleep(0.002)

        raise TimeoutError(f"Timeout waiting for response prefix={prefix.hex(' ')}")

    @staticmethod
    def _reset_input_buffer_locked(ser: serial.Serial) -> None:
        try:
            ser.reset_input_buffer()
        except serial.SerialException:
            while ser.in_waiting:
                ser.read(ser.in_waiting)

    @staticmethod
    def _build_frame(addr: int, func: int, payload: bytes = b"") -> bytes:
        body = bytes((0xFA, addr, func)) + payload
        return body + bytes((HeadCameraController._checksum8(body),))

    @staticmethod
    def _validate_speed(speed: int) -> int:
        speed = int(speed)
        if not 0 <= speed <= 3000:
            raise ValueError("speed must be in range [0, 3000] RPM")
        return speed

    @staticmethod
    def _validate_acceleration(acceleration: int) -> int:
        acceleration = int(acceleration)
        if not 0 <= acceleration <= 255:
            raise ValueError("acceleration must be in range [0, 255]")
        return acceleration

    @staticmethod
    def _parse_coordinate(value) -> int:
        """
        兼容原有写法：
        - 传 int 时按整数坐标使用
        - 传 str 时默认按十六进制解析，如 "1000" -> 0x1000
        """
        if isinstance(value, str):
            value = value.strip()
            negative = value.startswith("-")
            if negative:
                value = value[1:]
            result = int(value, 16)
            return -result if negative else result
        return int(value)

    @staticmethod
    def _twos_complement(value: int, bits: int) -> int:
        sign_bit = 1 << (bits - 1)
        return value - (1 << bits) if value & sign_bit else value

    @staticmethod
    def _checksum8(data: bytes) -> int:
        return sum(data) & 0xFF

    def __enter__(self) -> "HeadCameraController":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


pan_tilt = HeadCameraController()
