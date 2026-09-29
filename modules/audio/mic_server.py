#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦克风 HTTP 服务：录音 -> FunASR -> 姓名提取。

必须在安装了 pyaudio/webrtcvad 的语音环境运行。它与 ASR 服务分端口，避免
8001 端口冲突。一次只允许一个录音请求访问麦克风。

客户端（task_home.speech_service）可按需下发 ``hotword`` 热词，本服务随录音
一并转发给 8001 ASR 服务做热词增强；不传时行为与旧版一致。
"""
from __future__ import annotations

import os
import threading
import time
import wave
from typing import Optional

import uvicorn
from fastapi import FastAPI, Query
from pydantic import BaseModel

from common.port_guard import ensure_port_free

MIC_PORT = int(os.getenv("MIC_PORT", "8003"))
if __name__ == "__main__":
    ensure_port_free(MIC_PORT, "MIC")

from modules.audio import voice_assiant as voice_module


if os.getenv("INPUT_DEVICE_INDEX") not in (None, ""):
    voice_module.INPUT_DEVICE_INDEX = int(os.environ["INPUT_DEVICE_INDEX"])
extract_name = voice_module.extract_name
voice_assistant = voice_module.voice_assistant


_MIC_LOCK = threading.Lock()

# 诊断留档目录（可选）：设了就把每次录音写成 wav（文件名带 status），现场可直接
# 播回放核对"到底有没有人声"。不设则不留盘，避免长时间运行占满磁盘。
#   export MIC_DEBUG_WAV_DIR=~/桌面/logs/recordings
MIC_DEBUG_WAV_DIR = os.getenv("MIC_DEBUG_WAV_DIR", "").strip()

app = FastAPI(title="Robot microphone service", version="1.0")


class ListenResult(BaseModel):
    name: Optional[str] = None
    text: str = ""
    status: str


def _audio_stats(frames) -> tuple[int, float]:
    """估算录音字节数与时长(秒)；非 bytes 帧计 0（仅供诊断日志，不做判定）。"""
    total = 0
    for frame in frames or ():
        if isinstance(frame, (bytes, bytearray)):
            total += len(frame)
    rate = int(getattr(voice_module, "RATE", 16000) or 16000)
    return total, (total / (2 * rate) if total else 0.0)     # 16bit 单声道


def _dump_debug_wav(frames, status: str) -> None:
    """把本次录音留档（仅在 MIC_DEBUG_WAV_DIR 非空时），失败不影响主流程。"""
    if not MIC_DEBUG_WAV_DIR:
        return
    pcm = b"".join(f for f in (frames or ())
                   if isinstance(f, (bytes, bytearray)))
    if not pcm:
        return
    try:
        os.makedirs(MIC_DEBUG_WAV_DIR, exist_ok=True)
        rate = int(getattr(voice_module, "RATE", 16000) or 16000)
        stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}"
        path = os.path.join(MIC_DEBUG_WAV_DIR, f"mic_{stamp}_{status}.wav")
        with wave.open(path, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(pcm)
        print(f"[MIC] 录音留档: {path}")
    except Exception as exc:      # 留档只是诊断手段，绝不拖垮语音链路
        print(f"[MIC] 录音留档失败（主流程继续）: {exc}")


def _listen_once(hotword: str = "") -> tuple[str, str]:
    """独占麦克风录制一次，返回 (识别文本, 状态)。

    关闭录音设备本身也可能抛异常（USB 麦克风被拔、PortAudio 出错），所以锁必须
    放在最内层 finally 释放：否则一次异常会让后续请求永久返回 busy。

    每次录音都会打印一行 ``[MIC] diag ...``（帧数/秒数/识别文本），这是排查
    "到底没听到声音、还是听到了但没提取出名字"最直接的证据：

    * ``status=no_audio frames=0``        → 没听到人声（未开口 / VAD+能量未触发）；
    * ``status=no_speech ... text=''``    → 录到了声音但 ASR 没转出文字（噪声大、
      距离远、麦增益太低）；
    * ``status=ok ... text='今天天气好'`` → 听到了且转出了文字（若提取不出姓名，
      是姓名提取环节的问题，见 ``listen_owner_name`` 的 ``no_name`` 提示）。
    """
    hotword = " ".join(str(hotword or "").split())
    if not _MIC_LOCK.acquire(blocking=False):
        return "", "busy"
    try:
        voice_assistant.set_recording(1)
        frames = voice_assistant.record()
        if not frames:
            print("[MIC] diag status=no_audio frames=0 seconds=0.00 "
                  "→ 没听到人声（主人未开口，或 VAD+能量双门限未触发）")
            return "", "no_audio"
        text = str(voice_assistant.recognize(frames, hotwords=hotword) or "").strip()
        status = "ok" if text else "no_speech"
        _, seconds = _audio_stats(frames)
        print(f"[MIC] diag status={status} frames={len(frames)} "
              f"seconds={seconds:.2f} text={text!r}")
        _dump_debug_wav(frames, status)
        return (text, status)
    except Exception as exc:
        print(f"[MIC] 录音或识别失败: {exc}")
        return "", f"error:{type(exc).__name__}"
    finally:
        try:
            voice_assistant.set_recording(0)
        except Exception as exc:
            print(f"[MIC] 关闭录音设备失败（锁仍会释放）: {exc}")
        finally:
            _MIC_LOCK.release()


@app.get("/health")
def health():
    return {"status": "ok", "service": "microphone", "port": MIC_PORT}


@app.post("/api/listen_owner_name", response_model=ListenResult)
def listen_owner_name(
    timeout: float = Query(10.0, ge=1.0, le=30.0),
    hotword: Optional[str] = Query(None, max_length=200),
):
    """录制并按姓名提取；hotword 为客户端下发的空格分隔热词。

    voice_assiant.record() 自带 VAD 和最大录制时长；timeout 只用于 API 契约与
    客户端超时预算，旧 VoiceAssistant 的最大时长由其常量控制。
    """
    del timeout
    text, status = _listen_once(hotword or "")
    if status != "ok":
        return ListenResult(text=text, status=status)
    name = extract_name(text)
    if not name:
        print(f"[MIC] diag status=no_name text={text!r} "
              f"→ 听到了声音但没提取出姓名（只认中文 2~4 字，"
              f"外文名/数字/口头语提取不到）")
    return ListenResult(name=name, text=text, status="ok" if name else "no_name")


@app.post("/api/listen_text", response_model=ListenResult)
def listen_text(
    timeout: float = Query(10.0, ge=1.0, le=30.0),
    hotword: Optional[str] = Query(None, max_length=200),
):
    """录制并返回任意中文指令，不做姓名闭集过滤。"""
    del timeout
    text, status = _listen_once(hotword or "")
    return ListenResult(text=text, status=status)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=MIC_PORT)
