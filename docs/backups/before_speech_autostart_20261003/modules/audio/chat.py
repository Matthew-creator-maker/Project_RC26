"""V2 任务播报使用的 TTS 服务候选版，保持现有 HTTP 协议与 8002 端口。

启动命令（项目根目录）：python -m modules.audio.chat
需要现有环境中的 edge_tts、fastapi、uvicorn。不会安装任何依赖。
合成需要 Edge TTS 服务可用；已有有效缓存则直接返回缓存。
本模块不导入对话助手、麦克风、ASR 或 VAD，也不会主动终止占用端口的
其他进程。端口占用时 uvicorn 会正常报错，交由使用者检查。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# 兼容旧习惯：python modules/audio/chat.py。
# chat.py 的 parents[2] 才是 robocup_embody 项目根目录。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import edge_tts
import uvicorn
from fastapi import Body, FastAPI, HTTPException, Response

from modules.audio.speech_utils import (
    MAX_AUDIO_BYTES,
    cache_filename,
    is_probably_mp3,
    read_valid_mp3,
    validate_speed,
    write_bytes_atomic,
)


VOICE_DEFAULT = "zh-CN-XiaoxiaoNeural"
AUDIO_DIR = Path(__file__).resolve().parent / "audio_cache" / "tts_service"
app = FastAPI(title="RoboCup 物品中文播报 TTS")
_cache_locks: dict[str, asyncio.Lock] = {}


def speed_to_rate(speed: float) -> str:
    """把 1.2 转换成 Edge TTS 接受的 '+20%'。"""
    percent = round((validate_speed(speed) - 1.0) * 100)
    return f"{percent:+d}%"


@app.post("/v1/audio/speech")
async def speech(
    model: str = Body("tts-1", embed=True),
    input: str = Body(..., embed=True, min_length=1, max_length=500),
    voice: str = Body(VOICE_DEFAULT, embed=True, min_length=1, max_length=100),
    speed: float = Body(1.0, embed=True, ge=0.5, le=2.0),
) -> Response:
    """接收与原接口一致的 JSON 请求，返回完整 MP3 数据。

    model 字段为了兼容已有调用而保留；实际合成使用 edge_tts。
    相同缓存键共用一把异步锁，避免同时请求把同一个缓存写坏。
    客户端与本服务使用 speech_utils.py 中同一个缓存键算法；内容、音色、
    语速任意一项改变，都会合成对应的新文件，避免复用旧音色。
    """
    if not input.strip() or not voice.strip():
        raise HTTPException(status_code=422, detail="input 和 voice 不能为空白")
    filename = cache_filename(input, voice, speed)
    path = AUDIO_DIR / filename
    lock = _cache_locks.setdefault(filename, asyncio.Lock())
    async with lock:
        cached = read_valid_mp3(path)
        if cached is not None:
            return Response(content=cached, media_type="audio/mpeg")

        # 导入模块时不创建目录；只有明确的合成请求才准备文件。
        temporary_path: Path | None = None
        try:
            AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".tts-", suffix=".mp3", dir=AUDIO_DIR
            )
            os.close(descriptor)
            temporary_path = Path(temporary_name)
            communicate = edge_tts.Communicate(input, voice, rate=speed_to_rate(speed))
            await communicate.save(str(temporary_path))
            if temporary_path.stat().st_size > MAX_AUDIO_BYTES:
                raise ValueError("合成音频超过允许大小")
            audio = temporary_path.read_bytes()
            if not is_probably_mp3(audio):
                raise ValueError("合成服务返回的内容不是有效的 MP3 开头")
            write_bytes_atomic(path, audio)
            return Response(content=audio, media_type="audio/mpeg")
        except Exception as exc:
            print(f"[TTS 合成失败] {exc}", flush=True)
            raise HTTPException(status_code=502, detail="TTS 合成失败，请查看服务日志") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
