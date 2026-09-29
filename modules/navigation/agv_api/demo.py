'''
底盘通讯的使用demo
'''
from __future__ import annotations
import os
import sys
import math

# 获取当前脚本的绝对路径 (26-home/task3/arm_folding/tool/slide_locate.py)
current_dir = os.path.dirname(os.path.abspath(__file__))

# 向上跳三级回到真正的根目录 (26-home)
# tool -> arm_folding -> task3 -> 26-home
root_dir = os.path.abspath(os.path.join(current_dir, "../../.."))

if root_dir not in sys.path:
    sys.path.insert(0, root_dir)
    print(f"已修正项目根目录为: {root_dir}")
import time
from modules.navigation.agv_api import agv, wait_nav

# ── Demo ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":


    agv.start()
    
    w = 0.0
    
    for i in range(13):
        agv.send_velocity(0.0, 0.0, w, duration=0)
        print(f"速度: {w:.2f} rad/s")
        time.sleep(0.1)
        w += 0.15

    time.sleep(2)
        
    for i in range(13):
        w -= 0.15
        agv.send_velocity(0.0, 0.0, w, duration=0)
        print(f"速度: {w:.2f} rad/s")
        time.sleep(0.1)
        
    speed = 0.0
    
    for i in range(13):
        agv.send_velocity(speed, 0.0, duration=0)
        print(f"速度: {speed:.2f} m/s")
        time.sleep(0.1)
        speed += 0.07

    time.sleep(2)
        
    for i in range(13):
        agv.send_velocity(speed, 0.0, duration=0)
        print(f"速度: {speed:.2f} m/s")
        time.sleep(0.1)
        speed -= 0.07
        


    # # print(agv.navigate_to("", "LM1"))
    # print(agv.free_navigate_to(0, 0, 0))
    # print(agv.get_alarm())
    # time.sleep(3)
    # wait_nav()

    agv.stop()
