# import pyrealsense2 as rs
# import cv2
# import numpy as np
# import time
# from config import CAMERA_CHEST, CAMERA_HEAD, CAMERA_LEFT

# SERIAL = CAMERA_LEFT

# # 列出所有设备信息
# ctx = rs.context()
# print("已连接的 RealSense 设备:")
# for dev in ctx.devices:
#     sn = dev.get_info(rs.camera_info.serial_number)
#     name = dev.get_info(rs.camera_info.name)
#     usb = dev.get_info(rs.camera_info.usb_type_descriptor) if dev.supports(rs.camera_info.usb_type_descriptor) else "未知"
#     fw = dev.get_info(rs.camera_info.firmware_version) if dev.supports(rs.camera_info.firmware_version) else "未知"
#     match = " ← 目标" if sn == SERIAL else ""
#     print(f"  {sn}  {name}  USB {usb}  固件 {fw}{match}")

# # 先 hardware reset 再启动
# print("\n正在 reset 相机...")
# for dev in ctx.devices:
#     if dev.get_info(rs.camera_info.serial_number) == SERIAL:
#         dev.hardware_reset()
#         break
# time.sleep(3)  # reset 后等待重新枚举

# # ── 测试1: 只开深度流 ──
# print("\n测试1: 只开深度流...")
# pipeline = rs.pipeline()
# config = rs.config()
# config.enable_device(SERIAL)
# config.enable_stream(rs.stream.depth, 424, 240, rs.format.z16, 15)

# try:
#     profile = pipeline.start(config)
#     frames = pipeline.wait_for_frames(timeout_ms=5000)
#     print(f"  深度流 OK, 帧号: {frames.get_depth_frame().get_frame_number()}")
#     pipeline.stop()
# except Exception as e:
#     print(f"  深度流失败: {e}")
#     try: pipeline.stop()
#     except: pass
#     print("\n相机硬件可能有问题，尝试拔插 USB 线后重试")
#     exit(1)

# time.sleep(1)

# # ── 测试2: 深度+彩色 ──
# print("测试2: 深度+彩色...")
# pipeline = rs.pipeline()
# config = rs.config()
# config.enable_device(SERIAL)
# config.enable_stream(rs.stream.color, 424, 240, rs.format.bgr8, 15)
# config.enable_stream(rs.stream.depth, 424, 240, rs.format.z16, 15)

# try:
#     profile = pipeline.start(config)
#     print(f"  双流启动成功，按 q 退出\n")
# except Exception as e:
#     print(f"  双流启动失败: {e}")
#     exit(1)

# try:
#     while True:
#         frames = pipeline.wait_for_frames(timeout_ms=5000)
#         color_frame = frames.get_color_frame()
#         depth_frame = frames.get_depth_frame()
#         if not color_frame or not depth_frame:
#             continue

#         color = np.asanyarray(color_frame.get_data())
#         depth = np.asanyarray(depth_frame.get_data())

#         depth_colormap = cv2.applyColorMap(
#             cv2.convertScaleAbs(depth, alpha=0.03), cv2.COLORMAP_JET
#         )

#         combined = np.hstack((color, depth_colormap))
#         cv2.imshow("Color | Depth", combined)

#         if cv2.waitKey(1) & 0xFF == ord('q'):
#             break
# except RuntimeError as e:
#     print(f"取帧失败: {e}")
# finally:
#     pipeline.stop()
#     cv2.destroyAllWindows()

import cv2
import numpy as np
from camera_manager import camera_manager
from common.config import CAMERA_LEFT, CAMERA_HEAD

try:
    cam = camera_manager.start(CAMERA_HEAD)
    print("相机已启动，按 q 退出")
    while True:
        color, depth = cam.get_frames()
        if color is None or depth is None:
            continue
        depth_colormap = cv2.applyColorMap(
            cv2.convertScaleAbs(depth, alpha=0.03), cv2.COLORMAP_JET
        )
        cv2.imshow("Color | Depth", np.hstack((color, depth_colormap)))
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
except Exception as e:
    print(e)
finally:
    camera_manager.stop_all()
    cv2.destroyAllWindows()
