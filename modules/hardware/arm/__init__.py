# from .arm import left_arm, right_arm
from .arm import left_arm
from .gripper.gripper_servo import Gripper, GripperError
# from .gripper.gripper_io import IOGripper, IOGripperError

left_gripper = Gripper(left_arm)
# right_gripper = IOGripper(right_arm)