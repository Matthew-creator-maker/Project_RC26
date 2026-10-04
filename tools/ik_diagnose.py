"""只读 IK 诊断：查询状态与逆解，不移动机械臂、不切换坐标系。

在项目根目录运行 python -m tools.ik_diagnose --help。
默认只打印配置生成的抓取点；添加 --connect 才连接机械臂进行只读查询。
参考接口：https://develop.realman-robotics.com/robot/apipython/classes/algo/
"""
from __future__ import annotations

import argparse
import ctypes
import importlib
import json
import math
from pathlib import Path


def vector(value, count, name):
    """长度、单位和有限数检查；这里只检查输入，不发任何指令。"""
    if len(value) != count:
        raise ValueError(f"{name} 必须有 {count} 个数")
    result = [float(x) for x in value]
    if not all(math.isfinite(x) for x in result):
        raise ValueError(f"{name} 含 NaN/Inf")
    return result


def prepare(config_path, station, xyz_base):
    """按 main 使用的高度档复现两个目标，避免误用全局抓取姿态。"""
    from modules.perception.recognition_config import read_yaml
    from modules.perception.handeye_transform import (
        realman_pose_to_matrix, offset_pose_in_tool, matrix_to_realman_pose,
    )
    config = read_yaml(config_path)
    station = station.strip().upper()
    matches = [(name, profile) for name, profile in config.get('height_profiles', {}).items()
               if station in [str(s).strip().upper() for s in profile.get('stations', [])]]
    if len(matches) != 1:
        raise ValueError(f"{station} 必须恰好属于一个抓取高度档，当前找到 {len(matches)} 个")
    name, profile = matches[0]
    if profile.get('enabled') is not True:
        raise ValueError(f"{name} 未启用，先确认实机标定")
    arm = dict(config['arm'])
    for key in ('grasp_orientation_rad', 'target_offset_base_m', 'final_tool_offset_m', 'transition_tool_offset_m'):
        if key in profile:
            arm[key] = profile[key]
    orientation = vector(arm['grasp_orientation_rad'], 3, '抓取姿态（弧度）')
    center = realman_pose_to_matrix([*vector(xyz_base, 3, '物品中心（米）'), *orientation])
    arm['target_offset_base_m'] = vector(
        arm.get('target_offset_base_m', [0.0, 0.0, 0.0]), 3, '基座 XYZ 补偿（米）')
    center[:3, 3] += arm['target_offset_base_m']
    final = offset_pose_in_tool(center, vector(arm['final_tool_offset_m'], 3, '最终偏移（米）'))
    transition = offset_pose_in_tool(final, vector(arm['transition_tool_offset_m'], 3, '过渡偏移（米）'))
    poses = {'transition': matrix_to_realman_pose(transition), 'final': matrix_to_realman_pose(final)}
    return config, {'config_path': str(Path(config_path).resolve()),
                    'station': station, 'height_profile': name, 'xyz_base_m': list(xyz_base),
                    'effective_grasp_parameters': {key: arm[key] for key in
                        ('grasp_orientation_rad', 'target_offset_base_m', 'final_tool_offset_m', 'transition_tool_offset_m')},
                    'poses_m_rad': poses, 'hardware_connected': False,
                    'note': 'IK 成功不等于整段运动无碰撞；本工具不会执行抓取。'}


def query_frame(arm, method):
    code, frame = getattr(arm, method)()
    if code != 0 or not isinstance(frame, dict):
        raise RuntimeError(f"{method} 查询失败：{code}, {frame}")
    return frame


def describe_solution(arm, joints, j3_margin):
    """仅评价已算出的关节角；绝不把结果交给运动接口。"""
    joints = vector(joints, 6, 'IK 关节角（度）')
    result = {'joints_deg': joints, 'j3_margin_ok': abs(joints[2]) >= j3_margin}
    check = getattr(arm, 'rm_algo_ikine_check_joint_position_limit', None)
    if callable(check):
        try:
            try:
                code = check(joints)
            except ctypes.ArgumentError:
                code = check((ctypes.c_float * 6)(*joints))
            result['joint_limit_return'] = int(code)
            # 0：未超限；正整数：该编号关节超限；-1：SDK 不支持该检查。
        except Exception as exc:
            result['joint_limit_query_error'] = str(exc)
    else:
        result['joint_limit_query_error'] = '当前 SDK 不支持关节限位查询'
    return result


def solve_once(arm, params_type, pose, seed, j3_margin):
    row = {'target_pose_m_rad': list(pose), 'reference_joints_deg': list(seed)}
    try:
        code, joints = arm.rm_algo_inverse_kinematics(params_type(seed, pose, 1))
        row['return_code'] = int(code)
        if code == 0:
            row['solution'] = describe_solution(arm, joints, j3_margin)
    except Exception as exc:
        row['error'] = str(exc)
    return row


