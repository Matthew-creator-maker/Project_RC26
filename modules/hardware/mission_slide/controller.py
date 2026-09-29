"""Competition rail positions. The source driver is bundled beside this adapter."""
import time

TRAVEL_POSITION_INC = 0       # Source reposition.py home position.
POSITION_TOLERANCE_INC = 20000


class MissionSlide:
    def __init__(self, driver=None):
        # Lazy import: slide.py opens /dev/ttyACM0 during module import.
        if driver is None:
            from .slide import slide_control
            driver = slide_control
        self.driver = driver
        self.lowered = False
        self.current_position_inc = TRAVEL_POSITION_INC

    def move_to(self, position, timeout=60.0):  #原先是30s
        motor = self.driver
        if not motor.serial.is_open:
            motor.serial.open()
        status = motor.read_status_word()
        if status is None or (status & 0x6F) != 0x27:
            raise RuntimeError("导轨未使能或状态不可读，请检查设备后重试")

        motor.device_speed_set(450) #设置导轨速度，原速度就是200
        
        if motor.device_location_set(position) is False:
            raise RuntimeError("导轨目标位置指令发送失败")
        time.sleep(0.5)
        if motor.device_start("2F") is False:
            raise RuntimeError("导轨启动指令发送失败 (2F)")
        time.sleep(0.5)
        if motor.device_start("3F") is False:
            raise RuntimeError("导轨启动指令发送失败 (3F)")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            position_now = motor.read_actual_position()
            status = motor.read_status_word()
            if status is not None and (status & 0x08):
                raise RuntimeError("导轨驱动器报故障")
            if (status is not None and (status & 0x0400)
                    and position_now is not None
                    and abs(position_now - position) <= POSITION_TOLERANCE_INC):
                self.current_position_inc = int(position)
                return
            time.sleep(0.5)
        raise TimeoutError(f"导轨未在 {timeout:g} 秒内到达 {position} inc")

    def prepare(self, station, position_inc=TRAVEL_POSITION_INC):
        """移动到当前高度档的导轨位置；位置由 perception.yaml 决定。"""
        position_inc = int(position_inc)
        if abs(self.current_position_inc - position_inc) > POSITION_TOLERANCE_INC:
            self.move_to(position_inc)
        self.lowered = position_inc != TRAVEL_POSITION_INC
        print(f"[导轨] {station} 已到高度档位置 {position_inc} inc", flush=True)

    def travel(self):
        if self.lowered:
            self.move_to(TRAVEL_POSITION_INC)
            self.lowered = False
            print("[导轨] 已升至行驶位置", flush=True)
