# 地面识别与抓取位姿说明

当前地面站点由：

`modules/hardware/mission_slide/controller.py`

中的：

```python
LOW_STATIONS = frozenset({"LM3", "LM4"})
```

决定。

所有 `LOW_STATIONS` 共用同一套参数，填写位置为：

`config/perception.yaml -> arm -> ground_pose_override`

参数：

- `observation_joints_deg`：所有地面点共用的识别关节位姿，6 个关节角，单位度。
- `grasp_orientation_rad`：所有地面点共用的抓取末端姿态 `[rx, ry, rz]`，单位弧度。
- `final_tool_offset_m`：所有地面点共用的最终抓取工具偏移，单位米。
- `transition_tool_offset_m`：所有地面点共用的接近/撤回工具偏移，单位米。

LM2、LM5、LM6 等不在 `LOW_STATIONS` 中的点继续使用 `arm` 下原来的全局参数。

以后增加或减少地面点，只需修改 `LOW_STATIONS`，不需要再复制地面位姿。
