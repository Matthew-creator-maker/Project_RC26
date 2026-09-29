# LM1-LM7 软件验证报告

任务：**LM1 → LM2 → LM3 抓取后放下 → LM4 → LM5 抓取保持 → LM6 → LM7 松爪**。

## 本次验证结果

在当前离线环境执行：

```bash
python -m unittest discover -s tests -v
```

结果：**33 项测试收集，11 项执行通过，22 项因当前环境未安装完整视觉运行依赖而跳过；无失败**。

已实际执行并通过的部分包括：

- 完整 6 段导航命令顺序：LM1→LM2→LM3→LM4→LM5→LM6→LM7。
- LM3/LM5/LM7 动作调用顺序：release → carry → place。
- LM3 动作完成后回运输位；LM5 抓取后保持夹紧并回运输位。
- LM7 place 只执行释放逻辑，不需要视觉目标。
- 错误起点会在发送导航前终止。
- 导航拒绝、失败、超时、站点错误、急停、阻挡、推送断开、状态字段不完整均终止后续任务。
- LM3 动作失败后不会继续前往 LM4/LM5。
- `main.py` 纯预览不创建硬件对象。
- 项目相对路径会按 `task_config.json` 所在目录解析。

所有修改后的 Python 主文件均通过 `py_compile`；`main.py` 的纯软件预览输出与目标任务一致。

## 当前环境没有完成的验证

视觉/机械臂动作测试文件已经更新为 `release/carry/place` 三种语义，但由于当前容器没有完整安装 Ultralytics / RealSense / RealMan 等运行依赖，这些测试被 unittest 标记为 skip，而不是伪装成通过。

因此，本报告**不等于实机验收**。比赛前仍需在机器人 Ubuntu 环境执行：

```bash
python check_environment.py
python check_environment.py --hardware
python -m unittest discover -s tests -v
```

随后依次做导航-only、LM3 单动作、LM5 单动作、LM7 松爪和完整任务实测。

## 现场必须确认

1. `arm_transport.joints_deg` 是携带物品时也安全的实测运输位。
2. `grasp_test.workspace_m` 是实测安全工作空间；当前配置中的 `[-1, 1]` 宽范围必须重新确认。
3. LM3/LM5 停靠位置和朝向能让相机看到目标且机械臂可达。
4. LM5 抓住物品后回运输位时不会让物品撞机身。
5. LM7 当前在运输位直接松爪；若需要降低到容器/桌面，需要额外实测并配置放置姿态。
6. 现有代码没有持物传感器闭环，无法软件确认物品是否真的夹稳。
