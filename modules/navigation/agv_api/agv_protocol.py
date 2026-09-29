"""
仙知 AGV 通用通信工具模块
agv_protocol.py

提供：
  - AGVPort              各功能模块端口常量
  - AGVConfig            连接配置（host + port + timeout）
  - build_frame()        构建请求帧
  - recv_full_frame()    完整接收响应帧（自动处理分包）
  - parse_frame()        解析响应帧 → 结构化 dict
  - send_and_recv()      一次性完成：连接→发送→接收→解析→断开
  - AGVSession           长连接会话类，适合连续发多条指令

帧结构（字节偏移）：
  [0:4]   帧头         固定 5A 01 00 01
  [4:8]   数据区长度   4字节大端整数
  [8:10]  命令 ID      2字节
  [10:16] 保留位       6字节（响应帧中前2字节通常回显请求命令ID）
  [16:]   数据区       JSON UTF-8 字节流
"""

from socket import socket, AF_INET, SOCK_STREAM, SOL_SOCKET, SO_KEEPALIVE
from dataclasses import dataclass
from typing import Union, Optional
import json
import uuid

from common.config import AGV_HOST


# ═════════════════════════════════════════════
#  端口常量
# ═════════════════════════════════════════════

class AGVPort:
    """AGV 各功能模块端口常量 (仙知协议)。

    与 AGVApi 门面 (agv_api.py) 中硬编码的端口保持一致:
      - STATUS  19204  状态查询 (机器人信息/位姿/任务状态)
      - CONTROL 19205  运动控制 (速度/停止)
      - NAV     19206  导航指令 (位点/自由导航/平动/转动/圆弧)
      - CONFIG  19207  地图/推送配置
      - OTHER   19210  其它扩展
      - PUSH    19301  AGV 主动推送订阅
    """
    STATUS = 19204
    CONTROL = 19205
    NAV = 19206
    CONFIG = 19207
    OTHER = 19210
    PUSH = 19301


# ═════════════════════════════════════════════
#  连接配置
# ═════════════════════════════════════════════

@dataclass
class AGVConfig:
    """
    AGV 连接配置，封装 host + port + timeout

    示例：
        cfg_nav    = AGVConfig(port=AGVPort.NAV)      # 导航端口
        cfg_status = AGVConfig(port=AGVPort.STATUS)   # 状态查询端口
        cfg_custom = AGVConfig(host="192.168.1.100", port=19210)  # 自定义
    """
    host:    str   = AGV_HOST
    port:    int   = 19204
    timeout: float = 5.0



# ═════════════════════════════════════════════
#  一、构建请求帧
# ═════════════════════════════════════════════

# 帧头固定值
FRAME_HEADER = bytes.fromhex("5A010001")

def build_frame(cmd_id: str, data: Optional[dict] = None) -> bytes:
    """
    构建完整协议帧

    :param cmd_id: 命令ID，4位十六进制字符串，如 "03E8"
    :param data:   请求数据（dict），无数据传 None
    :return:       完整帧 bytes

    示例：
        frame = build_frame("03E8")                                    # 无数据区
        frame = build_frame("0BEB", {"id": "B2", "source_id": "A1"})  # 有数据区
    """
    json_bytes = (
        json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if data is not None else b''
    )
    length_bytes  = len(json_bytes).to_bytes(4, byteorder='big')  # 数据区长度（4字节大端）
    cmd_bytes     = bytes.fromhex(cmd_id.zfill(4))                 # 命令ID（2字节）
    reserved      = bytes(6)                                        # 保留位（6字节全零）
    return FRAME_HEADER + length_bytes + cmd_bytes + reserved + json_bytes


# ═════════════════════════════════════════════
#  二、接收完整响应帧
# ═════════════════════════════════════════════

def recv_full_frame(sock: socket) -> bytes:
    """
    完整接收一帧响应，自动处理 TCP 分包问题

    先读 16 字节固定帧头，从中解析数据区长度，再读取剩余数据。
    无论响应多大（如 4380 字节），都能完整接收。

    :param sock: 已连接的 socket
    :return:     完整帧 bytes
    :raises ConnectionError: 连接断开时抛出
    """
    header = _recv_exact(sock, 16)
    data_length = int.from_bytes(header[4:8], byteorder='big')
    body = _recv_exact(sock, data_length) if data_length > 0 else b''
    return header + body


def _recv_exact(sock: socket, n: int) -> bytes:
    """循环读取，保证恰好读满 n 字节（recv_into + memoryview 零拷贝）"""
    buf = bytearray(n)
    view = memoryview(buf)
    pos = 0
    while pos < n:
        nbytes = sock.recv_into(view[pos:])
        if not nbytes:
            raise ConnectionError("AGV 连接已断开")
        pos += nbytes
    return bytes(buf)


# ═════════════════════════════════════════════
#  三、解析响应帧
# ═════════════════════════════════════════════

