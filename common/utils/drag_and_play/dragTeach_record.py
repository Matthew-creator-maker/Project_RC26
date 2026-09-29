from Robotic_Arm.rm_robot_interface import *
import time
import os
import sys
import select
from datetime import datetime  # 新增：用于生成时间戳

# -------------------------- 配置项 --------------------------
ARM_IP = "192.168.192.18"
ARM_PORT = 8080
# 轨迹保存目录（仅目录，不再是固定文件）
TRAJECTORY_SAVE_DIR = "/home/blinx/26-home/trajectory/"
# 拖动灵敏度（0-100，数值越小越沉，建议50-80）
DRAG_SENSITIVITY = 100

# -------------------------- 生成带时间戳的轨迹文件名 --------------------------
def generate_trajectory_filename():
    """生成带时间戳的轨迹文件名，避免覆盖"""
    # 时间戳格式：年-月-日_时-分-秒（例如 20240521_154030）
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 文件名：trajectory_时间戳.txt
    filename = f"trajectory_{timestamp}.txt"
    # 完整路径 = 保存目录 + 文件名
    full_path = os.path.join(TRAJECTORY_SAVE_DIR, filename)
    return full_path


# -------------------------- 主流程 --------------------------
if __name__ == "__main__":
    # 1. 初始化机械臂连接
    arm = RoboticArm(rm_thread_mode_e.RM_TRIPLE_MODE_E)
    handle = arm.rm_create_robot_arm(ARM_IP, ARM_PORT)
    if handle.id < 0:
        print("机械臂连接失败，请检查IP和网络")
        exit()
    print("机械臂连接成功")

    # 生成本次示教的轨迹文件名（带时间戳）
    trajectory_file = generate_trajectory_filename()

    try:
        # 2. 确保保存目录存在
        if not os.path.exists(TRAJECTORY_SAVE_DIR):
            os.makedirs(TRAJECTORY_SAVE_DIR)
            print(f" 自动创建轨迹保存目录：{TRAJECTORY_SAVE_DIR}")

        # 3. 设置拖动灵敏度
        ret = arm.rm_set_drag_teach_sensitivity(DRAG_SENSITIVITY)
        if ret == 0:
            print(f"✅ 拖动灵敏度设置成功：{DRAG_SENSITIVITY}")
        else:
            print(f"⚠️ 灵敏度设置失败，错误码：{ret}，使用默认灵敏度")

        # 4. 启动拖动示教，开启轨迹记录
        ret = arm.rm_start_drag_teach(trajectory_record=1)
        if ret != 0:
            raise RuntimeError(f"拖动示教启动失败，错误码：{ret}")

        # 5. 等待用户操作
        input("\n请按回车键结束示教...")
    except KeyboardInterrupt:
        print("\n\n⚠️ 检测到 Ctrl+C，强制结束拖动示教")
    except Exception as e:
        print(f"\n❌ 示教流程出错：{e}")
    finally:
        # 6. 停止拖动示教
        print("\n 正在停止拖动示教...")
        arm.rm_stop_drag_teach()
        time.sleep(0.5)

        # 7. 保存轨迹（使用带时间戳的文件名）
        print(" 正在保存轨迹...")
        ret, point_count = arm.rm_save_trajectory(trajectory_file)
        if ret == 0:
            print(f"✅ 轨迹保存成功！")

        else:
            print(f"❌ 轨迹保存失败，错误码：{ret}")

        # 8. 释放机械臂资源
        arm.rm_delete_robot_arm()