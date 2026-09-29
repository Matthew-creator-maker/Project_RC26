#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
完整语音助手模块 - 支持全流程测试（两位客人姓名&饮品）
优化名字提取，增强 Richard 和 Jennier 等名字的识别纠正。
直接运行本脚本即可开始测试，无需机器人硬件。

门铃检测使用 common/skills/audio_module/doorbell_calibration.py 生成的样本：
- 主模板: models/doorbell_template.wav
- 多模板: models/doorbell_templates/*.wav
- 负样本: models/doorbell_negative_samples/*.wav

检测器会自动加载这些文件，使用滑动窗口比较门铃模板，并用负样本降低
人声、脚步、机器人运动声和 TTS 回声造成的误触发。
"""

import time
import threading
import collections
import requests
import numpy as np
import os
import tempfile
import re
import hashlib
import subprocess
import wave
import webrtcvad
import sys
import select
from difflib import SequenceMatcher, get_close_matches
from pathlib import Path
import pyaudio

# ---------- 配置 ----------
DOORBELL_ENABLED = True          # 测试时可保留，但流程中会跳过
INPUT_DEVICE_INDEX = None
RECORD_DURATION = 1.5
SIMILARITY_THRESHOLD = 0.55
SMOOTHING_WINDOW = 5
COOLDOWN_SECONDS = 0.5
DOORBELL_STEP_SECONDS = 0.15
DOORBELL_CONFIRM_SECONDS = 0.8
DOORBELL_CONFIRM_HITS = 2
DOORBELL_HIGH_THRESHOLD_BONUS = 0.08
DOORBELL_NEGATIVE_MARGIN = 0.08
DOORBELL_NOISE_PERCENTILE = 20
DOORBELL_NOISE_RATIO = 1.8
DOORBELL_MIN_RMS = 0.006
DOORBELL_MIN_RMS_DELTA = 0.003
DOORBELL_CENTROID_RANGE = (500, 5500)
DOORBELL_MAX_FLATNESS = 0.45
DOORBELL_MAX_NEGATIVE_WINDOWS = 80

# TTS 语速（1.0 正常，1.2 略快）
TTS_SPEED = 1.2

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))
from common.config import LANGUAGE, COMMON_DRINKS

DOORBELL_TEMPLATE_PATH = PROJECT_ROOT / "models" / "doorbell_template.wav"
DOORBELL_TEMPLATE_DIR = PROJECT_ROOT / "models" / "doorbell_templates"
DOORBELL_NEGATIVE_DIR = PROJECT_ROOT / "models" / "doorbell_negative_samples"
DOORBELL_AUDIO_EXTENSIONS = {".wav", ".ogg", ".mp3", ".flac", ".m4a"}

ASR_URL = "http://127.0.0.1:8001/api/speech_recognition"
TTS_URL = "http://127.0.0.1:8002/v1/audio/speech"

FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000
FRAME_DURATION_MS = 30
CHUNK = int(RATE * FRAME_DURATION_MS / 1000)
SPEECH_START_FRAMES = 12
SILENCE_TIMEOUT_MS = 1500
MAX_RECORD_DURATION_MS = 10000
RING_BUFFER_MAXLEN = int(1000 / FRAME_DURATION_MS)
MAX_LOW_ENERGY_FRAMES = 30

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
TTS_CACHE_DIR = os.path.join(CURRENT_DIR, "audio_cache")
os.makedirs(TTS_CACHE_DIR, exist_ok=True)

# 饮料列表
if LANGUAGE == "en":
    COMMON_DRINKS_LIST = COMMON_DRINKS
else:
    COMMON_DRINKS_LIST = [
        "可乐", "雪碧", "芬达", "美年达", "七喜", "果汁", "橙汁", "苹果汁",
        "牛奶", "酸奶", "水", "矿泉水", "茶", "红茶", "绿茶", "乌龙茶",
        "咖啡", "拿铁", "卡布奇诺", "啤酒", "红酒"
    ]

# 比赛姓名是固定名单，使用闭集识别，避免嘈杂环境下把普通词误当成人名。
KNOWN_GUEST_NAMES = {
    "jack": "Jack",
    "john": "John",
    "allen": "Allen",
    "richard": "Richard",
    "mike": "Mike",
    "grace": "Grace",
    "linda": "Linda",
    "lucy": "Lucy",
    "jennier": "Jennier",
}

NAME_ALIASES = {
    "jhon": "John",
    "jon": "John",
    "johnny": "John",
    "alan": "Allen",
    "allan": "Allen",
    "ellen": "Allen",
    "rechard": "Richard",
    "richord": "Richard",
    "rickard": "Richard",
    "ricard": "Richard",
    "recard": "Richard",
    "mikey": "Mike",
    "mic": "Mike",
    "mikee": "Mike",
    "grance": "Grace",
    "grase": "Grace",
    "grayce": "Grace",
    "linda": "Linda",
    "lindah": "Linda",
    "lusi": "Lucy",
    "lucie": "Lucy",
    "luci": "Lucy",
    "jenny": "Jennier",
    "jennie": "Jennier",
    "jennifer": "Jennier",
    "gennifer": "Jennier",
    "jenn": "Jennier",
    "jenner": "Jennier",
}

NAME_NOISE_WORDS = {
    "a", "an", "and", "are", "be", "bye", "call", "can", "could",
    "er", "hello", "hey", "hi", "hmm", "i", "im", "is", "it",
    "me", "mm", "my", "name", "no", "of", "oh", "ok", "okay",
    "okey", "please", "say", "so", "sorry", "thank", "thanks",
    "that", "the", "then", "there", "this", "to", "uh", "um",
    "yes", "you",
}

NAME_PREFIX_PATTERNS = [
    r"\bmy\s+name\s+is\s+([a-z]+)\b",
    r"\bname\s+is\s+([a-z]+)\b",
    r"\bi\s+am\s+([a-z]+)\b",
    r"\bi'm\s+([a-z]+)\b",
    r"\bim\s+([a-z]+)\b",
    r"\bcall\s+me\s+([a-z]+)\b",
    r"\bthis\s+is\s+([a-z]+)\b",
]

NAME_AMBIGUITY_MARGIN = 0.08

DRINK_NOISE_WORDS = NAME_NOISE_WORDS | {
    "catch", "clearly", "drink", "favorite", "favourite", "have",
    "like", "repeat", "want", "would",
}

DRINK_ALIASES = {
    "cola": "coke",
    "cocacola": "coca cola",
    "coca": "coca cola",
    "cofe": "coffee",
    "cofee": "coffee",
    "watter": "water",
    "waterr": "water",
    "sevenup": "7up",
}

# ---------- 辅助函数 ----------
def safe_filename(text: str, max_len=100) -> str:
    """将文本转换为安全、可读的文件名"""
    illegal_chars = r'[\\/*?:"<>|]'
    safe = re.sub(illegal_chars, '_', text)
    safe = safe.strip('. ')
    if len(safe) > max_len:
        safe = safe[:max_len]
    return safe + ".mp3"

# ---------- 名字提取优化 ----------
def _log_name_extract(raw_text, candidates, result, reason):
    """输出姓名抽取诊断，便于现场根据 ASR 文本补充别名或噪声词。"""
    print(
        f"[NameExtract] raw={raw_text!r} candidates={candidates} "
        f"result={result!r} reason={reason}"
    )


def _dedupe_preserve_order(items):
    """按原始出现顺序去重，保持候选词优先级稳定。"""
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _collect_name_candidates(text):
    """从 ASR 文本中提取可能的姓名词，并过滤明显噪声。"""
    normalized = text.lower().replace("i m", "im")
    prefix_candidates = []
    for pattern in NAME_PREFIX_PATTERNS:
        prefix_candidates.extend(re.findall(pattern, normalized))

    words = re.findall(r"[a-z]+", normalized)
    candidates = []
    for word in prefix_candidates + words:
        if len(word) < 3:
            continue
        if word in NAME_NOISE_WORDS:
            continue
        candidates.append(word)

    return _dedupe_preserve_order(candidates)


def _fuzzy_match_known_name(token):
    """在已知名单内做谨慎模糊匹配，分数不足或歧义过大时拒绝。"""
    scores = sorted(
        (
            (canonical, SequenceMatcher(None, token, known).ratio())
            for known, canonical in KNOWN_GUEST_NAMES.items()
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    best_name, best_score = scores[0]
    second_score = scores[1][1] if len(scores) > 1 else 0.0
    threshold = 0.82 if len(token) <= 4 else 0.74

    if best_score < threshold:
        return None, best_score, "low_confidence"
    if best_score - second_score < NAME_AMBIGUITY_MARGIN:
        return None, best_score, "ambiguous"
    return best_name, best_score, "fuzzy"


def extract_name_en(text):
    """从 ASR 文本中提取比赛名单内的英文姓名，无法确定时返回 None。"""
    if not text:
        _log_name_extract("", [], None, "empty_text")
        return None

    candidates = _collect_name_candidates(text)
    if not candidates:
        _log_name_extract(text, [], None, "no_candidate")
        return None

    for token in candidates:
        result = KNOWN_GUEST_NAMES.get(token)
        if result:
            _log_name_extract(text, candidates, result, "exact")
            return result

    for token in candidates:
        result = NAME_ALIASES.get(token)
        if result:
            _log_name_extract(text, candidates, result, f"alias:{token}")
            return result

    best_reject_reason = "no_whitelist_match"
    best_reject_score = 0.0
    for token in candidates:
        result, score, reason = _fuzzy_match_known_name(token)
        if result:
            _log_name_extract(text, candidates, result, f"fuzzy:{token}:{score:.2f}")
            return result
        if score > best_reject_score:
            best_reject_score = score
            best_reject_reason = f"{reason}:{token}:{score:.2f}"

    _log_name_extract(text, candidates, None, best_reject_reason)
    return None

def extract_name_zh(text):
    if not text:
        return None
    patterns = [
        r'(?:我叫|我是|名字是|姓名是|本人叫|本人是)\s*([^\s，。、]+)',
        r'叫\s*([^\s，。、]+)',
        r'^([^\s，。、]{2,4})$',
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    words = re.findall(r'[\u4e00-\u9fff]+', text)
    return words[-1] if words else None

def _format_drink_name(drink):
    """保持原有英文饮品首字母格式，供介绍阶段播报。"""
    return drink.capitalize()


def extract_drink_en(text):
    """从 ASR 文本中提取饮品，避免把短噪声词模糊成 Tea/Coke。"""
    if not text:
        return None

    tl = text.lower()
    drinks_by_length = sorted(COMMON_DRINKS_LIST, key=len, reverse=True)
    for drink in drinks_by_length:
        pattern = r"\b" + re.escape(drink) + r"\b"
        if re.search(pattern, tl):
            return _format_drink_name(drink)

    words = re.findall(r"[a-z0-9]+", tl)
    candidates = [
        word for word in words
        if len(word) >= 4 and word not in DRINK_NOISE_WORDS
    ]

    for word in candidates:
        alias = DRINK_ALIASES.get(word)
        if alias:
            return _format_drink_name(alias)

    single_word_drinks = [drink for drink in COMMON_DRINKS_LIST if " " not in drink and len(drink) >= 4]
    for word in candidates:
        scored = sorted(
            (
                (drink, SequenceMatcher(None, word, drink).ratio())
                for drink in single_word_drinks
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        if scored and scored[0][1] >= 0.82:
            return _format_drink_name(scored[0][0])

    return None

def extract_drink_zh(text):
    if not text:
        return None
    for d in COMMON_DRINKS_LIST:
        if d in text:
            return d
    matches = get_close_matches(text, COMMON_DRINKS_LIST, n=1, cutoff=0.6)
    return matches[0] if matches else None

if LANGUAGE == "en":
    extract_name = extract_name_en
    extract_drink = extract_drink_en
else:
    extract_name = extract_name_zh
    extract_drink = extract_drink_zh

# ---------- VoiceAssistant ----------
class VoiceAssistant:
    def __init__(self):
        self.vad = webrtcvad.Vad(3)
        self.audio = pyaudio.PyAudio()
        self.stream = None
        self.noise_floor = 500.0
        self.energy_history = collections.deque(maxlen=50)
        self._check_offline_tts()
        # 预加载常用语句（后台异步）
        self._preload_common_phrases()

    def _check_offline_tts(self):
        self.offline_tts_cmd = None
        if subprocess.call(['which', 'pico2wave'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
            self.offline_tts_cmd = 'pico2wave'
            print("[TTS] 离线引擎: pico2wave")
        elif subprocess.call(['which', 'espeak'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
            self.offline_tts_cmd = 'espeak'
            print("[TTS] 离线引擎: espeak")
        else:
            print("[TTS] 警告: 无离线 TTS 引擎")

    def _play_audio(self, file_path):
        """通用音频播放：优先 mpg123，回退 aplay"""
        if not os.path.exists(file_path):
            print(f"[播放] 文件不存在: {file_path}")
            return False

        mpg123_path = subprocess.run(['which', 'mpg123'], capture_output=True, text=True).stdout.strip()
        if mpg123_path:
            cmd = [mpg123_path, '-a', 'default', file_path]
            try:
                print(f"[播放] 执行: {' '.join(cmd)}")
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if result.returncode == 0:
                    print("[播放] 成功（mpg123）")
                    return True
                else:
                    print(f"[播放] mpg123 返回码 {result.returncode}")
            except subprocess.TimeoutExpired:
                print("[播放] mpg123 超时")
            except Exception as e:
                print(f"[播放] mpg123 异常: {e}")

        if file_path.endswith('.wav') and subprocess.call(['which', 'aplay'], stdout=subprocess.DEVNULL) == 0:
            cmd = ['aplay', '-f', 'S16_LE', '-r', '16000', '-c', '1', file_path]
            try:
                print(f"[播放] 执行: {' '.join(cmd)}")
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if result.returncode == 0:
                    print("[播放] 成功（aplay）")
                    return True
            except subprocess.TimeoutExpired:
                print("[播放] aplay 超时")
            except Exception as e:
                print(f"[播放] aplay 异常: {e}")

        print("[播放] 所有播放器均失败")
        return False

    def _offline_speak(self, text):
        if self.offline_tts_cmd == 'pico2wave':
            wav_path = os.path.join(TTS_CACHE_DIR, f"offline_{hashlib.md5(text.encode()).hexdigest()}.wav")
            if not os.path.exists(wav_path):
                cmd = ['pico2wave']
                if LANGUAGE == "en":
                    cmd.extend(['-l', 'en-US'])
                else:
                    cmd.extend(['-l', 'zh-CN'])
                cmd.extend(['-w', wav_path, text])
                try:
                    subprocess.run(cmd, timeout=10, check=True)
                    print(f"[TTS离线] 生成 WAV: {wav_path}")
                except Exception as e:
                    print(f"[TTS离线] 生成失败: {e}")
                    return False
            return self._play_audio(wav_path)
        elif self.offline_tts_cmd == 'espeak':
            try:
                subprocess.run(['espeak', text], timeout=10, check=True)
                return True
            except:
                pass
        return False

    def speak(self, text):
        print(f"[机器人 TTS 输出]: {text}")
        self.set_recording(2)

        cache_path = os.path.join(TTS_CACHE_DIR, safe_filename(text))
        if os.path.exists(cache_path):
            if self._play_audio(cache_path):
                self.set_recording(3)
                return
            else:
                os.remove(cache_path)

        for attempt in range(2):
            try:
                resp = requests.post(TTS_URL, json={"model": "tts-1", "input": text, "speed": TTS_SPEED}, timeout=8)
                if resp.status_code == 200 and len(resp.content) > 1024:
                    with open(cache_path, 'wb') as f:
                        f.write(resp.content)
                    if self._play_audio(cache_path):
                        self.set_recording(3)
                        return
                else:
                    print(f"[TTS在线] 状态码 {resp.status_code}, 长度 {len(resp.content)}")
            except requests.exceptions.Timeout:
                print(f"[TTS在线] 超时，尝试 {attempt+1}/2")
                if attempt == 0:
                    time.sleep(1)
                    continue
            except Exception as e:
                print(f"[TTS在线] 异常: {e}")
            break

        self._offline_speak(text)
        self.set_recording(3)

    def set_recording(self, state):
        if state == 1:
            if self.stream is None:
                try:
                    self.stream = self.audio.open(format=FORMAT, channels=CHANNELS, rate=RATE,
                                                  input=True, frames_per_buffer=CHUNK,
                                                  input_device_index=INPUT_DEVICE_INDEX)
                    time.sleep(0.2)
                    for _ in range(3):
                        self.stream.read(CHUNK, exception_on_overflow=False)
                    print("[录音] 设备打开成功")
                except Exception as e:
                    print(f"[录音] 打开失败: {e}")
                    self.stream = None
        elif state == 0:
            if self.stream:
                self.stream.stop_stream()
                self.stream.close()
                self.stream = None
                print("[录音] 设备关闭")
        elif state == 2:
            if self.stream and self.stream.is_active():
                self.stream.stop_stream()
        elif state == 3:
            if self.stream and not self.stream.is_active():
                self.stream.start_stream()

    def update_noise_floor(self, frame_energy, is_speech_vad):
        if not is_speech_vad:
            self.energy_history.append(frame_energy)
            if len(self.energy_history) >= 10:
                sorted_energies = sorted(self.energy_history)
                p20_index = max(1, int(len(sorted_energies) * 0.2)) - 1
                self.noise_floor = min(max(sorted_energies[p20_index] * 1.2, 20.0), 8000.0)

    def _apply_agc(self, audio_int16, target_rms=8000):
        if len(audio_int16) == 0: return audio_int16
        rms = np.sqrt(np.mean(audio_int16.astype(np.float32)**2))
        if rms < 1e-6: return audio_int16
        gain = target_rms / rms
        gain = np.clip(gain, 0.3, 3.0)
        audio_float = audio_int16.astype(np.float32) * gain
        audio_float = np.clip(audio_float, -32768, 32767)
        return audio_float.astype(np.int16)

    def _highpass_filter(self, audio_int16, cutoff_hz=80):
        if len(audio_int16) < 2: return audio_int16
        rc = 1.0 / (2 * np.pi * cutoff_hz)
        dt = 1.0 / RATE
        alpha = rc / (rc + dt)
        x = audio_int16.astype(np.float32)
        y = np.zeros_like(x)
        for i in range(1, len(x)):
            y[i] = alpha * (y[i-1] + x[i] - x[i-1])
        return y.astype(np.int16)

    def recognize(self, audio_frames, hotwords=None):
        """录音帧 → 文本；``hotwords`` 为空格分隔热词，随请求转发给 ASR 服务。

        热词由客户端（task_home.speech_service）按名单下发，8001 侧与内置热词
        合并，避免"改了名单却动不了服务端热词"造成的姓名识别退化。
        """
        if not audio_frames:
            print("[ASR] 无音频帧")
            return ""
        print(f"[ASR] 收到 {len(audio_frames)} 帧")
        audio_bytes = b''.join(audio_frames)
        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        audio_int16 = self._highpass_filter(audio_int16)
        audio_int16 = self._apply_agc(audio_int16)

        frame_size = CHUNK * 2
        frames = [audio_int16[i:i+frame_size].tobytes() for i in range(0, len(audio_int16), frame_size)
                  if len(audio_int16[i:i+frame_size]) == frame_size]
        if not frames:
            print("[ASR] 重切分后无有效帧")
            return ""

        temp_path = os.path.join(TTS_CACHE_DIR, f"temp_{int(time.time()*1000)}.wav")
        wf = wave.open(temp_path, 'wb')
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(self.audio.get_sample_size(FORMAT))
        wf.setframerate(RATE)
        wf.writeframes(b''.join(frames))
        wf.close()
        print(f"[ASR] 保存临时文件 {temp_path}")

        hotwords = " ".join(str(hotwords or "").split())
        form = {"hotword": hotwords} if hotwords else None
        try:
            with open(temp_path, 'rb') as af:
                resp = requests.post(
                    ASR_URL,
                    files={"audio": ("audio.wav", af, "audio/wav")},
                    data=form,
                    timeout=5,
                )
                print(f"[ASR] HTTP {resp.status_code}")
                if resp.status_code == 200:
                    text = resp.json().get('text', '').strip()
                    text = re.sub(r'<\|[^>]+\|>', '', text)
                    text = re.sub(r'\b(uh|um|ah|eh|er|mm|hmm)\b', '', text, flags=re.IGNORECASE)
                    print(f"[ASR 识别结果]: '{text}'")
                    return text
                else:
                    print(f"[ASR] 错误: {resp.text}")
        except Exception as e:
            print(f"[ASR] 异常: {e}")
        finally:
            try: os.unlink(temp_path)
            except: pass
        return ""

    def record(self):
        if self.stream is None:
            print("[录音] 流未打开")
            return []
        ring_buffer = collections.deque(maxlen=RING_BUFFER_MAXLEN)
        frames = []
        recording = False
        silence_frames = 0
        speech_frames = 0
        total_frames = 0
        max_silence_frames = int(SILENCE_TIMEOUT_MS / FRAME_DURATION_MS)
        max_total_frames = int(MAX_RECORD_DURATION_MS / FRAME_DURATION_MS)
        print("[录音] 开始监听语音...")
        while True:
            try:
                data = self.stream.read(CHUNK, exception_on_overflow=False)
            except Exception as e:
                print(f"[录音] 读取错误: {e}")
                break
            audio_array = np.frombuffer(data, dtype=np.int16)
            energy = np.sqrt(np.mean(audio_array.astype(float)**2))
            is_vad = self.vad.is_speech(data, RATE)
            self.update_noise_floor(energy, is_vad)
            thresh = self.noise_floor * 1.2
            is_speech = is_vad and (energy > thresh)
            total_frames += 1

            if not recording:
                ring_buffer.append(data)
                if is_speech:
                    speech_frames += 1
                    if speech_frames >= SPEECH_START_FRAMES:
                        frames.extend(ring_buffer)
                        recording = True
                        print("[录音] 检测到语音，开始录制")
                else:
                    speech_frames = 0
            else:
                frames.append(data)
                if not is_speech:
                    silence_frames += 1
                    if silence_frames >= max_silence_frames:
                        print("[录音] 语音结束（静音超时）")
                        break
                else:
                    silence_frames = 0
            if total_frames >= max_total_frames:
                print("[录音] 达到最大时长")
                break
        print(f"[录音] 录制完成，共 {len(frames)} 帧")
        return frames

    def close(self):
        self.set_recording(0)
        self.audio.terminate()

    def _preload_common_phrases(self):
        """后台预加载常用 TTS 缓存"""
        common_phrases = [
            "Welcome! May I know your name?",
            "Sorry, I didn't catch your name. Could you say it again?",
            "What is your favorite drink?",
            "Sorry, I didn't catch that. What is your favorite drink?",
            "Please have a seat here.",
            "Thank you! Enjoy the party."
        ]
        for phrase in common_phrases:
            threading.Thread(target=self._cache_tts, args=(phrase,), daemon=True).start()

    def _cache_tts(self, text):
        """异步生成 TTS 缓存文件"""
        cache_path = os.path.join(TTS_CACHE_DIR, safe_filename(text))
        if os.path.exists(cache_path):
            return
        try:
            resp = requests.post(TTS_URL, json={"model": "tts-1", "input": text, "speed": TTS_SPEED}, timeout=10)
            if resp.status_code == 200 and len(resp.content) > 1024:
                with open(cache_path, 'wb') as f:
                    f.write(resp.content)
                print(f"[TTS预加载] 已缓存: {text[:30]}...")
        except Exception as e:
            print(f"[TTS预加载] 失败: {e}")

# 为兼容旧接口添加别名
VoiceAssistant.record_utterance = VoiceAssistant.record
VoiceAssistant.recognize_speech = VoiceAssistant.recognize

# ---------- 全局实例 ----------
voice_assistant = VoiceAssistant()

# ---------- 门铃检测部分（测试时可忽略） ----------
if DOORBELL_ENABLED:
    try:
        import librosa
        import sounddevice as sd
        DOORBELL_DEPS = True
    except ImportError as e:
        DOORBELL_DEPS = False
        print(f"[门铃] 依赖缺失，自动检测禁用: {e}")

    if DOORBELL_DEPS:
        # ---------- 特征提取 ----------
        def _cosine_similarity(a, b):
            """计算两个特征向量的余弦相似度，避免零向量产生 NaN。"""
            denom = float(np.linalg.norm(a) * np.linalg.norm(b))
            if denom < 1e-8:
                return 0.0
            return float(np.dot(a, b) / denom)


        def _prepare_audio(y):
            """把音频转为 float32 mono，并做峰值归一化。"""
            audio = np.asarray(y, dtype=np.float32).reshape(-1)
            if audio.size == 0:
                return audio
            peak = float(np.max(np.abs(audio)))
            if peak > 1e-6:
                audio = audio / peak
            return audio


        def _normalize_feature_vector(features):
            """标准化特征向量，减少不同特征量纲差异。"""
            features = np.asarray(features, dtype=np.float32)
            if features.size == 0:
                return features
            mean = float(np.mean(features))
            std = float(np.std(features))
            if std < 1e-6:
                return features - mean
            return (features - mean) / std


        def extract_features(y, sr):
            """提取整段统计特征，作为门铃模板相似度的一部分。"""
            y = _prepare_audio(y)
            if y.size < int(0.2 * sr):
                return np.zeros(47, dtype=np.float32)

            mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
            mfcc_delta = librosa.feature.delta(mfcc)
            mfcc_delta2 = librosa.feature.delta(mfcc, order=2)
            centroid = librosa.feature.spectral_centroid(y=y, sr=sr)
            rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr)
            flatness = librosa.feature.spectral_flatness(y=y)
            zcr = librosa.feature.zero_crossing_rate(y)
            features = np.concatenate([
                np.mean(mfcc, axis=1),
                np.mean(mfcc_delta, axis=1),
                np.mean(mfcc_delta2, axis=1),
                [np.mean(centroid), np.std(centroid)],
                [np.mean(rolloff), np.std(rolloff)],
                [np.mean(flatness), np.std(flatness)],
                [np.mean(zcr), np.std(zcr)],
            ])
            return _normalize_feature_vector(features)


        def extract_sequence(y, sr):
            """提取短时 log-mel 序列，保留门铃的时间结构。"""
            y = _prepare_audio(y)
            if y.size < int(0.2 * sr):
                return np.zeros((1, 32), dtype=np.float32)

            mel = librosa.feature.melspectrogram(
                y=y,
                sr=sr,
                n_mels=32,
                fmin=250,
                fmax=6000,
                n_fft=1024,
                hop_length=256,
                power=2.0,
            )
            log_mel = librosa.power_to_db(mel, ref=np.max).T.astype(np.float32)
            mean = np.mean(log_mel, axis=0, keepdims=True)
            std = np.std(log_mel, axis=0, keepdims=True)
            return (log_mel - mean) / np.maximum(std, 1e-6)


        def _resample_sequence(sequence, target_frames):
            """把短时特征序列重采样到同一帧数后比较。"""
            if sequence.shape[0] == target_frames:
                return sequence
            if sequence.shape[0] <= 1:
                return np.repeat(sequence, target_frames, axis=0)

            old_x = np.linspace(0.0, 1.0, sequence.shape[0])
            new_x = np.linspace(0.0, 1.0, target_frames)
            columns = [
                np.interp(new_x, old_x, sequence[:, col])
                for col in range(sequence.shape[1])
            ]
            return np.stack(columns, axis=1).astype(np.float32)


        def _sequence_similarity(candidate_sequence, reference_sequence):
            """比较两个门铃短时特征序列，返回 0-1 相似度。"""
            target_frames = max(candidate_sequence.shape[0], reference_sequence.shape[0], 1)
            cand = _resample_sequence(candidate_sequence, target_frames)
            ref = _resample_sequence(reference_sequence, target_frames)

            cand_norm = np.linalg.norm(cand, axis=1)
            ref_norm = np.linalg.norm(ref, axis=1)
            denom = np.maximum(cand_norm * ref_norm, 1e-8)
            frame_scores = np.sum(cand * ref, axis=1) / denom
            return float((np.mean(frame_scores) + 1.0) / 2.0)


        def _audio_rms(audio):
            """计算候选音频的 RMS 能量。"""
            return float(np.sqrt(np.mean(audio**2))) if audio.size else 0.0


        def _spectral_metrics(audio, sr):
            """计算候选音频的基本频谱指标。"""
            if audio.size < int(0.2 * sr):
                return 0.0, 1.0
            centroid = float(np.mean(librosa.feature.spectral_centroid(y=audio, sr=sr)))
            flatness = float(np.mean(librosa.feature.spectral_flatness(y=audio)))
            return centroid, flatness


        def _make_reference(name, audio, sr):
            """把模板或负样本音频转为检测引用。"""
            return {
                "name": name,
                "features": extract_features(audio, sr),
                "sequence": extract_sequence(audio, sr),
            }


        def _reference_score(audio_data, reference, sr=RATE):
            """计算候选音频和单个引用样本的组合相似度。"""
            if len(audio_data) < int(0.4 * sr):
                return 0.0
            features = extract_features(audio_data, sr)
            sequence = extract_sequence(audio_data, sr)
            summary_score = _cosine_similarity(features, reference["features"])
            summary_score = (summary_score + 1.0) / 2.0
            sequence_score = _sequence_similarity(sequence, reference["sequence"])
            return float(0.55 * summary_score + 0.45 * sequence_score)


        def compute_similarity(audio_data, references, sr=RATE):
            """返回候选音频对一组引用样本的最高相似度和引用名。"""
            if not references:
                return 0.0, ""

            best_score = 0.0
            best_name = ""
            for reference in references:
                score = _reference_score(audio_data, reference, sr)
                if score > best_score:
                    best_score = score
                    best_name = reference["name"]
            return best_score, best_name


        def _iter_audio_files(path):
            """遍历一个文件或目录下可作为模板/负样本的音频文件。"""
            path = Path(path)
            if path.is_file() and path.suffix.lower() in DOORBELL_AUDIO_EXTENSIONS:
                return [path]
            if not path.is_dir():
                return []
            return sorted(
                child for child in path.iterdir()
                if child.is_file() and child.suffix.lower() in DOORBELL_AUDIO_EXTENSIONS
            )


        def _load_audio(path, max_duration=None):
            """读取音频文件并转为检测器采样率。"""
            audio, _ = librosa.load(str(path), sr=RATE, mono=True)
            if max_duration:
                max_len = int(RATE * max_duration)
                audio = audio[:max_len]
            return audio.astype(np.float32)


        def _load_template_references(template_path, template_dir, duration):
            """加载主模板和多模板目录。"""
            paths = []
            for candidate in [template_path, template_dir]:
                for path in _iter_audio_files(candidate):
                    if path not in paths:
                        paths.append(path)

            references = []
            for path in paths:
                try:
                    audio = _load_audio(path, max_duration=duration)
                    references.append(_make_reference(path.name, audio, RATE))
                    print(f"[门铃] 已加载模板: {path}")
                except Exception as exc:
                    print(f"[门铃] 模板加载失败 {path}: {exc}")
            return references


        def _load_negative_references(negative_dir, duration):
            """加载现场负样本，并切成检测窗口长度参与拒绝判定。"""
            paths = _iter_audio_files(negative_dir)
            references = []
            window_len = int(duration * RATE)
            hop_len = max(int(window_len / 2), 1)

            for path in paths:
                try:
                    audio = _load_audio(path)
                    if len(audio) < window_len:
                        windows = [audio]
                    else:
                        windows = [
                            audio[start:start + window_len]
                            for start in range(0, len(audio) - window_len + 1, hop_len)
                        ]

                    for idx, window in enumerate(windows):
                        if len(references) >= DOORBELL_MAX_NEGATIVE_WINDOWS:
                            break
                        if float(np.sqrt(np.mean(window**2))) < 0.002:
                            continue
                        references.append(_make_reference(f"{path.name}#{idx}", window, RATE))

                    print(f"[门铃] 已加载负样本: {path}")
                except Exception as exc:
                    print(f"[门铃] 负样本加载失败 {path}: {exc}")

                if len(references) >= DOORBELL_MAX_NEGATIVE_WINDOWS:
                    break
            return references

        class SoundDeviceDoorbellDetector:
            def __init__(self, template_path, device_index=None,
                         duration=1.5, threshold=0.75, window_size=5, cooldown=0.5,
                         template_dir=None, negative_dir=None):
                self.device_index = device_index
                self.duration = duration
                self.threshold = threshold
                self.high_threshold = threshold + DOORBELL_HIGH_THRESHOLD_BONUS
                self.window_size = window_size
                self.cooldown = cooldown
                self.running = False
                self.doorbell_detected = threading.Event()
                self.stream_thread = None
                self.templates = _load_template_references(
                    template_path=template_path,
                    template_dir=template_dir,
                    duration=duration,
                )
                self.negative_refs = _load_negative_references(
                    negative_dir=negative_dir,
                    duration=duration,
                )

                if not self.templates:
                    raise FileNotFoundError("未找到可用门铃模板")

                print(
                    "[门铃] 检测器初始化完成: "
                    f"templates={len(self.templates)}, negatives={len(self.negative_refs)}, "
                    f"window={self.duration:.2f}s, step={DOORBELL_STEP_SECONDS:.2f}s, "
                    f"threshold={self.threshold:.2f}"
                )

            def start(self):
                if self.running:
                    return
                self.running = True
                self.doorbell_detected.clear()
                self.stream_thread = threading.Thread(target=self._listen, daemon=True)
                self.stream_thread.start()
                print(f"[门铃] 检测器已启动")

            def stop(self):
                self.running = False
                if self.stream_thread:
                    self.stream_thread.join(timeout=1)
                print("[门铃] 检测器已停止")

            def wait_for_doorbell(self, timeout=None):
                return self.doorbell_detected.wait(timeout)

            def _listen(self):
                audio_window = collections.deque(maxlen=int(self.duration * RATE))
                rms_history = collections.deque(maxlen=max(10, int(8 / DOORBELL_STEP_SECONDS)))
                hit_times = collections.deque(maxlen=max(self.window_size, DOORBELL_CONFIRM_HITS * 3))
                step_samples = int(DOORBELL_STEP_SECONDS * RATE)
                last_trigger_time = 0
                last_log_time = 0

                try:
                    with sd.InputStream(
                        samplerate=RATE,
                        channels=1,
                        dtype="float32",
                        blocksize=step_samples,
                        device=self.device_index,
                    ) as stream:
                        while self.running:
                            if time.time() - last_trigger_time < self.cooldown:
                                time.sleep(DOORBELL_STEP_SECONDS)
                                continue

                            block, overflowed = stream.read(step_samples)
                            if overflowed:
                                print("[门铃] 音频输入溢出，可能丢帧")

                            samples = np.asarray(block, dtype=np.float32).reshape(-1)
                            audio_window.extend(samples.tolist())
                            if len(audio_window) < audio_window.maxlen:
                                continue

                            audio = np.fromiter(audio_window, dtype=np.float32, count=len(audio_window))
                            rms = _audio_rms(audio)

                            if len(rms_history) >= 5:
                                noise_floor = float(np.percentile(rms_history, DOORBELL_NOISE_PERCENTILE))
                            else:
                                noise_floor = 0.005

                            energy_ok = (
                                rms > max(DOORBELL_MIN_RMS, noise_floor * DOORBELL_NOISE_RATIO)
                                and (rms - noise_floor) > DOORBELL_MIN_RMS_DELTA
                            )
                            centroid = 0.0
                            flatness = 1.0
                            centroid_ok = False
                            tonal_ok = False

                            if energy_ok:
                                centroid, flatness = _spectral_metrics(audio, RATE)
                                centroid_ok = (
                                    DOORBELL_CENTROID_RANGE[0] <= centroid <= DOORBELL_CENTROID_RANGE[1]
                                )
                                tonal_ok = flatness <= DOORBELL_MAX_FLATNESS

                            positive_score = 0.0
                            positive_name = ""
                            negative_score = 0.0
                            negative_name = ""
                            score_ok = False
                            margin_ok = True

                            if energy_ok and centroid_ok and tonal_ok:
                                positive_score, positive_name = compute_similarity(audio, self.templates, RATE)
                                negative_score, negative_name = compute_similarity(audio, self.negative_refs, RATE)
                                margin_ok = (
                                    not self.negative_refs
                                    or positive_score - negative_score >= DOORBELL_NEGATIVE_MARGIN
                                )
                                score_ok = positive_score >= self.threshold and margin_ok

                            if score_ok:
                                now = time.time()
                                hit_times.append(now)
                                recent_hits = [
                                    hit_time for hit_time in hit_times
                                    if now - hit_time <= DOORBELL_CONFIRM_SECONDS
                                ]
                                high_score = positive_score >= self.high_threshold
                                is_match = len(recent_hits) >= DOORBELL_CONFIRM_HITS
                                should_log = True
                            else:
                                is_match = False
                                high_score = False
                                should_log = time.time() - last_log_time >= 1.0
                                if not energy_ok or positive_score < self.threshold - 0.05:
                                    rms_history.append(rms)

                            if should_log:
                                print(
                                    "[门铃] "
                                    f"score={positive_score:.3f}/{self.threshold:.3f} "
                                    f"high={high_score} tpl={positive_name or '-'} | "
                                    f"neg={negative_score:.3f} neg_tpl={negative_name or '-'} "
                                    f"margin_ok={margin_ok} | "
                                    f"RMS={rms:.4f} noise={noise_floor:.4f} energy={energy_ok} | "
                                    f"centroid={centroid:.0f} centroid_ok={centroid_ok} "
                                    f"flatness={flatness:.3f} tonal={tonal_ok} | "
                                    f"hits={len(hit_times)} trigger={is_match}"
                                )
                                last_log_time = time.time()

                            if is_match:
                                print("[门铃] ✅ 检测到匹配的门铃声！")
                                self.doorbell_detected.set()
                                last_trigger_time = time.time()
                                hit_times.clear()
                except Exception as e:
                    print(f"[门铃] 检测错误: {e}")

        _template_path = DOORBELL_TEMPLATE_PATH
        _has_primary_template = _template_path.exists()
        _has_template_dir = bool(_iter_audio_files(DOORBELL_TEMPLATE_DIR))
        if _has_primary_template or _has_template_dir:
            try:
                doorbell = SoundDeviceDoorbellDetector(
                    template_path=_template_path,
                    device_index=INPUT_DEVICE_INDEX,
                    duration=RECORD_DURATION,
                    threshold=SIMILARITY_THRESHOLD,
                    window_size=SMOOTHING_WINDOW,
                    cooldown=COOLDOWN_SECONDS,
                    template_dir=DOORBELL_TEMPLATE_DIR,
                    negative_dir=DOORBELL_NEGATIVE_DIR,
                )
                print("[门铃] 全局门铃检测器已创建")
            except Exception as e:
                print(f"[门铃] 创建失败: {e}")
                doorbell = None
        else:
            print(
                "[门铃] 模板不存在，门铃不可用: "
                f"{DOORBELL_TEMPLATE_PATH} 或 {DOORBELL_TEMPLATE_DIR}"
            )
            doorbell = None
    else:
        doorbell = None
else:
    doorbell = None
    print("[门铃] 已禁用（DOORBELL_ENABLED=False）")

__all__ = ['voice_assistant', 'doorbell', 'extract_name', 'extract_drink']

# ---------- 全流程测试入口 ----------
def ask_info(prompt, extract_func, retry_prompt, default, max_attempts=2):
    for attempt in range(max_attempts):
        voice_assistant.speak(prompt if attempt == 0 else retry_prompt)
        audio = voice_assistant.record()
        if audio:
            text = voice_assistant.recognize(audio)
            if text:
                info = extract_func(text)
                if info:
                    return info
    return default

def test_full_flow():
    print("\n===== 全流程测试（两位客人）=====")
    input("按 Enter 开始接待第一位客人...")
    
    # 客人1
    name1 = ask_info(
        "Welcome! May I know your name?",
        extract_name,
        "Sorry, I didn't catch your name. Could you say it again?",
        "Friend"
    )
    drink1 = ask_info(
        f"{name1}, what is your favorite drink?",
        extract_drink,
        "Sorry, I didn't catch that. What is your favorite drink?",
        "Water"
    )
    voice_assistant.speak(f"Great, {name1}. Please have a seat.")
    print(f"\n--- 客人1 信息 ---\n姓名: {name1}\n饮品: {drink1}\n")
    
    input("\n按 Enter 开始接待第二位客人...")
    
    # 客人2
    name2 = ask_info(
        "Welcome! May I know your name?",
        extract_name,
        "Sorry, I didn't catch your name. Could you say it again?",
        "Friend"
    )
    drink2 = ask_info(
        f"{name2}, what is your favorite drink?",
        extract_drink,
        "Sorry, I didn't catch that. What is your favorite drink?",
        "Water"
    )
    voice_assistant.speak(f"Great, {name2}. Please have a seat.")
    voice_assistant.speak(f"{name2}, let me introduce {name1}, who likes to drink {drink1}.")
    voice_assistant.speak(f"{name1}, this is {name2}, who likes to drink {drink2}.")
    voice_assistant.speak("Thank you both for coming! Enjoy the party.")
    
    print("\n===== 全流程测试完成 =====")

if __name__ == "__main__":
    # 启用门铃检测
    if doorbell is not None:
        print("等待门铃触发（30秒内）...")
        doorbell.start()
        if doorbell.wait_for_doorbell(timeout=30):
            print("门铃已触发，开始全流程测试")
        else:
            print("等待超时，仍开始测试")
        doorbell.stop()
    else:
        print("门铃检测不可用，直接开始测试")
    
    voice_assistant.set_recording(1)
    test_full_flow()
    voice_assistant.close()
