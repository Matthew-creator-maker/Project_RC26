# 二次复检报告

## 结论

这次把修改版项目的运行链路重新从头检查了一遍。

**对于你们现在“提前建图 + RoboShop 打 LM 点 + 比赛时按 LM 点导航”的方案，不需要修改 `9.16_lu/agv_api/` 里的导航 SDK 源文件。**
比赛策略层只需要由 `main.py` 负责任务调度，由 `navigation_adapter.py` 负责把调度转换成 9.16 SDK 的点位导航调用。

`9.16_lu/agv_api/roboshop_lm_navigation.py` 中虽然也写了 `LM1 -> LM2 -> LM3`，但它只是一个可以单独运行的导航测试脚本，`main.py` 没有导入它，因此不会限制正式比赛路线。

## 为什么 9.16 不需要改

实际调用链是：

```text
main.py
  -> navigation_adapter.Navigator
      -> 动态加载 9.16_lu/agv_api/agv_api.py
      -> AGVApi.navigate_to(source, target)
      -> 9.16_lu/agv_api/agv_protocol.py
      -> TCP 发送给底盘
```

`AGVApi.navigate_to(source, target)` 本身接受任意 RoboShop 点位名，并没有把 LM1、LM2、LM3 写死。

`agv_client.py`、`agv_manager.py`、`agv_protocol.py` 都属于通讯/协议层，也没有比赛路线逻辑。

本次核对了 `navigation_grasp/9.16_lu/` 下 **15 个文件**，与用户最初的 `9.20_lu.zip` 相比 **0 个文件发生修改**。

## V2 额外修正

第一次修改版有一个小的状态问题：

```text
语音播报失败
-> main 仍然把该 label 加进 announced_labels
-> 后面再次看到这个物品时不会再播
```

这对“先识别播报拿识别分”的策略不合适。

V2 改为：

```text
播报
-> 失败最多再试一次
-> 只有 speak_blocking() 真正返回成功，才记为已播报
-> 如果仍失败，后面再次看到该物品时还会再次尝试
```

这个修正仍然只在 `main.py` 中。

## 完整流程软件仿真

不是只做语法检查，而是使用项目现有的 localhost 模拟 AGV，让**真实 `navigation_adapter.py`** 参与通信，再用假的视觉/抓取模块模拟物品。

仿真场景：

```text
LM1 起点
-> LM2 识别 Cola
-> LM3 无物品
-> LM4 识别 Cup
-> LM5 识别 Shampoo
-> LM6 HOME

抓取优先级：
LM6 -> LM5 -> LM4 -> LM3 -> LM2

执行：
LM6 -> LM5 抓 Shampoo -> LM7 SCORE
LM7 -> LM4 抓 Cup      -> LM7 SCORE
LM7 -> LM2 抓 Cola     -> LM7 SCORE
LM7 -> LM6 HOME -> LM8 EXIT
```

结果：

- `FULL_COMPETITION_SIMULATION=PASS`
- 成功模拟送回：3 件
- 最终站点：LM8
- 共验证：13 条点位导航命令
- 扫描、HOME、近点优先抓取、SCORE、最终 EXIT 的完整调度链均跑通

## 原项目测试

执行：

```bash
python -m unittest discover -s tests -v
```

结果：

- 收集 33 项
- 已执行的软件测试全部通过
- 22 项视觉/机械臂相关测试因当前运行环境缺少完整机器人依赖而被原测试代码主动 skip
- 无失败

另外，全项目 `.py` 源码均通过 Python 编译检查。

## 当前仍不能在这里替代实机验证的部分

软件调度跑通 **不等于机器人现场一定能完整得分**。正式实机前还需要确认：

1. RoboShop 地图中确实存在 `START/SCAN/HOME/SCORE/EXIT` 对应点。当前 `main.py` 示例里的 `LM8` 是预留的真正门外离场点；如果地图没有 LM8，必须重新打点或改名称。
2. 所有扫描/抓取点的停靠朝向要让眼在手上的相机能看到目标，并让机械臂可达。
3. 当前 `place` 动作只是到 SCORE 后在机械臂当前运输姿态松爪。必须实测这个姿态释放后物品确实落在 1m×1m 得分区内；如果不行，就需要另做放置姿态，这已经属于机械臂动作而不是导航。
4. `9.15/config.yaml` 的工作空间目前仍需要你们根据实机安全范围确认。
5. 当前权重的已有验证记录显示模型是 12 类。最终比赛如果出现模型未覆盖的类别，导航流程仍会走，但视觉无法把那些类别识别出来。
6. 当前复检环境没有安装 `ultralytics`、`pyrealsense2`，也没有真实底盘、RealSense 和 RealMan 机械臂，因此不能在这里做真实相机、机械臂和 AGV 实机联调。

## 关于内嵌备用 ZIP

原外层包里还有一个：

```text
9.20_lu/robocup_embody_0919.zip
```

第一次修改时，实际运行目录中的 `main.py` 和 `navigation_adapter.py` 已更新，但这个**内嵌备用 ZIP 里的同名文件仍然是旧版本**。

V2 已经把内嵌 ZIP 里的 `main.py` 和 `navigation_adapter.py` 同步成相同版本，避免你们以后误解压备用包时又拿到旧代码。

**外层和内层的目录结构都没有改变。**

## 当前建议的正式测试顺序

```text
1. python main.py
   只看比赛路线预览

2. python check_environment.py
   检查依赖/配置（注意这个脚本本身仍保留旧 LM1-LM7 检查文字）

3. 在 RoboShop 确认所有比赛 LM 点都存在

4. python main.py --execute --navigation-only
   只跑第一圈扫描路线，机械臂/视觉不动

5. 单独验证一个扫描点：停稳 -> 观测位 -> 检测 -> 播报 -> 收拢

6. 单独验证一件：
   抓取点 -> 抓住 -> 收拢 -> SCORE -> 松爪

7. 最后再执行：
   python main.py --execute --confirm-calibration
```

因此，**从代码架构上看，比赛策略不需要去改 9.16 SDK；修改 `main.py` + `navigation_adapter.py` 是正确的边界。**
V2 已完成完整软件仿真和包内副本同步，但正式上场前仍必须做底盘、相机、机械臂的实机串联测试。
