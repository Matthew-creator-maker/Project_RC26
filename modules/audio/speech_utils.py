"""任务播报的音频工具层：合成、播放与安全缓存集中在一个文件。

给 Python 初学者的分层说明：
1. speech.py 决定“什么时候说、说什么”，并负责异步播报队列。
2. 本文件负责“这句话如何变成 MP3、保存在哪里、如何播放”。
3. chat.py 提供原项目约定的本地 TTS HTTP 接口，复用本文件的缓存规则。

本文件不导入旧 voice_assiant.py，不初始化麦克风、ASR、VAD 或对话模型。
导入本文件、创建 ObjectVoice 都不会创建缓存目录、访问网络或发出声音。
只有明确调用 speak/cache_text 时，才会播放或准备音频。

运行环境沿用机器人现有的 TTS 服务（8002 端口）与 mpg123 播放器；
不安装依赖、不更改系统声卡、不终止其他进程。测试可替换合成和播放函数，
因此无需真实网络、扬声器或机器人即可检查逻辑。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile

import shutil
import subprocess
import threading
from typing import Callable
from urllib import request
from pathlib import Path


CACHE_VERSION = "object-speech-v1"
MAX_AUDIO_BYTES = 16 * 1024 * 1024


def validate_speed(speed: float) -> float:
    """检查并统一语速数值；1.0 是原速，1.2 表示提高 20%。"""
    value = float(speed)
    if not math.isfinite(value) or not 0.5 <= value <= 2.0:
        raise ValueError("speed 必须是 0.5 到 2.0 之间的有限数字")
    return value


def cache_filename(text: str, voice: str, speed: float) -> str:
    """返回安全文件名：内容、音色、语速或缓存版本变化都会改变它。"""
    parameters = {
        "version": CACHE_VERSION,
        "text": text,
        "voice": voice,
        "speed": validate_speed(speed),
    }
    encoded = json.dumps(
        parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest() + ".mp3"


def is_probably_mp3(audio: bytes) -> bool:
    """排除空文件及常见 HTML/JSON 错误响应，检查首个 MP3 帧头。

    这不是完整音频解码器。通过本检查只代表数据具有合理的 MP3 开头；
    最终是否能正常播放，仍然必须根据播放器的退出状态判断。
    MP3 可以直接以音频帧开始，也可以先包含一段 ID3 元数据。
    """
    if not isinstance(audio, bytes) or not 64 <= len(audio) <= MAX_AUDIO_BYTES:
        return False
    offset = 0
    if audio.startswith(b"ID3"):
        if audio[3] not in (2, 3, 4) or any(value & 0x80 for value in audio[6:10]):
            return False
        tag_size = (
            (audio[6] << 21) | (audio[7] << 14) | (audio[8] << 7) | audio[9]
        )
        offset = 10 + tag_size
        # ID3v2.4 的 footer 是额外的 10 字节，不属于音频帧。
        if audio[3] == 4 and audio[5] & 0x10:
            offset += 10
    if offset + 4 > len(audio):
        return False
    first, second, third, _ = audio[offset : offset + 4]
    # 检查 MPEG 同步字、版本、Layer III、比特率以及采样率。
    return (
        first == 0xFF
        and second & 0xE0 == 0xE0
        and (second >> 3) & 0x03 != 1
        and (second >> 1) & 0x03 == 1
        and 0 < (third >> 4) < 15
        and (third >> 2) & 0x03 != 3
    )


def read_valid_mp3(path: Path) -> bytes | None:
    """只读检查缓存；不存在、太大或格式不符时返回 None。"""
    try:
        if path.stat().st_size > MAX_AUDIO_BYTES:
            return None
        audio = path.read_bytes()
    except OSError:
        return None
    return audio if is_probably_mp3(audio) else None


def write_bytes_atomic(path: Path, audio: bytes) -> None:
    """先写临时文件，再一次性替换正式缓存，避免读到写了一半的 MP3。

    只有明确调用此函数才创建目录。临时文件与正式文件放在同一目录，
    这样 os.replace 才能利用同一文件系统上的原子替换操作。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".speech-", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(audio)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


# ---------------- 音频合成与播放 ----------------

DEFAULT_TTS_URL = "http://127.0.0.1:8002/v1/audio/speech"
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_SPEED = 1.2
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / "audio_cache" / "object_task"

# 所有 ObjectVoice 实例共用同一把锁：避免扫描和抓取的声音互相重叠。
# 此锁只串行音频操作，不阻塞机器人主线程中的导航或抓取。
# RLock 允许同一线程在内部复用方法时再次持锁，不会把自己锁住。
_SPEAK_LOCK = threading.RLock()
_default_voice: ObjectVoice | None = None


