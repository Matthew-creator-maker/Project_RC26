# 以夹爪接触反馈确认抓取

`modules/grasp/realman_arm.py` 使用阻塞式
`rm_set_gripper_pick_on(speed, force, True, timeout)`。不同版本的睿尔曼 API2
文档对 `-4` 有不同解释，因此单看 SDK 返回值无法判断是否夹住物品。

这版不再要求配置 `held_actpos_range` 或打开额外恢复开关。指令返回 `0` 或
`-4` 后，程序查询夹爪状态；若仍在闭合（`mode=4`），再短暂读取状态。
只有夹爪在线、无内部错误且反馈 `mode=6`（闭合时因力控接触停止），
程序才会执行撤回。`mode=2` 只表示完全闭合，`mode=4` 只表示仍在闭合，
两者都不构成夹持确认。其他 SDK 错误和无法读取状态均保持停止。

`config/perception.yaml` 的力控阈值从 `1000` 降为厂商示例的 `200`。
这只是较低力的试验起点，尚未证明能够稳定运输物品。先在固定、安全位置
观察夹爪是否接触目标、返回的 `mode` 和物品是否滑动，再根据实际夹持
需要调整力控阈值。抓取位、撤回位以及返回运输位的路径仍需独立验证。

## 查询实际夹爪状态

停止比赛程序后，在机器人上用同一个 Python 环境执行以下只读命令：

```bash
python3 - <<'PY'
from Robotic_Arm.rm_robot_interface import RoboticArm, rm_thread_mode_e, rm_api_version
print('API:', rm_api_version())
arm = RoboticArm(rm_thread_mode_e.RM_TRIPLE_MODE_E)
handle = arm.rm_create_robot_arm('192.168.192.18', 8080)
if handle.id < 0:
    raise RuntimeError(f'连接失败: {handle.id}')
try:
    print('夹爪状态:', arm.rm_get_gripper_state())
finally:
    arm.rm_delete_robot_arm()
PY
```

注意：`mode=6` 表示夹爪遇到了阻力，无法单独证明阻力来自正确物品；
若夹爪碰到桌面或机身，也可能出现接触反馈。确认两指确实夹住物品后，
再允许机器人执行抓取后的撤回和运输。
