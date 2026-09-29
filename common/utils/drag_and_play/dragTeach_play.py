#!/usr/bin/env python3
# coding: utf-8
# 用法  ：  from dragTeach_play import play_robot_trajectory
# 替换为你实际的轨迹文件路径（比如录制的多轨迹中的某一个）
#   traj_file = "/home/blinx/桌面/arm/trajectory/trajectory_20240520_153045.txt"    
# 调用封装好的函数
#success = play_robot_trajectory(trajectory_file=traj_file)

from Robotic_Arm.rm_robot_interface import *
import time
import os
import json
import numpy as np
import sys

# -------------------------- 核心配置（可通过函数参数覆盖） --------------------------
DEFAULT_ARM_IP = "192.168.192.18"
DEFAULT_ARM_PORT = 8080

# 轨迹平滑配置
SKIP = 4
MAX_JOINT_STEP = 0.5
SEND_INTERVAL_MS = 20
FOLLOW_MODE = True  # 1.1.4版本必须用低跟随
TRAJECTORY_MODE = 0  # 曲线拟合模式
SMOOTH_RADIO = 50

# -------------------------- 轨迹加载与插补（内部辅助函数） --------------------------
def load_and_interpolate_trajectory(file_path):
    """加载轨迹文件并进行插补平滑（内部函数，无需外部调用）"""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"轨迹文件不存在：{file_path}")
    
    raw_joint_list = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            if i % SKIP != 0:
                continue
            try:
                data = json.loads(line)
                point = data.get("point")
                if point and len(point) == 6:
                    joints = [x / 1000.0 for x in point]
                    raw_joint_list.append(np.array(joints))
            except json.JSONDecodeError:
                continue
    
    if len(raw_joint_list) < 2:
        raise ValueError("轨迹点数量不足（至少需要2个点）")
    
    print(f"✅ 轨迹加载完成")
    print(f"   原始抽稀后：{len(raw_joint_list)} 个点")

    # 线性插补生成平滑点
    interpolated_joints = []
    for i in range(len(raw_joint_list) - 1):
        start = raw_joint_list[i]
        end = raw_joint_list[i+1]
        max_diff = np.max(np.abs(end - start))
        step_count = max(1, int(np.ceil(max_diff / MAX_JOINT_STEP)))
        for step in range(step_count + 1):
            inter = start * (1 - step/step_count) + end * (step/step_count)
            interpolated_joints.append(inter.tolist())
    
    # 去重
    final_joints = []
    last = None
    for j in interpolated_joints:
        if last is None or not np.allclose(j, last, atol=0.01):
            final_joints.append(j)
            last = j
    
    print(f"   插补后平滑：{len(final_joints)} 个点")
    print(f"   预计时长：{len(final_joints)*SEND_INTERVAL_MS/1000:.1f}s")
    return final_joints

def load_trajectory_raw(file_path):
    """
    Load and parse trajectory file without skipping points or interpolation.
    :param file_path: Path to the trajectory file
    :return: list of joint arrays (6 joints each, scaled by 1/1000)
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Trajectory file does not exist: {file_path}")
    
    raw_joint_list = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                point = data.get("point")
                if point and len(point) == 6:
                    # Scale down by 1000 as per original logic to convert units
                    joints = [x / 1000.0 for x in point]
                    raw_joint_list.append(np.array(joints))
            except json.JSONDecodeError:
                continue
    
    if len(raw_joint_list) < 2:
        raise ValueError("Insufficient trajectory points (need at least 2)")
    
    print(f"✅ Raw trajectory loaded")
    print(f"   Total points loaded: {len(raw_joint_list)}")
    
    return raw_joint_list
# -------------------------- 轨迹执行（内部辅助函数） --------------------------
def run_canfd_trajectory(arm, joint_list):
    """执行CANFD轨迹复现（内部函数，无需外部调用）"""
    # 1. 运动到轨迹起点
    first_joint = joint_list[0]
    # print(f"1. 运动到轨迹起点")
    arm.rm_movej(first_joint, 30, 0, 0, 1)
    # time.sleep(1.5)
    
    # 2. 高频透传执行轨迹
    # print(f"2. 开始透传执行轨迹...")
    total = len(joint_list)
    success = 0
    fail = 0
    interval = 0.005

    for idx, joint in enumerate(joint_list):
        ret = arm.rm_movej_canfd(
            joint=joint,
            follow=FOLLOW_MODE,
            expand=0.0,
            trajectory_mode=TRAJECTORY_MODE,
            radio=SMOOTH_RADIO
        )
        
        if ret == 0:
            success += 1
            print(f"\r✅ 第{idx+1}点成功", end="")
        else:
            fail += 1
            print(f"⚠️  第{idx+1}点失败：{ret}")

        
        time.sleep(interval)
    
    print(f"\n✅ 轨迹透传完成 | 总：{total} | 成功：{success} | 失败：{fail}")

# 核心函数
def play_robot_trajectory(trajectory_file, arm_ip=DEFAULT_ARM_IP, arm_port=DEFAULT_ARM_PORT, arm=None):
    """
    【外部调用核心函数】机械臂轨迹复现函数
    :param trajectory_file: 轨迹文件路径（必填）
    :param arm_ip: 机械臂IP地址（可选）
    :param arm_port: 机械臂端口（可选）
    :param arm: 已初始化的RoboticArm实例（可选，复用连接）
    :return: bool - 执行成功返回True，失败返回False
    """
    # DEBUG
    # print("="*60)
    # print(f"📌 开始执行轨迹复现：{trajectory_file}")
    # print("="*60)

    # 复用外部传入的arm实例，否则新建
    own_arm = arm is None
    if own_arm:
        arm = RoboticArm(rm_thread_mode_e.RM_TRIPLE_MODE_E)
        handle = arm.rm_create_robot_arm(arm_ip, arm_port)
        if handle.id < 0:
            print("❌ 机械臂连接失败")
            return False

    try:
        joint_list = load_trajectory_raw(trajectory_file)
        run_canfd_trajectory(arm, joint_list)
        return True
    except Exception as e:
        print(f"\n❌ 轨迹复现出错：{str(e)}")
        return False
    finally:
        # 仅销毁自己创建的arm，外部传入的不销毁
        if own_arm:
            arm.rm_delete_robot_arm()
            print("\n✅ 机械臂连接已释放")
        else:
            # print("\n✅ 复用的arm连接未释放")
            None
# -------------------------- 保留命令行运行逻辑 --------------------------
if __name__ == "__main__":
    # 命令行运行时，支持传入轨迹文件参数
    if len(sys.argv) == 2:
        trajectory_file = sys.argv[1]
    else:
        # 兼容原有逻辑，使用默认轨迹文件
        trajectory_file = "/home/blinx/桌面/arm/trajectory/grab_place_trajectory.txt"
    
    # 调用核心函数
    play_robot_trajectory(trajectory_file)