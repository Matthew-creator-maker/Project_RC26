import serial
import time
import threading
from queue import Queue
import struct

class ModbusRTUMonitor:
    def __init__(self, port='COM5', baudrate=38400, timeout=1):
        """初始化Modbus RTU监控器"""
        self.serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=8,
            parity=serial.PARITY_NONE,
            stopbits=1,
            timeout=timeout
        )
        self.running = False
        self.receive_queue = Queue()
        self.thread = None
        self._lock = threading.Lock()  # 串口读写锁，防止多线程冲突

    # ========================================================================
    #  底层通讯方法
    # ========================================================================

    def send_command(self, hex_str):
        """发送十六进制指令（只发不收）"""
        if not self.serial.is_open:
            print("串口未打开")
            return False
        try:
            command_bytes = bytes.fromhex(hex_str)
            chks_code = self.calculate_chks(command_bytes)
            hex_str = hex_str + str(chks_code)
            command_bytes = bytes.fromhex(hex_str)
            with self._lock:
                self.serial.write(command_bytes)
            print(f"[发送指令] {hex_str}")
            return True
        except Exception as e:
            print(f"发送失败: {e}")
            return False

    def send_and_receive(self, hex_str, response_len=10):
        """
        发送指令并等待接收响应（同步方式）
        :param hex_str: 9字节十六进制字符串（不含CHKS）
        :param response_len: 期望接收的字节数，默认10
        :return: 响应字节列表，失败返回 None
        """
        if not self.serial.is_open:
            print("串口未打开")
            return None
        try:
            # 构建完整帧
            command_bytes = bytes.fromhex(hex_str)
            chks_code = self.calculate_chks(command_bytes)
            full_hex = hex_str + str(chks_code)
            command_bytes = bytes.fromhex(full_hex)

            with self._lock:
                self.serial.reset_input_buffer()  # 清空接收缓冲区
                self.serial.write(command_bytes)
                print(f"[发送指令] {full_hex}")

                # 等待响应
                response = self.serial.read(response_len)

            if len(response) == response_len:
                response_hex = ' '.join(f"{b:02X}" for b in response)
                print(f"[收到响应] {response_hex}")
                return list(response)
            else:
                print(f"[响应超时] 期望 {response_len} 字节，实际收到 {len(response)} 字节")
                return None
        except Exception as e:
            print(f"通讯失败: {e}")
            return None

    # ========================================================================
    #  读取对象方法（核心新增）
    # ========================================================================

    def read_object(self, index, subindex=0x00):
        """
        读取驱动器对象字典中的值
        :param index: 对象索引，如 0x6041
        :param subindex: 子索引，默认0
        :return: (raw_bytes, value) 元组，失败返回 (None, None)

        协议格式（手册第9.3.2节）：
        byte0: 站号(01)
        byte1: 命令字(40 = 读取)
        byte2: 索引低字节
        byte3: 索引高字节
        byte4: 子索引
        byte5-8: 0x00（读取时数据区填0）
        byte9: CHKS校验码
        """
        index_low = index & 0xFF
        index_high = (index >> 8) & 0xFF

        cmd = f"01 40 {index_low:02X} {index_high:02X} {subindex:02X} 00 00 00 00 "
        response = self.send_and_receive(cmd)

        if response is None:
            return (None, None)

        # 响应 byte5~byte8 为数据（小端字节序）
        data_bytes = response[5:9]
        value = struct.unpack('<I', bytes(data_bytes))[0]
        return (data_bytes, value)

    # ========================================================================
    #  状态监控方法
    # ========================================================================

    def read_status_word(self):
        """
        读取状态字 0x6041
        :return: 状态字数值(16位)，失败返回 None
        """
        data_bytes, value = self.read_object(0x6041)
        if value is not None:
            value = value & 0xFFFF  # 状态字只有16位
            status_text = self.parse_status_word(value)
            print(f"[状态字] 0x{value:04X} -> {status_text}")
        return value

    def read_actual_position(self):
        """
        读取实际位置 0x6064
        :return: 实际位置(脉冲增量，有符号32位)，失败返回 None
        """
        data_bytes, value = self.read_object(0x6064)
        if value is not None:
            # 转为有符号32位
            if value >= 0x80000000:
                value = value - 0x100000000
            print(f"[实际位置] {value} inc")
        return value

    def read_actual_velocity(self):
        """
        读取实际速度 0x606C
        :return: 实际速度(驱动器内部单位)，失败返回 None
        """
        data_bytes, value = self.read_object(0x606C)
        if value is not None:
            if value >= 0x80000000:
                value = value - 0x100000000
            # 内部单位转 RPM: rpm = value * 1875 / (512 * 65536)
            rpm = value * 1875 / (512 * 65536)
            print(f"[实际速度] {rpm:.2f} RPM (原始值: {value})")
            return rpm
        return None

    def read_actual_torque(self):
        """
        读取实际力矩 0x6077
        :return: 实际力矩(千分比)，失败返回 None
        """
        data_bytes, value = self.read_object(0x6077)
        if value is not None:
            value = value & 0xFFFF
            if value >= 0x8000:
                value = value - 0x10000
            print(f"[实际力矩] {value / 10:.1f}% 额定力矩")
        return value

    def read_error_code(self):
        """
        读取错误代码 0x2601
        :return: 错误代码，失败返回 None
        """
        data_bytes, value = self.read_object(0x2601)
        if value is not None:
            error_text = self.parse_error_code(value)
            print(f"[错误代码] 0x{value:04X} -> {error_text}")
        return value

    def read_operation_mode(self):
        """
        读取当前操作模式 0x6061
        :return: 模式代码，失败返回 None
        """
        data_bytes, value = self.read_object(0x6061)
        if value is not None:
            value = value & 0xFF
            mode_text = self.parse_operation_mode(value)
            print(f"[操作模式] {value} -> {mode_text}")
        return value

    def get_full_status(self):
        """
        一次性读取所有关键状态信息
        :return: 字典，包含所有状态数据
        """
        print("=" * 60)
        print("  Kinco 伺服驱动器 状态总览")
        print("=" * 60)

        status = {}

        status['status_word'] = self.read_status_word()
        time.sleep(0.05)

        status['operation_mode'] = self.read_operation_mode()
        time.sleep(0.05)

        status['actual_position'] = self.read_actual_position()
        time.sleep(0.05)

        status['actual_velocity'] = self.read_actual_velocity()
        time.sleep(0.05)

        status['actual_torque'] = self.read_actual_torque()
        time.sleep(0.05)

        status['error_code'] = self.read_error_code()

        print("=" * 60)
        return status

    # ========================================================================
    #  后台持续监控线程
    # ========================================================================

    def start_monitoring(self, interval=0.5):
        """
        启动后台状态监控线程
        :param interval: 轮询间隔（秒），默认0.5秒
        """
        self.running = True
        self.monitor_interval = interval
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()
        print(f"[监控] 后台监控已启动，轮询间隔 {interval}s")

    def _monitor_loop(self):
        """后台监控循环"""
        while self.running:
            try:
                # 读取状态字
                data_bytes, value = self.read_object(0x6041)
                if value is not None:
                    sw = value & 0xFFFF
                    state = self.parse_status_word(sw)

                    # 读取实际位置
                    time.sleep(0.03)
                    _, pos_val = self.read_object(0x6064)
                    if pos_val is not None and pos_val >= 0x80000000:
                        pos_val = pos_val - 0x100000000

                    # 打包放入队列供外部使用
                    monitor_data = {
                        'timestamp': time.time(),
                        'status_word': sw,
                        'state': state,
                        'position': pos_val,
                    }
                    self.receive_queue.put(monitor_data)

                    # 检测故障
                    if sw & 0x08:  # bit3 = Fault
                        print(f"[监控 警告] 检测到故障！状态字: 0x{sw:04X}")

            except Exception as e:
                print(f"[监控 异常] {e}")

            time.sleep(self.monitor_interval)

    def stop_monitoring(self):
        """停止监听并关闭串口"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=3)
        if self.serial.is_open:
            self.serial.close()
        print("已停止监听，串口已关闭")

    # ========================================================================
    #  故障处理方法
    # ========================================================================

    def clear_fault(self):
        """
        清除故障并重新使能电机
        流程：
          1. 写控制字 0x0080（故障复位，bit7上升沿触发）
          2. 等待200ms
          3. 写控制字 0x0000（清零）
          4. 等待200ms
          5. 写控制字 0x0006（关闭 -> 准备好）
          6. 等待200ms
          7. 写控制字 0x0007（准备好 -> 开启）
          8. 等待200ms
          9. 写控制字 0x000F（开启 -> 使能运行）
        :return: True 清除成功, False 清除失败
        """
        print("[故障清除] 开始执行故障清除流程...")

        # 步骤1: 发送故障复位指令 (bit7 = 1)
        print("[故障清除] 步骤1: 发送故障复位 (0x0080)...")
        cmd = "01 2B 40 60 00 80 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.2)

        # 步骤2: 清零控制字
        print("[故障清除] 步骤2: 清零控制字 (0x0000)...")
        cmd = "01 2B 40 60 00 00 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.2)

        # 步骤3: 按 CiA402 状态机逐步使能
        print("[故障清除] 步骤3: Shutdown (0x0006)...")
        cmd = "01 2B 40 60 00 06 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.2)

        print("[故障清除] 步骤4: Switch On (0x0007)...")
        cmd = "01 2B 40 60 00 07 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.2)

        print("[故障清除] 步骤5: Enable Operation (0x000F)...")
        cmd = "01 2B 40 60 00 0F 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.3)

        # 验证是否清除成功
        status = self.read_status_word()
        if status is not None:
            if status & 0x08:  # bit3 仍然为1 -> 仍在故障状态
                print("[故障清除] ✗ 故障未能清除，请检查故障原因！")
                self.read_error_code()
                return False
            elif (status & 0x6F) == 0x27:  # Operation Enabled
                print("[故障清除] ✓ 故障已清除，电机已重新使能！")
                return True
            else:
                print(f"[故障清除] △ 故障已清除，但电机未完全使能，当前状态: 0x{status:04X}")
                return True
        else:
            print("[故障清除] ? 无法读取状态，请检查通讯")
            return False

    def quick_fault_reset(self):
        """
        快速故障复位（仅发送复位指令 + 使能，不做状态验证）
        适用于需要快速恢复的场景
        """
        print("[快速复位] 发送故障复位...")
        cmd = "01 2B 40 60 00 80 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.3)

        print("[快速复位] 重新使能...")
        self.device_enabled()

    # ========================================================================
    #  模式设置方法
    # ========================================================================

    def set_operation_mode(self, mode):
        """
        设置驱动器操作模式（写对象 0x6060）

        :param mode: 模式代码，支持数字或字符串：
            1  / 'position'  -> 位置模式 (Profile Position)
            3  / 'velocity'  -> 速度模式 (Profile Velocity)
           -3  / 'analog_velocity' -> 模拟速度模式
            4  / 'torque'    -> 力矩模式
           -4  / 'pulse'     -> 脉冲模式
            6  / 'homing'    -> 原点回归模式
            7  / 'ip'        -> 插补位置模式
        :return: True 设置成功, False 失败

        注意：切换模式前应先将电机去使能（或至少处于 Switched On 状态），
              切换完成后再重新使能，否则部分模式切换可能不生效。
        """
        # 支持字符串别名
        mode_alias = {
            'position': 1,
            'velocity': 3,
            'analog_velocity': -3,
            'torque': 4,
            'pulse': -4,
            'homing': 6,
            'ip': 7,
        }

        if isinstance(mode, str):
            mode = mode_alias.get(mode.lower())
            if mode is None:
                print(f"[模式设置] 不支持的模式名称，可选: {list(mode_alias.keys())}")
                return False

        # 有符号转无符号（-3 -> 0xFD, -4 -> 0xFC）
        if mode < 0:
            mode_byte = mode & 0xFF
        else:
            mode_byte = mode

        mode_name = self.parse_operation_mode(mode_byte)
        print(f"[模式设置] 切换至: {mode_name}")

        # 0x2F = SDO 写1字节, 索引 0x6060, 子索引 0x00
        cmd = f"01 2F 60 60 00 {mode_byte:02X} 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.2)

        # 读回验证
        current_mode = self.read_operation_mode()
        if current_mode is not None:
            # 比较时统一转有符号
            if current_mode >= 128:
                current_mode = current_mode - 256
            expected = mode if mode < 128 else mode - 256
            if current_mode == expected:
                print(f"[模式设置] ✓ 模式切换成功")
                return True
            else:
                print(f"[模式设置] △ 读回模式不一致，可能需要先去使能再切换")
                return False
        return False

    def switch_to_position_mode(self):
        """
        完整切换到位置模式的推荐流程：
          1. 去使能 (Shutdown)
          2. 设置模式为位置模式 (0x6060 = 1)
          3. 重新使能 (Enable Operation)
        """
        print("[位置模式] 开始切换到位置模式...")

        # 步骤1: Shutdown（去使能但保持通讯）
        print("[位置模式] 步骤1: Shutdown (0x0006)...")
        cmd = "01 2B 40 60 00 06 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.2)

        # 步骤2: 写入操作模式 = 1（位置模式）
        print("[位置模式] 步骤2: 设置操作模式 = 位置模式...")
        result = self.set_operation_mode(1)
        time.sleep(0.2)

        # 步骤3: 重新使能
        print("[位置模式] 步骤3: Switch On (0x0007)...")
        cmd = "01 2B 40 60 00 07 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.2)

        print("[位置模式] 步骤4: Enable Operation (0x000F)...")
        cmd = "01 2B 40 60 00 0F 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.2)

        print("[位置模式] ✓ 切换完成")
        return result

    def switch_to_velocity_mode(self):
        """完整切换到速度模式（流程与位置模式相同，mode=3）"""
        print("[速度模式] 开始切换到速度模式...")
        cmd = "01 2B 40 60 00 06 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.2)

        result = self.set_operation_mode(3)
        time.sleep(0.2)

        cmd = "01 2B 40 60 00 07 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.2)

        cmd = "01 2B 40 60 00 0F 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.2)

        print("[速度模式] ✓ 切换完成")
        return result

    def switch_to_homing_mode(self):
        """完整切换到原点回归模式（mode=6）"""
        print("[原点模式] 开始切换到原点回归模式...")
        cmd = "01 2B 40 60 00 06 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.2)

        result = self.set_operation_mode(6)
        time.sleep(0.2)

        cmd = "01 2B 40 60 00 07 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.2)

        cmd = "01 2B 40 60 00 0F 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.2)

        print("[原点模式] ✓ 切换完成")
        return result

    # ========================================================================
    #  原有控制方法（保持不变）
    # ========================================================================

    def device_enabled(self):
        """使能电机（完整状态机过渡）"""
        # Shutdown
        cmd = "01 2B 40 60 00 06 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.2)
        # Switch On
        cmd = "01 2B 40 60 00 07 00 00 00 "
        self.send_command(cmd)
        time.sleep(0.2)
        # Enable Operation
        cmd = "01 2B 40 60 00 0F 00 00 00 "
        self.send_command(cmd)


    def device_location_set(self, num_inc):
        """位置设定"""
        cmd = "01 23 7A 60 00 "
        num_inc = self.num_conversion(num_inc)
        cmd = cmd + num_inc + " "
        self.send_command(cmd)

    def device_speed_set(self, num_inc):
        """速度设置"""
        cmd = "01 23 81 60 00 "
        num_inc = int(num_inc * 512 * 65536 / 1875)
        num_inc = self.num_conversion(num_inc)
        cmd = cmd + num_inc + " "
        self.send_command(cmd)

    def device_start(self, con_word):
        """控制字设置"""
        cmd = "01 2B 40 60 00 " + str(con_word) + " 00 00 00 "
        self.send_command(cmd)

    # ========================================================================
    #  工具方法
    # ========================================================================

    def num_conversion(self, num):
        """数字转小端4字节（支持负数）"""
        packed = struct.pack('<i', num)  # 小写 'i' = 有符号
        hex_str = ' '.join(f'{b:02X}' for b in packed)
        return hex_str

    def calculate_chks(self, data_frame):
        """计算CHKS校验码"""
        if len(data_frame) != 9:
            raise ValueError(f"输入数据帧长度必须为9字节，当前为{len(data_frame)}字节")
        sum_bytes = sum(data_frame)
        chks_code = (-sum_bytes) & 0xFF
        chks_code = f"{chks_code:02X}"
        return chks_code

    # ========================================================================
    #  状态解析方法
    # ========================================================================

    def parse_status_word(self, sw):
        """
        解析 CiA402 状态字 (0x6041)
        :param sw: 16位状态字
        :return: 状态描述字符串
        """
        # CiA402 状态机判断（低7位掩码）
        state_bits = sw & 0x6F

        if state_bits == 0x00:
            state = "Not Ready to Switch On (初始化中)"
        elif state_bits == 0x40:
            state = "Switch On Disabled (等待上电)"
        elif state_bits == 0x21:
            state = "Ready to Switch On (准备就绪)"
        elif state_bits == 0x23:
            state = "Switched On (已开启)"
        elif state_bits == 0x27:
            state = "Operation Enabled (运行使能)"
        elif state_bits == 0x07:
            state = "Quick Stop Active (快速停止中)"
        elif state_bits == 0x0F or state_bits == 0x08:
            state = "Fault (故障)"
        else:
            state = f"未知状态 (bits=0x{state_bits:02X})"

        # 附加标志位
        flags = []
        if sw & 0x0400:  # bit10
            flags.append("目标到达")
        if sw & 0x1000:  # bit12
            flags.append("跟随/设定确认")
        if sw & 0x2000:  # bit13
            flags.append("通讯报警")

        if flags:
            state += " | " + ", ".join(flags)

        return state

    def parse_error_code(self, code):
        """
        解析错误代码
        :param code: 错误代码数值
        :return: 错误描述字符串
        """
        error_map = {
            0x0000: "无错误",
            0x2310: "过流",
            0x3110: "直流母线过压",
            0x3120: "直流母线欠压",
            0x4210: "驱动器过温",
            0x5113: "逻辑电源异常",
            0x5441: "编码器异常",
            0x6010: "软件内部错误",
            0x7121: "电机堵转",
            0x7305: "编码器计数异常",
            0x7500: "通讯超时",
            0x8311: "跟随误差过大",
            0x8400: "速度偏差过大",
            0x8611: "位置超限",
            0xFF01: "过载",
            0xFF02: "抱闸异常",
        }
        return error_map.get(code, f"未知错误 (0x{code:04X})")

    def parse_operation_mode(self, mode):
        """
        解析操作模式
        :param mode: 模式代码 (有符号8位)
        :return: 模式描述字符串
        """
        if mode >= 128:
            mode = mode - 256
        mode_map = {
            1:  "位置模式 (Profile Position)",
            3:  "速度模式 (Profile Velocity)",
            -3: "速度模式-模拟量 (Analog Velocity)",
            4:  "力矩模式 (Torque)",
            -4: "脉冲模式 (Pulse)",
            6:  "原点回归模式 (Homing)",
            7:  "插补位置模式 (Interpolated Position)",
        }
        return mode_map.get(mode, f"未知模式 ({mode})")


slide_control = ModbusRTUMonitor(
        port='/dev/ttyACM0',  # Windows: 'COM3' / 'COM5'
        baudrate=38400
    )

# ============================================================================
#  使用示例
# ============================================================================

if __name__ == "__main__":

    monitor = ModbusRTUMonitor(
        port='/dev/ttyACM0',  # Windows: 'COM3' / 'COM5'
        baudrate=38400
    )

    if not monitor.serial.is_open:
        monitor.serial.open()

    # --------------------------------------------------
    #  示例1: 读取完整状态
    # --------------------------------------------------
    monitor.switch_to_position_mode()
    print("\n>>> 示例1: 读取驱动器当前状态")
    monitor.get_full_status()
    time.sleep(0.5)

    # --------------------------------------------------
    #  示例2: 正常运动流程（带状态检查）
    # --------------------------------------------------
    print("\n>>> 示例2: 执行位置运动（带状态检查）")

    # 使能电机
    monitor.device_enabled()
    time.sleep(0.5)
    monitor.switch_to_position_mode()

    # 确认使能成功
    sw = monitor.read_status_word()
    if sw is not None and (sw & 0x6F) == 0x27:
        print("电机已使能，开始运动...")

        monitor.device_location_set(2000000)
        time.sleep(0.5)

        monitor.device_speed_set(200)
        time.sleep(0.5)

        # 控制启动
        monitor.device_start("2F")
        time.sleep(0.5)
        monitor.device_start("3F")

        # 等待运动完成，轮询目标到达标志
        print("等待运动完成...")
        for i in range(60):  # 最多等待30秒
            time.sleep(0.5)
            sw = monitor.read_status_word()
            if sw is not None and (sw & 0x0400):  # bit10 = Target Reached
                print("✓ 目标位置已到达！")
                pos = monitor.read_actual_position()
                break
        else:
            print("△ 等待超时，运动可能未完成")

    else:
        print("使能失败，尝试清除故障...")
        monitor.clear_fault()

    # --------------------------------------------------
    #  示例3: 切换到位置模式
    # --------------------------------------------------
    # 一键切换（自动完成 去使能 -> 设模式 -> 重新使能）
    #
    # print("\n>>> 示例3: 切换到位置模式")
    # monitor.switch_to_position_mode()

    # 或者用通用方法，传数字或字符串都行：
    # monitor.set_operation_mode(1)
    # monitor.set_operation_mode('position')
    # monitor.set_operation_mode('velocity')   # 速度模式
    # monitor.set_operation_mode('homing')     # 原点回归

    # --------------------------------------------------
    #  示例4: 完整的模式切换 + 运动流程
    # --------------------------------------------------
    # print("\n>>> 示例4: 切换到位置模式并运动")
    # monitor.switch_to_position_mode()
    # time.sleep(0.3)
    # monitor.device_location_set(1000000)
    # time.sleep(0.3)
    # monitor.device_speed_set(100)
    # time.sleep(0.3)
    # monitor.device_start("2F")
    # time.sleep(0.5)
    # monitor.device_start("3F")

    # --------------------------------------------------
    #  示例5: 故障清除
    # --------------------------------------------------
    # print("\n>>> 示例5: 清除故障")
    # monitor.clear_fault()

    # --------------------------------------------------
    #  示例6: 后台监控模式
    # --------------------------------------------------
    # print("\n>>> 示例6: 启动后台监控")
    # monitor.start_monitoring(interval=1.0)
    # time.sleep(10)
    # while not monitor.receive_queue.empty():
    #     data = monitor.receive_queue.get()
    #     print(f"  时间:{data['timestamp']:.1f} "
    #           f"状态:{data['state']} "
    #           f"位置:{data['position']}")
    # monitor.stop_monitoring()
