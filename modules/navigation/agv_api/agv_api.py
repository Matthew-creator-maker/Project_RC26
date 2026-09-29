"""
AGV 常用 API 封装
方法参考
https://seer-group.feishu.cn/wiki/X0LSw4NRRiXTtGksoMxcQ4knnxh


单个门面类封装所有常用指令，隐藏端口号和 cmd_id 细节。
调用方只需关心业务语义：agv.get_pose()、agv.send_velocity(...)

底层通过 AGVManager 的队列机制通信，线程安全。
"""
from __future__ import annotations
import time
from typing import Optional, Union
from .agv_manager import AGVManager, Result

# ── 端口常量（内部使用，调用方无需关心）─────────────────────────────────────
_STATUS = 19204
_CONTROL = 19205
_NAV = 19206
_CONFIG = 19207
_OTHER = 19210
_PUSH = 19301


class AGVApi:
    """
    AGV 常用 API 的高层封装

    用法：
        from agv_api import agv_api
        agv_api.start()

        pose = agv_api.get_pose()
        agv_api.send_velocity(0.3, 0.0)
        agv_api.navigate_to(1.0, 2.0, 1.57)

        agv_api.stop()
    """

    def __init__(self):
        self._mgr = AGVManager()

    def start(self, timeout: float = 10.0) -> bool:
        return self._mgr.start(timeout=timeout)

    def stop(self):
        self._mgr.stop()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    # ── 内部工具 ──────────────────────────────────────────────────────────

    def _query(self, port: int, cmd_id: str, data: dict = None,
               timeout: float = 5.0) -> Optional[Result]:
        return self._mgr.query(port, cmd_id, data, timeout)

    def _send(self, port: int, cmd_id: str, data: dict = None):
        self._mgr.send(port, cmd_id, data)

    def _query_data(self, port: int, cmd_id: str, data: dict = None,
                    timeout: float = 5.0) -> Optional[dict]:
        """查询并直接返回 response["data"]，失败返回 None"""
        result = self._query(port, cmd_id, data, timeout)
        if result and result.ok:
            return result.response["data"]
        return None

    # ═════════════════════════════════════════════════════════════════════
    #  状态查询
    # ═════════════════════════════════════════════════════════════════════

    def get_robot_info(self) -> Optional[dict]:
        """查询机器人基本信息（型号、地图、固件等）"""
        return self._query_data(_STATUS, "03E8")

    def get_pose(self) -> Optional[dict]:
        """
        查询机器人位置

        返回: {"x", "y", "angle", "confidence", "current_station", ...}
        """
        return self._query_data(_STATUS, "03EC")

    def get_lidar(self) -> Optional[list]:
        """
        获取激光雷达点云数据

        返回: lasers 列表，每个元素包含 beams/device_info/install_info
        """
        data = self._query_data(_STATUS, "03F1")
        return data.get("lasers") if data else None

    def get_task_status(self, simple: bool = True) -> Optional[dict]:
        """
        查询当前导航任务状态

        0 = NONE, 

        1 = WAITING(目前不可能出现该状态), 

        2 = RUNNING, 

        3 = SUSPENDED, 

        4 = COMPLETED, 

        5 = FAILED, 
        
        6 = CANCELED
        """
        return self._query_data(_STATUS, "03FC", {"simple": simple})

    def get_area_info(self) -> Optional[dict]:
        """获取区域 ID 信息"""
        return self._query_data(_STATUS, "03F3")

    def get_slam_status(self) -> Optional[dict]:
        """查询 SLAM 状态"""
        return self._query_data(_STATUS, "0401")
    
    def get_current_station(self) -> str :
        """离机器人最近站点的 id（该判断比较严格，机器人必须很靠近某一个站点，
        这个距离可以通过参数配置中的 CurrentPointDist修改，默认为 0.3m。
        如果机器人与所有站点的距离大于这个距离，
        则这个字段为空字符。查询当前所在站点""" 
        data = self._query_data(_STATUS, "03EC")
        return data.get("current_station") if data else ""
    
    def get_alarm(self) -> Optional[dict]:
        """
        查询当前机器人的报警信息
        https://seer-group.feishu.cn/wiki/WrZFwfzDKinuVFkKrsxcbBVhnSg?from=space_search
        """
        return self._query_data(_STATUS, "041A")

    # ═════════════════════════════════════════════════════════════════════
    #  运动控制
    # ═════════════════════════════════════════════════════════════════════

    def send_velocity(self, vx: float, vy: float = 0.0, w: float = 0.0,
                      duration: int = 200) -> Optional[dict]:
        """
        发送速度指令（开环控制）

        :param vx:       前向速度 m/s，正前负后
        :param vy:       横向速度 m/s，差速底盘通常为 0
        :param w:        角速度 rad/s，逆时针为正
        :param duration: 看门狗时间 ms，超时自动停（0=不限）
        """
        return self._query_data(_CONTROL, "07DA", {
            "vx": vx, "vy": vy, "w": w, "duration": duration,
        })

    def stop_motion(self):
        """紧急停止（发送零速度）"""
        self.send_velocity(0.0, 0.0, 0.0)

    # ═════════════════════════════════════════════════════════════════════
    #  导航
    # ═════════════════════════════════════════════════════════════════════

    def free_navigate_to(self, x: float, y: float, theta: float) -> bool:
        """
        自由导航到目标点（全局路劲规划）

        角度是弧度制

        :return: True=指令发送成功（不代表已到达）
        """
        data = self._query_data(_NAV, "0BEB", {
            "freeGo": {"x": x, "y": y, "theta": theta},
            "id": "SELF_POSITION",
        })
        return data is not None and data.get("ret_code") == 0
    
    def navigate_to(self, source: str, target: str, angle: float = "") -> Optional[dict] :
        """
        导航到目标点。

        ``source`` 非空时按指定起点/终点下发固定路径导航；``source`` 为空时只
        下发目标 ``id``，让底盘从当前实际位置规划到目标点。后者用于阻塞回退，
        避免向控制器发送空字符串 ``source_id``。

        :param source:  导航起始点 id；空字符串表示从当前实际位置出发
        :param target:  导航目标点 id
        :param angle:   世界坐标中的姿态 rad，可缺省
        """
        payload = {"id": target}
        if source:
            payload["source_id"] = source
        if angle != "":
            payload["angle"] = angle
        return self._query_data(_NAV, "0BEB", payload)

    def move_straight(self, dist: float, vx: float, mode: int = 0):
        """
        平动：以固定速度直线运动固定距离

        :param dist: 运动距离 m（绝对值）
        :param vx:   速度 m/s，正前负后
        :param mode: 0=里程模式, 1=定位模式
        """
        self._send(_NAV, "0BEF", {"dist": dist, "vx": vx, "mode": mode})

    def rotate(self, angle: float, vw: float, mode: int = 0):
        """
        转动：以固定角速度旋转固定角度

        :param angle: 转动角度 rad（绝对值，可大于 2π）
        :param vw:    角速度 rad/s，正=逆时针，负=顺时针
        :param mode:  0=里程模式, 1=定位模式
        """
        self._send(_NAV, "0BF0", {"angle": angle, "vw": vw, "mode": mode})
        
    
    def move_circle(self, radius: float, degree: float, vw: float = 0.5, mode: int = 0):
        """
        圆弧运动：以固定角速度沿圆弧运动固定角度

        :param radius: 旋转半径, 单位m; 圆心在机器人坐标系左边为正, 右边为负
        :param degree:  旋转角度, 单位度
        :param vw:     旋转速度, 单位为 rad/s, 如果是负数, 则说明是顺时针走
        :param mode:   0=里程模式, 1=定位模式
        """
        self._send(_NAV, "0BF2", {"radius": radius, "angle": degree, "vw": vw, "mode": mode})

    def pause_navigation(self):
        """暂停当前导航"""
        self._send(_NAV, "0BB9")

    def cancel_navigation(self):
        """取消当前导航"""
        self._send(_NAV, "0BBB")

    # ═════════════════════════════════════════════════════════════════════
    #  配置
    # ═════════════════════════════════════════════════════════════════════

    def get_map_data(self, map_name: str) -> Optional[dict]:
        """获取指定地图的 JSON 数据"""
        return self._query_data(_CONFIG, "0FAB", {"map_name": map_name})

    # ═════════════════════════════════════════════════════════════════════
    #  推送
    # ═════════════════════════════════════════════════════════════════════

    def configure_push(self, interval: int = 500,
                       fields: list[str] = None) -> Optional[dict]:
        """
        配置机器人主动推送

        :param interval: 推送间隔 ms
        :param fields:   推送包含的字段列表
        """
        data = {"interval": interval}
        if fields is not None:
            data["included_fields"] = fields
        return self._query_data(_PUSH, "2454", data)

    def poll_push(self) -> Optional[Result]:
        """非阻塞取出一条 AGV 主动推送，无消息返回 None"""
        return self._mgr.poll()



# 模块级单例
agv = AGVApi()

def wait_nav(timeout: float = 30.0) -> bool:
    """轮询等待导航完成"""
    start = time.time()
    while time.time() - start < timeout:
        status = agv.get_task_status()
        if status is None:
            time.sleep(0.3)
            continue
        ts = status.get("task_status", "")
        if ts == 4:
            return True
        if ts == 5:
            print("导航失败")
            return False
        if ts == 6:
            print("导航被取消")
            return False
        time.sleep(0.5)
    print("导航超时")
    return False