def diagnose(arm, params_type, config, report, expected_tool='gripper'):
    """可用假 SDK 离线测试；真实版本也只调用查询和算法接口。"""
    report['tool_frame'] = query_frame(arm, 'rm_get_current_tool_frame')
    report['work_frame'] = query_frame(arm, 'rm_get_current_work_frame')
    name = report['tool_frame'].get('name', report['tool_frame'].get('frame_name'))
    if isinstance(name, bytes):
        name = name.decode('utf-8')
    if name != expected_tool:
        raise RuntimeError(f"当前工具为 {name!r}，预期为 {expected_tool!r}；停止本次诊断，不切换坐标系")
    work_name = report['work_frame'].get('name', report['work_frame'].get('frame_name'))
    if isinstance(work_name, bytes):
        work_name = work_name.decode('utf-8')
    if work_name != 'Base':
        raise RuntimeError(f"当前工作坐标系为 {work_name!r}；输入是 Base 坐标，请先核对实际设置")
    code, state = arm.rm_get_current_arm_state()
    if code != 0:
        raise RuntimeError(f"读取当前机械臂状态失败：{code}")
    seed = vector(state.get('joint', state.get('joints', [])), 6, '当前关节角')
    current = vector(state.get('pose', []), 6, '当前 TCP 位姿')
    report['current_state'] = state
    margin = float(config.get('grasp_test', {}).get('motion_safety', {}).get('j3_min_abs_deg', 8))
    # 检查当前已实现位姿的逆解，帮助识别算法配置/坐标系问题。
    report['current_pose_ik'] = solve_once(arm, params_type, current, seed, margin)
    poses = report['poses_m_rad']
    transition = solve_once(arm, params_type, poses['transition'], seed, margin)
    report['transition_ik'] = transition
    report['final_ik_current_seed'] = solve_once(arm, params_type, poses['final'], seed, margin)
    if 'solution' in transition:
        # 两个点都只预检。把预测过渡关节角作为最终点参考，可以检查构型选解的影响。
        report['final_ik_transition_seed'] = solve_once(
            arm, params_type, poses['final'], transition['solution']['joints_deg'], margin)
    all_solve = getattr(arm, 'rm_algo_inverse_kinematics_all', None)
    if callable(all_solve):
        try:
            result = all_solve(params_type(seed, poses['final'], 1))
            # 官方 SDK 返回结构体；兼容部分版本返回字典的封装。
            get = lambda key: result[key] if isinstance(result, dict) else getattr(result, key)
            code, count = int(get('result')), int(get('num'))
            all_report = {'return_code': code, 'num': count, 'solutions': []}
            if code == 0 and count > 0:
                solutions = get('q_solve')
                if count > len(solutions):
                    raise ValueError('SDK 报告的解数量超过返回数组长度')
                for values in solutions[:count]:
                    all_report['solutions'].append(describe_solution(arm, list(values)[:6], margin))
            report['final_all_solutions'] = all_report
        except Exception as exc:
            report['final_all_solutions'] = {'error': str(exc)}
    else:
        report['final_all_solutions'] = {'error': '当前 SDK 无全解接口，其他诊断仍有效'}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[1]/'config/perception.yaml')
    parser.add_argument('--station', default='LM6')
    parser.add_argument('--xyz-base', type=float, nargs=3, required=True, metavar=('X', 'Y', 'Z'),
                        help='日志的 xyz_base_m 物品中心，单位米；不要填 xyz_camera_m 或 final_xyz_m')
    parser.add_argument('--connect', action='store_true', help='连接机械臂，只查询状态与 IK，不发送运动指令')
    parser.add_argument('--expected-tool', default='gripper')
    parser.add_argument('--output', type=Path, help='可选：保存 JSON 报告')
    args = parser.parse_args(argv)
    report = {}
    failed = False
    try:
        config, report = prepare(args.config, args.station, args.xyz_base)
        if args.connect:
            sdk = importlib.import_module('Robotic_Arm.rm_robot_interface')
            arm = sdk.RoboticArm(sdk.rm_thread_mode_e.RM_TRIPLE_MODE_E)
            handle = arm.rm_create_robot_arm(config['arm']['ip'], int(config['arm'].get('port', 8080)))
            if getattr(handle, 'id', -1) < 0:
                raise RuntimeError('机械臂连接失败')
            report['hardware_connected'] = True
            try:
                diagnose(arm, sdk.rm_inverse_kinematics_params_t, config, report, args.expected_tool)
            finally:
                arm.rm_delete_robot_arm()
    except Exception as exc:
        report['diagnostic_error'] = str(exc)
        failed = True
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(text)
    if args.output:
        args.output.write_text(text+'\n', encoding='utf-8')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
