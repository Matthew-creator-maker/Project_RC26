# 工程目录重组说明

## 已完成的整理

本工程已从“按日期/版本堆目录”的方式改成“按功能模块分目录”的方式：

- `modules/perception/`：视觉识别、RealSense、深度定位、手眼变换、YOLO 检测流水线
- `modules/grasp/`：抓取主流程、RealMan 抓取适配、感知-抓取桥接
- `modules/navigation/`：导航适配、动态避障、统一 AGV API
- `modules/hardware/`：通用机械臂、相机、头部、滑轨硬件控制
- `modules/audio/`：语音模块
- `modules/reid/`：ReID
- `app/`：比赛主任务编排
- `config/`：任务配置与视觉/抓取配置
- `assets/models/`：模型权重
- `common/`：公共配置、状态机和工具
- `tools/`：环境检查与部署脚本
- `tests/`：测试
- `docs/`：历史测试报告、复检报告和验证材料
- `archive/`：旧备份，不参与运行

## 集成处理

1. 删除了运行代码对 `9.15`、`9.16_lu` 等日期目录的依赖。
2. 导航 AGV API 合并为一份，统一放在 `modules/navigation/agv_api/`。
3. 抓取控制器改为通过正式包路径延迟加载，避免主程序依赖 `sys.path` 注入。
4. 视觉模块改为包内相对导入。
5. `config/task_config.json` 不再保存 `navigation_dir` / `perception_dir` 版本目录。
6. YOLO 权重路径调整为 `assets/models/classes_12new.pt`。
7. 滑轨当前运行代码归入 `modules/hardware/slide_control/`，旧 `.bak` 文件移入 `archive/slide_control_backups/`。
8. 语音重复副本移入 `archive/audio_backups/`。
9. 新增根入口 `run.py`，避免用户进入深层目录运行。

## 离线验证结果

- Python 文件编译检查：67 个 `.py` 文件，0 个语法错误。
- 单元测试：共 33 项；11 项导航/任务控制测试通过；22 项因当前环境缺少完整视觉/RealSense/RealMan 运行依赖而跳过；0 项失败。
- `python run.py` 预览模式正常。
- `python tools/check_environment.py --help` 正常。
- 运行目录中无日期式文件夹名称。

> 说明：这些检查证明目录重组和软件层集成没有破坏已覆盖的导航/任务逻辑，但不能代替机器人实机验证。赛前仍需确认手眼标定、机械臂安全工作空间、运输位、AGV 点位及硬件地址。
