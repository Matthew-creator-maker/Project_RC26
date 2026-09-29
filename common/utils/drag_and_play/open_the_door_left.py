import time
from Robotic_Arm.rm_robot_interface import *
from Robotic_Arm.rm_robot_interface import RoboticArm, rm_thread_mode_e

from dragTeach_play import play_robot_trajectory

arm = RoboticArm(rm_thread_mode_e.RM_TRIPLE_MODE_E)
handle = arm.rm_create_robot_arm("192.168.192.19", 8080)
if handle.id < 0:
    raise RuntimeError("❌ 机械臂连接失败，请检查硬件/端口")

traj_file_get =  "./open_door_trajectory/get.txt"
traj_file_move =  "./open_door_trajectory/move.txt"
traj_file_leave = "./open_door_trajectory/leave.txt"

try:
    arm.rm_set_gripper_route(0, 1000)
    # 1. 张开夹爪
    print("打开夹爪...")
    arm.rm_set_gripper_release(speed=1000, block=False, timeout=10)
    # time.sleep(1.5)

    # 2. 移动到把手处（传入已创建的arm实例，避免重复销毁）
    print("执行轨迹：移动到把手处...")
    success = play_robot_trajectory(trajectory_file=traj_file_get, arm=arm)
    if not success:
        raise RuntimeError("❌ 移动到把手轨迹执行失败")

    # 3. 闭合夹爪（阻塞等待到位 + 校验状态）
    print("闭合夹爪...")
    # 闭合夹爪
    print(arm.rm_set_gripper_pick_on(500, 700, True, 10))#(速度，力度,持续力抓取)


    # 4. 拉开门（复用arm实例）
    print("执行轨迹：拉开门...")
    success = play_robot_trajectory(trajectory_file=traj_file_move, arm=arm)
    if not success:
        raise RuntimeError("❌ 拉开门轨迹执行失败")

    # 5. 张开夹爪
    print("打开夹爪...")
    arm.rm_set_gripper_release(speed=1000, block=True, timeout=10)
    
    success = play_robot_trajectory(trajectory_file=traj_file_leave, arm=arm)
    if not success:
        raise RuntimeError("❌ 轨迹执行失败")

except RuntimeError as e:
    print(f"\n❌ 轨迹执行失败：{e}")
finally:
    # 最终释放机械臂连接
    arm.rm_delete_robot_arm()
    print("\n✅ 程序结束，机械臂连接已释放")