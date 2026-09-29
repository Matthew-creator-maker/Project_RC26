import time
from .gripper_servo import Gripper
from Robotic_Arm.rm_robot_interface import *

arm = RoboticArm(rm_thread_mode_e.RM_TRIPLE_MODE_E)
handle = arm.rm_create_robot_arm("192.168.1.18", 8080)

gripper = Gripper(arm)
gripper.open()
time.sleep(2)
gripper.close()
time.sleep(2)

print(gripper.state)