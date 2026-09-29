# asr_server_sensesmall.py
from fastapi import FastAPI, File, Form, UploadFile
import uvicorn
from funasr import AutoModel
import os
from pathlib import Path
import sys

# 将项目根目录添加到 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from common.config import LANGUAGE, COMMON_DRINKS
from common.port_guard import ensure_port_free

# 设置模型缓存目录
CACHE_DIR = PROJECT_ROOT / "models" / "modelscope_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
os.environ["MODELSCOPE_CACHE"] = str(CACHE_DIR)

print(f"项目根目录: {PROJECT_ROOT}")
print(f"模型缓存目录: {CACHE_DIR}")
print(f"当前识别语言: {LANGUAGE}")

# 根据语言选择热词（英文或中文）
if LANGUAGE == "en":
    # 饮料热词
    drink_hotwords = " ".join(COMMON_DRINKS)
    # 名字热词（对难识别的名字增加权重：重复出现多次）
    name_hotwords = (
        "jack john richard richard richard allen mike "
        "grace linda lily lucy jennier jennier jennier"
    )
    HOTWORDS = drink_hotwords + " " + name_hotwords
else:
    # 中文饮料 + 主人姓名热词（2026-09 语音切中文；名字按比赛名单增删）
    drink_hotwords = " ".join(COMMON_DRINKS)
    name_hotwords = (
        "我叫 我是 我的名字是 名字是 "              # 引导语，帮助 ASR 切出人名
        "张三 张伟 王芳 李娜 李四 王五 刘洋 陈静 杨帆 赵敏"
    )
    HOTWORDS = drink_hotwords + " " + name_hotwords

# 允许客户端（task_home.speech_service）追加热词，避免改了名单却动不了服务端。
MAX_EXTRA_HOTWORD_CHARS = 200


def merge_hotwords(extra: str = "") -> str:
    """把客户端热词规范化后并到内置热词之后；为空时行为与旧版完全一致。"""
    extra = " ".join(str(extra or "").split())[:MAX_EXTRA_HOTWORD_CHARS]
    if not extra:
        return HOTWORDS
    print(f"[ASR] 追加客户端热词: {extra}")
    return f"{HOTWORDS} {extra}"


print(f"热词列表: {HOTWORDS}")

# 启动前释放被旧服务占用的端口，避免 uvicorn 因 Address already in use 退出
if __name__ == "__main__":
    ensure_port_free(8001, "ASR")

# 使用本地模型缓存，避免 FunASR 将裸名称解释成 ModelScope repo_id
MODEL_DIR = CACHE_DIR / "models" / "iic" / "SenseVoiceSmall"
if not MODEL_DIR.exists():
    raise RuntimeError(f"ASR 模型目录不存在: {MODEL_DIR}")

# 加载SenseVoiceSmall模型
print(f"正在加载SenseVoiceSmall模型: {MODEL_DIR} ...")
model = AutoModel(
    model=str(MODEL_DIR),
    trust_remote_code=True,
    vad_model="fsmn-vad",
    device="cuda",   # 如果有GPU则使用，否则改为 "cpu"
    disable_update=True,
)
print("模型加载完成！")

app = FastAPI()

@app.post("/api/speech_recognition")
async def speech_recognition(audio: UploadFile = File(...), hotword: str = Form("")):
    """
    语音识别接口，支持热词增强和置信度过滤。

    ``hotword`` 为可选的客户端追加热词（空格分隔），与内置 HOTWORDS 合并使用；
    不传时与旧版行为一致。
    """
    audio_data = await audio.read()
    
    # 根据 LANGUAGE 设置识别语言
    lang = "en" if LANGUAGE == "en" else "zh"
    
    # 调用模型，加入热词（部分版本支持 hotwords_weight）
    res = model.generate(
        input=audio_data,
        cache={},
        language=lang,
        use_itn=True,
        hotwords=merge_hotwords(hotword),   # 内置热词 + 客户端追加热词
        # hotwords_weight=2.0,       # 可选：热词权重（如果模型支持，可取消注释）
    )
    
    if not res:
        return {"code": 200, "text": "识别失败"}
    
    text = res[0].get("text", "").strip()
    # 去除语言标记
    text = text.replace("<|EN|>", "").replace("<|ZH|>", "").strip()
    
    return {"code": 200, "text": text}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)