class ObjectVoice:
    """阻塞播报器：speak 返回后，本句的播放已经成功结束或失败。

    synthesizer 和 player 是测试用的可替换接口。未提供时使用真实 HTTP
    合成与 mpg123。合成函数接收 (text, voice, speed)，返回 MP3 字节；
    播放函数接收 Path，只有实际播放成功才返回 True。
    """

    def __init__(
        self,
        *,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        tts_url: str = DEFAULT_TTS_URL,
        voice: str = DEFAULT_VOICE,
        speed: float = DEFAULT_SPEED,
        synthesizer: Callable[[str, str, float], bytes] | None = None,
        player: Callable[[Path], bool] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.tts_url = tts_url
        self.voice = voice
        self.speed = validate_speed(speed)
        self._synthesizer = synthesizer or self._request_audio
        self._player = player or self._play_audio

    def _request_audio(self, text: str, voice: str, speed: float) -> bytes:
        """显式播报或预缓存时才调用；连接/读取超时参数为 8 秒。

        urllib 的 timeout 约束连接与读取等待，不保证整个 HTTP 请求
        从开始到结束的总时长一定不超过 8 秒。
        """
        payload = json.dumps(
            {"model": "tts-1", "input": text, "voice": voice, "speed": speed},
            ensure_ascii=False,
        ).encode("utf-8")
        req = request.Request(
            self.tts_url,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "audio/mpeg"},
            method="POST",
        )
        with request.urlopen(req, timeout=8) as response:
            if response.status != 200:
                raise RuntimeError(f"TTS HTTP 状态码为 {response.status}")
            # 限制读取大小，避免异常服务把大文件全部读入内存。
            return response.read(MAX_AUDIO_BYTES + 1)

    @staticmethod
    def _play_audio(path: Path) -> bool:
        """沿用项目 Linux 的 mpg123 -a default 播放方式。

        不会自动安装播放器，不会自行修改系统音频设备。
        最多等待播放器 15 秒，超时异常由 speak 捕获并返回 False。
        returncode == 0 是播放器确认成功退出的依据；这不能检测扬声器
        音量是否为零、扬声器电源是否接通或人是否实际听见声音。
        """
        executable = shutil.which("mpg123")
        if executable is None:
            print("[语音失败] 未找到 mpg123 播放器。", flush=True)
            return False
        result = subprocess.run(
            [executable, "-a", "default", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            print(f"[语音失败] mpg123 退出码：{result.returncode}", flush=True)
        return result.returncode == 0

    @staticmethod
    def _check_text(text: str) -> None:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("播报文字不能为空")

    def _prepare_cache(self, text: str) -> Path:
        """有有效缓存就复用，否则只合成一次并原子写入。

        这个内部方法不播放声音。调用方负责持有 _SPEAK_LOCK 并捕获异常。
        """
        path = self.cache_dir / cache_filename(text, self.voice, self.speed)
        if read_valid_mp3(path) is not None:
            return path
        audio = self._synthesizer(text, self.voice, self.speed)
        if not is_probably_mp3(audio):
            raise ValueError("TTS 返回的数据不是有效的 MP3 开头，拒绝写入缓存")
        write_bytes_atomic(path, audio)
        return path

    def cache_text(self, text: str) -> bool:
        """只准备一句话的缓存，不播放；适合赛前预缓存 12 句名称。

        True 只表示缓存准备成功，不代表扬声器已经播放过这句话。
        有有效缓存时不请求网络；无缓存且 TTS 服务不可用时返回 False。
        """
        with _SPEAK_LOCK:
            try:
                self._check_text(text)
                self._prepare_cache(text)
                print(f"[语音缓存] 已准备：{text}", flush=True)
                return True
            except Exception as exc:
                print(f"[语音缓存失败] {text!r}：{exc}", flush=True)
                return False

    def speak(self, text: str) -> bool:
        """阻塞播放一句话；合成或播放失败均返回 False，不假报成功。

        已有有效缓存时只尝试播放一次。播放失败可能来自扬声器或播放器，
        不能因此删除有效缓存或重新请求网络。由上层业务决定是否再次调用。
        只有缓存缺失或格式检查失败才合成一次，播放器失败直接返回 False。
        """
        with _SPEAK_LOCK:
            try:
                self._check_text(text)
                print(f"[语音播报] {text}", flush=True)
                path = self._prepare_cache(text)
                if self._player(path) is True:
                    return True
                print(f"[语音失败] 未完成播放：{text}", flush=True)
                return False
            except Exception as exc:
                print(f"[语音失败] {text!r}：{exc}", flush=True)
                return False


def speak_text_blocking(text: str) -> bool:
    """主流程的默认入口，第一次真实调用时才创建共享播报器。

    本函数只处理文字的合成和播放。类别映射、扫描去重和抓取播报时机
    交给任务层处理，避免音频模块依赖导航或抓取状态机。
    """
    global _default_voice
    with _SPEAK_LOCK:
        if _default_voice is None:
            _default_voice = ObjectVoice()
        return _default_voice.speak(text)
