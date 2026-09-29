# RoboCup Embodied Robot Project

本目录已经按**功能模块**重组，不再用 `9.15`、`9.16_lu`、`9.20_lu` 这类日期/版本目录承载运行代码。

## 目录

```text
robocup_embody/
├── app/                    # 比赛任务编排与主流程
├── modules/
│   ├── perception/         # 视觉识别、深度定位、手眼变换
│   ├── grasp/              # 抓取流程、机械臂抓取适配
│   ├── navigation/         # 导航适配、动态避障、AGV API
│   ├── hardware/           # 通用硬件：机械臂/相机/头部/滑轨
│   ├── audio/              # 语音模块
│   └── reid/               # ReID
├── common/                 # 全局配置、状态机、通用工具
├── config/                 # 任务配置与视觉配置
├── assets/models/          # YOLO 权重等运行资源
├── tools/                  # 环境检查与安装脚本
├── tests/                  # 自动化测试
├── docs/                   # 测试/复检文档与验证材料
└── archive/                # 不参与运行的旧备份
```

## 常用入口

- 预览比赛流程：`python run.py`
- 只测试导航：`python run.py --execute --navigation-only`
- 完整实机：`python run.py --execute --confirm-calibration`
- 环境检查：`python tools/check_environment.py`
- 单元测试：`python -m unittest discover -s tests -v`

## 配置位置

- 任务/导航参数：`config/task_config.json`
- 视觉/机械臂抓取参数：`config/perception.yaml`
- 模型权重：`assets/models/classes_12new.pt`

## 集成说明

- 主流程不再通过修改 `sys.path` 去动态加载某个日期目录。
- 抓取控制器通过 `modules.grasp.grasp_controller` 延迟加载，未安装视觉依赖时仍可运行导航相关测试。
- 导航只保留一份 AGV API，统一位于 `modules/navigation/agv_api/`。
- 滑轨当前代码位于 `modules/hardware/slide_control/`；历史 `.bak` 文件已移到 `archive/slide_control_backups/`。
- 旧语音副本不再与运行文件混放，已移入 `archive/audio_backups/`。

> 注意：目录整理和离线测试不能替代机器人实机标定与安全验证。赛前仍需确认手眼标定、机械臂工作空间、运输位、AGV 点位和硬件地址。
