# chat.py
import edge_tts
import uvicorn
from fastapi import FastAPI, Body, Response
import os
import hashlib
import shutil
import glob
import re
from datetime import datetime
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))
from common.config import LANGUAGE
from common.port_guard import ensure_port_free

# 安全文件名函数（必须与客户端完全一致）
def safe_filename(text: str, max_len=100) -> str:
    illegal_chars = r'[\\/*?:"<>|]'
    safe = re.sub(illegal_chars, '_', text)
    safe = safe.strip('. ')
    if len(safe) > max_len:
        safe = safe[:max_len]
    return safe + ".mp3"

app = FastAPI()

if LANGUAGE == "en":
    VOICE_DEFAULT = "en-US-JennyNeural"
else:
    VOICE_DEFAULT = "zh-CN-XiaoxiaoNeural"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "audio_cache")
CACHE_SUBDIR = os.path.join(AUDIO_DIR, "cache")
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(CACHE_SUBDIR, exist_ok=True)

def speed_to_rate(speed):
    percent = int((speed - 1.0) * 100)
    return f"{percent:+d}%"

@app.post("/v1/audio/speech")
async def speech(
    model: str = Body("tts-1", embed=True),
    input: str = Body(..., embed=True),
    voice: str = Body(VOICE_DEFAULT, embed=True),
    speed: float = Body(1.0, embed=True)
):
    # 主缓存路径（与客户端一致）
    cache_filename = safe_filename(input)
    cache_path = os.path.join(AUDIO_DIR, cache_filename)

    # 如果主缓存已存在，直接返回
    if os.path.exists(cache_path):
        return Response(content=open(cache_path, "rb").read(), media_type="audio/mpeg")

    # 生成原始备份文件名（时间戳+哈希，仅作备份）
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    text_hash = hashlib.md5(input.encode()).hexdigest()[:8]
    backup_filename = f"{timestamp}_{text_hash}.mp3"
    backup_path = os.path.join(CACHE_SUBDIR, backup_filename)

    # 调用 edge_tts 生成音频到备份路径
    communicate = edge_tts.Communicate(input, voice, rate=speed_to_rate(speed))
    try:
        await communicate.save(backup_path)
    except Exception as e:
        return Response(content=f"TTS failed: {e}", status_code=500)

    # 复制到主缓存路径
    shutil.copy2(backup_path, cache_path)

    # 清理旧备份（保留最近200个）
    try:
        backups = glob.glob(os.path.join(CACHE_SUBDIR, "*.mp3"))
        if len(backups) > 200:
            backups.sort(key=os.path.getmtime)
            for f in backups[:-200]:
                os.remove(f)
    except:
        pass

    return Response(content=open(cache_path, "rb").read(), media_type="audio/mpeg")

if __name__ == "__main__":
    ensure_port_free(8002, "TTS")
    uvicorn.run(app, host="0.0.0.0", port=8002)
