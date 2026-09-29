"""
RoboShop 点位导航测试

导航顺序：
LM1 -> LM2 -> LM3
"""

import time
# Support both:
#   1) VSCode "Run Python File" / python agv_api/roboshop_lm_navigation.py
#   2) python -m agv_api.roboshop_lm_navigation
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    _project_root = Path(__file__).resolve().parents[3]
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))
    from modules.navigation.agv_api import agv, wait_nav
else:
    from .agv_api import agv, wait_nav
POINTS = [
    "LM1",
    "LM2",
    "LM3",
]


def navigate(source, target):
    print("\n============================")
    print(f"开始导航: {source} -> {target}")

    result = agv.navigate_to(
        source=source,
        target=target
    )

    print("导航指令返回:", result)

    if result is None:
        print("导航指令发送失败")
        return False

    print(f"正在等待机器人到达 {target} ...")

    success = wait_nav(timeout=120)

    if success:
        print(f"已成功到达 {target}")
    else:
        print(f"未成功到达 {target}")

    return success


if __name__ == "__main__":

    print("正在连接 AGV...")

    agv.start()

    try:
        for i in range(len(POINTS) - 1):

            source = POINTS[i]
            target = POINTS[i + 1]

            ok = navigate(source, target)

            if not ok:
                print("导航中止")
                break

            time.sleep(1)

    except KeyboardInterrupt:
        print("\n用户停止程序")

    finally:
        agv.stop()
        print("AGV 通讯已关闭")
