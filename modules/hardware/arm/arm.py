'''
机械臂单例
'''
from Robotic_Arm.rm_robot_interface import *

LEFT_ARM_IP = "192.168.192.19"
# RIGHT_ARM_IP = "192.168.192.19"
ARM_PORT = 8080




left_arm = RoboticArm(rm_thread_mode_e.RM_TRIPLE_MODE_E)
left_arm.rm_create_robot_arm(LEFT_ARM_IP, ARM_PORT)
# right_arm = RoboticArm(rm_thread_mode_e.RM_TRIPLE_MODE_E)
# right_arm.rm_create_robot_arm(RIGHT_ARM_IP, ARM_PORT)

# code = left_arm.rm_movej(LEFT_TARGET_JOINTS, v=SPEED, r=0, connect=0, block=1)
# code = right_arm.rm_movej(RIGHT_TARGET_JOINTS, v=SPEED, r=0, connect=0, block=1)
# print("完成" if code == 0 else f"失败，错误码: {code}")

# left_arm.rm_delete_robot_arm()
# right_arm.rm_delete_robot_arm()