def parse_frame(raw: bytes) -> dict:
    """
    解析响应帧，返回结构化结果

    :param raw: recv_full_frame() 返回的完整帧
    :return: {
        "cmd_id":   str,   响应命令ID（大写十六进制，如 "2AF8"）
        "cmd_dec":  int,   响应命令ID（十进制，便于对照文档）
        "data_len": int,   数据区字节数
        "reserved": str,   保留位十六进制（6字节）
        "echo_cmd": str,   保留位前2字节（通常回显请求命令ID，如 "03E8"）
        "data":     dict,  解析后的 JSON 数据，无数据则为 {}
        "raw_data": str,   原始数据区字符串（调试用）
    }
    """
    if len(raw) < 16:
        raise ValueError(f"帧长度不足：{len(raw)} 字节，至少需要 16 字节")

    data_length = int.from_bytes(raw[4:8],  byteorder='big')
    cmd_id      = raw[8:10].hex().upper()
    reserved    = raw[10:16].hex().upper()
    echo_cmd    = raw[10:12].hex().upper()   # 保留位前2字节，通常回显请求命令ID
    body        = raw[16:]

    data = {}
    raw_data = ""
    if body:
        raw_data = body.decode('utf-8', errors='replace')
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError as e:
            raise ValueError(f"数据区 JSON 解析失败: {e}\n原始内容(前200字符): {raw_data[:200]}")

    return {
        "cmd_id":   cmd_id,
        "cmd_dec":  int(cmd_id, 16),
        "data_len": data_length,
        "reserved": reserved,
        "echo_cmd": echo_cmd,
        "data":     data,
        "raw_data": raw_data,
    }


# ═════════════════════════════════════════════
#  四、一次性请求（短连接）
# ═════════════════════════════════════════════

def send_and_recv(
    cmd_id:  str,
    data:    Optional[dict] = None,
    config:  AGVConfig   = None,
    # 也支持直接传参（兼容旧写法）
    host:    str   = None,
    port:    int   = None,
    timeout: float = None,
) -> dict:
    """
    短连接：连接 → 发送 → 接收 → 解析 → 断开，一步完成

    :param cmd_id:  命令ID，如 "03E8"
    :param data:    请求数据（dict），无数据传 None
    :param config:  AGVConfig 连接配置（推荐），如 CFG_NAV / CFG_STATUS
    :param host:    AGV IP（直接传参，优先级低于 config）
    :param port:    AGV 端口（直接传参，优先级低于 config）
    :param timeout: 超时秒数（直接传参，优先级低于 config）

    示例：
        # 推荐写法：使用预定义配置
        result = send_and_recv("03E8", config=CFG_STATUS)
        result = send_and_recv("0BEB", data={...}, config=CFG_NAV)

        # 临时指定端口
        result = send_and_recv("03E8", port=19204)

        # 完全自定义
        result = send_and_recv("03E8", host="192.168.1.100", port=19210, timeout=3.0)
    """
    cfg = config or AGVConfig(
        host    = host,
        port    = port    or AGVPort.NAV,
        timeout = timeout or 5.0,
    )
    sock = socket(AF_INET, SOCK_STREAM)
    sock.setsockopt(SOL_SOCKET, SO_KEEPALIVE, 1)
    sock.settimeout(cfg.timeout)
    try:
        sock.connect((cfg.host, cfg.port))
        sock.send(build_frame(cmd_id, data))
        raw = recv_full_frame(sock)
        return parse_frame(raw)
    finally:
        sock.close()


# ═════════════════════════════════════════════
#  五、长连接会话类
# ═════════════════════════════════════════════

class AGVSession:
    """
    长连接会话，适合连续发送多条指令的场景

    同一个 AGVSession 实例只连接一个端口。
    如需同时操作多个端口，创建多个 AGVSession 实例即可。

    示例：
        # 单端口
        with AGVSession(CFG_NAV) as agv:
            agv.request("0BEB", {"source_id": "A1", "id": "B2", ...})
            agv.request("0BB9")   # 暂停

        # 多端口并用
        with AGVSession(CFG_STATUS) as status:
            with AGVSession(CFG_NAV) as nav:
                info = status.request("03E8")
                nav.request("0BEB", {"source_id": "A1", "id": "B2", ...})
    """

    def __init__(self, config: AGVConfig = None, host=None, port=None, timeout=None):
        """
        :param config: AGVConfig（推荐），如 CFG_NAV / CFG_STATUS
        :param host:   直接传参（兼容旧写法）
        :param port:   直接传参（兼容旧写法）
        """
        self._cfg = config or AGVConfig(
            host    = host,
            port    = port    or AGVPort.NAV,
            timeout = timeout or 5.0,
        )
        self._sock = None

    def connect(self):
        self._sock = socket(AF_INET, SOCK_STREAM)
        self._sock.setsockopt(SOL_SOCKET, SO_KEEPALIVE, 1)
        self._sock.settimeout(self._cfg.timeout)
        self._sock.connect((self._cfg.host, self._cfg.port))
        print(f"[AGV] 已连接 {self._cfg.host}:{self._cfg.port}")

    def disconnect(self):
        if self._sock:
            self._sock.close()
            self._sock = None
            print(f"[AGV] 已断开 {self._cfg.host}:{self._cfg.port}")

    def request(self, cmd_id: str, data: Optional[dict] = None) -> dict:
        """
        发送一条指令并返回解析后的响应

        :param cmd_id: 命令ID，如 "03E8"
        :param data:   请求数据（dict），无数据传 None
        :return:       parse_frame() 的返回值
        """
        self._sock.send(build_frame(cmd_id, data))
        return parse_frame(recv_full_frame(self._sock))

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()