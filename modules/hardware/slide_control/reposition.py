from .slide import slide_control
import time

from modules.hardware.head_control import pan_tilt

# 控制器重启后会把当前位置重新记为 0，因此头部和滑轨都回 0。
HEAD_HORIZONTAL = 0
HEAD_VERTICAL = 0
HOME_POSITION_INC = 0


def home_head():
    print(f"头部归正: horizontal={HEAD_HORIZONTAL}, vertical={HEAD_VERTICAL}")
    pan_tilt.move_absolute(
        horizontal=HEAD_HORIZONTAL,
        vertical=HEAD_VERTICAL,
    )


def send_axis(abs_axis):
    # 发送坐标
    slide_control.device_location_set(abs_axis)
    time.sleep(0.5)
    # 启动控制
    slide_control.device_start("2F")
    time.sleep(0.5)
    slide_control.device_start("3F")
    print("等待运动完成...")
    for i in range(60):  # 最多等待30秒
        time.sleep(0.5)
        sw = slide_control.read_status_word()
        if sw is not None and (sw & 0x0400):  # bit10 = Target Reached
            print("✓ 目标位置已到达！")
            pos = slide_control.read_actual_position()
            break


if __name__ == "__main__":
    home_head()
    time.sleep(0.5)

    # 检查是否开启
    if not slide_control.serial.is_open:
        slide_control.serial.open()

    # 检查使能
    sw = slide_control.read_status_word()
    if sw is not None and (sw & 0x6F) == 0x27:
        print("电机已使能，开始运动...")
        print(f"滑轨归正目标: {HOME_POSITION_INC} inc")
        send_axis(HOME_POSITION_INC)
    else:
        print("使能失败，尝试清除故障...")
        slide_control.clear_fault()
        print("重新设为位置模式")
        slide_control.switch_to_position_mode()
        # 发送速度
        slide_control.device_speed_set(200)
        print("已恢复，请重新运行脚本")
