#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
English Voice Assistant with Doorbell Detection (Local YAMNet)
Supports manual Enter key fallback.
Added: AGC, high-pass filter, fuzzy drink matching.
"""

import time
import threading
import collections
import requests
import io
import numpy as np
import noisereduce as nr
import os
import tempfile
import re
import hashlib
import subprocess
import pyaudio
import wave
import webrtcvad
import spacy
import sys
import select
from difflib import get_close_matches
from pathlib import Path

# ------------------ Import language config ------------------
# 添加项目根目录到 sys.path，以便导入 common.config
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))
from common.config import LANGUAGE, COMMON_DRINKS

# ------------------ Doorbell detection (Local YAMNet) ------------------
import tensorflow as tf

YAMNET_MODEL_PATH = os.path.join(PROJECT_ROOT, "models", "yamnet")

# ------------------ Configuration ------------------
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

# 缓存目录
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
TTS_CACHE_DIR = os.path.join(CURRENT_DIR, "audio_cache")
os.makedirs(TTS_CACHE_DIR, exist_ok=True)

# 饮料列表（用于模糊匹配）
if LANGUAGE == "en":
    COMMON_DRINKS_LIST = COMMON_DRINKS  # 从 config 导入的英文列表
else:
    COMMON_DRINKS_LIST = [
        "可乐", "雪碧", "芬达", "美年达", "七喜", "果汁", "橙汁", "苹果汁",
        "牛奶", "酸奶", "水", "矿泉水", "茶", "红茶", "绿茶", "乌龙茶",
        "咖啡", "拿铁", "卡布奇诺", "啤酒", "红酒"
    ]

# 加载英文 NLP 模型（仅在英文模式下）
if LANGUAGE == "en":
    try:
        nlp = spacy.load("en_core_web_sm")
    except OSError:
        print("English model not found. Installing...")
        subprocess.run(["python", "-m", "spacy", "download", "en_core_web_sm"])
        nlp = spacy.load("en_core_web_sm")
else:
    nlp = None

# ------------------ 名字和饮料提取（带模糊匹配） ------------------
def extract_name_en(text):
    if not text:
        return None
    doc = nlp(text)
    for ent in doc.ents:
        if ent.label_ == "PERSON":
            return ent.text
    patterns = [
        r"(?:my name is|i am|i'm|called?|name's?)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        r"^(?:i'?m?|this is)\s+([A-Z][a-z]+)",
        r"call me\s+([A-Z][a-z]+)",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(1)
    words = re.findall(r'[A-Z][a-z]+', text)
    return words[0] if words else None

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

def extract_drink_en(text):
    if not text:
        return None
    text_lower = text.lower()
    # 直接匹配
    for drink in COMMON_DRINKS_LIST:
        if drink in text_lower:
            return drink.capitalize()
    # 模糊匹配
    matches = get_close_matches(text_lower, COMMON_DRINKS_LIST, n=1, cutoff=0.6)
    if matches:
        return matches[0].capitalize()
    return None

def extract_drink_zh(text):
    if not text:
        return None
    for drink in COMMON_DRINKS_LIST:
        if drink in text:
            return drink
    matches = get_close_matches(text, COMMON_DRINKS_LIST, n=1, cutoff=0.6)
    if matches:
        return matches[0]
    return None

# 根据语言绑定函数
if LANGUAGE == "en":
    extract_name = extract_name_en
    extract_drink = extract_drink_en
else:
    extract_name = extract_name_zh
    extract_drink = extract_drink_zh

# ------------------ 辅助函数 ------------------
def wait_for_doorbell_with_fallback(detector):
    print("Waiting for doorbell... (Press Enter to manually trigger)")
    while True:
        if detector and detector.wait_for_doorbell(timeout=0.5):
            print("Doorbell detected!")
            return True
        if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
            line = sys.stdin.readline()
            if line.strip() == "":
                print("Manual trigger by Enter key.")
                return True

def ask_info(assistant, prompt, extract_func, retry_prompt, default=None, max_attempts=2):
    for attempt in range(max_attempts):
        assistant.speak(prompt if attempt == 0 else retry_prompt)
        audio = assistant.record()
        if audio:
            text = assistant.recognize(audio)
            if text:
                info = extract_func(text)
                if info:
                    return info
    return default

# ------------------ Doorbell Detector Class (Local Model) ------------------
class DoorbellDetector:
    def __init__(self, threshold=0.5, chunk_seconds=1.0):
        self.threshold = threshold
        self.chunk_seconds = chunk_seconds
        self.running = False
        self.doorbell_detected = threading.Event()
        self.stream = None
        self.p = None
        self.model = None
        self.doorbell_index = None
        self._init_model()

    def _init_model(self):
        print("[Doorbell] Loading YAMNet model from:", YAMNET_MODEL_PATH)
        self.model = tf.saved_model.load(YAMNET_MODEL_PATH)

        csv_path = os.path.join(YAMNET_MODEL_PATH, 'assets', 'yamnet_class_map.csv')
        if not os.path.exists(csv_path):
            raise FileNotFoundError(f"Class map file not found: {csv_path}")

        class_names = []
        with open(csv_path, 'r') as f:
            lines = f.readlines()
            for line in lines[1:]:
                parts = line.strip().split(',')
                if len(parts) >= 3:
                    class_names.append(parts[2].strip('"'))

        for i, name in enumerate(class_names):
            if 'doorbell' in name.lower():
                self.doorbell_index = i
                break

        if self.doorbell_index is None:
            raise ValueError("Doorbell class not found in class map")
        print(f"[Doorbell] Doorbell class index: {self.doorbell_index} ({class_names[self.doorbell_index]})")

    def _detect(self, audio_bytes):
        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        scores, embeddings, spectrogram = self.model(audio)
        doorbell_probs = scores[:, self.doorbell_index].numpy()
        avg_prob = np.mean(doorbell_probs)
        return avg_prob > self.threshold

    def start(self):
        if self.running:
            return
        self.running = True
        self.doorbell_detected.clear()
        self.p = pyaudio.PyAudio()
        chunk_size = int(RATE * self.chunk_seconds)
        self.stream = self.p.open(format=FORMAT, channels=CHANNELS, rate=RATE,
                                  input=True, frames_per_buffer=chunk_size)
        self.thread = threading.Thread(target=self._listen, daemon=True)
        self.thread.start()
        print("[Doorbell] Listening started...")

    def _listen(self):
        while self.running:
            try:
                data = self.stream.read(int(RATE * self.chunk_seconds), exception_on_overflow=False)
                if self._detect(data):
                    print("[Doorbell] Doorbell detected!")
                    self.doorbell_detected.set()
                    time.sleep(2)
            except Exception as e:
                print(f"[Doorbell] Error: {e}")
                time.sleep(0.5)

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        if self.p:
            self.p.terminate()
        print("[Doorbell] Listening stopped.")

    def wait_for_doorbell(self, timeout=None):
        return self.doorbell_detected.wait(timeout)

# ------------------ VoiceAssistant Class (with AGC and HPF) ------------------
class VoiceAssistant:
    def __init__(self):
        self.vad = webrtcvad.Vad(3)   # 高灵敏度
        self.audio = pyaudio.PyAudio()
        self.stream = None
        self.noise_floor = 500.0
        self.energy_history = collections.deque(maxlen=50)
        self._mpg123_available = self._check_mpg123()

    def _check_mpg123(self):
        try:
            subprocess.run(['which', 'mpg123'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            print("[Info] mpg123 available")
            return True
        except:
            print("[Warning] mpg123 not found, please install: sudo apt install mpg123")
            return False

    def _play_mp3(self, file_path):
        if not self._mpg123_available:
            return False
        try:
            subprocess.run(['mpg123', '-q', file_path], check=True, timeout=10)
            time.sleep(0.3)
            return True
        except Exception as e:
            print(f"[播放异常] {e}")
            return False

    def _get_cache_path(self, text):
        return os.path.join(TTS_CACHE_DIR, hashlib.md5(text.encode()).hexdigest() + ".mp3")

    # ---------- 自动增益控制 ----------
    def _apply_agc(self, audio_int16, target_rms=8000):
        if len(audio_int16) == 0:
            return audio_int16
        rms = np.sqrt(np.mean(audio_int16.astype(np.float32)**2))
        if rms < 1e-6:
            return audio_int16
        gain = target_rms / rms
        gain = np.clip(gain, 0.3, 3.0)
        audio_float = audio_int16.astype(np.float32) * gain
        audio_float = np.clip(audio_float, -32768, 32767)
        return audio_float.astype(np.int16)

    # ---------- 高通滤波（去除低频噪音） ----------
    def _highpass_filter(self, audio_int16, cutoff_hz=80, sample_rate=16000):
        if len(audio_int16) < 2:
            return audio_int16
        rc = 1.0 / (2 * np.pi * cutoff_hz)
        dt = 1.0 / sample_rate
        alpha = rc / (rc + dt)
        x = audio_int16.astype(np.float32)
        y = np.zeros_like(x)
        for i in range(1, len(x)):
            y[i] = alpha * (y[i-1] + x[i] - x[i-1])
        return y.astype(np.int16)

    # ---------- 预加载 ----------
    def preload_tts(self, text):
        cache_path = self._get_cache_path(text)
        if os.path.exists(cache_path):
            return
        try:
            payload = {"model": "tts-1", "input": text, "speed": 1.0}
            resp = requests.post(TTS_URL, json=payload, timeout=10)
            if resp.status_code == 200:
                with open(cache_path, 'wb') as f:
                    f.write(resp.content)
                print(f"[Preload] Cached: {text[:50]}...")
        except Exception as e:
            print(f"[Preload] Error: {e}")

    def preload_common_phrases(self):
        if LANGUAGE == "en":
            common_texts = [
                "Hello, welcome to the party! What's your name?",
                "Sorry, I didn't catch your name. Could you say it again?",
                "What's your favorite drink?",
                "Sorry, I didn't catch that. What's your favorite drink?",
                "Great. Please have a seat.",
                "Thank you both for coming! Enjoy the party."
            ]
        else:
            common_texts = [
                "你好，欢迎来到派对！请问你叫什么名字？",
                "抱歉，我没听清你的名字，能再说一遍吗？",
                "你最喜欢的饮料是什么？",
                "抱歉，我没听清你喜欢的饮料，能再说一次吗？",
                "好的，请坐。",
                "感谢两位的光临！祝你们玩得开心。"
            ]
        for text in common_texts:
            threading.Thread(target=self.preload_tts, args=(text,), daemon=True).start()

    # ---------- 录音控制 ----------
    def set_recording(self, state):
        if state == 1:
            if self.stream is None:
                self.stream = self.audio.open(format=FORMAT, channels=CHANNELS, rate=RATE,
                                              input=True, frames_per_buffer=CHUNK)
                time.sleep(0.3)
                for _ in range(5):
                    try:
                        self.stream.read(CHUNK, exception_on_overflow=False)
                    except:
                        pass
        elif state == 0:
            if self.stream is not None:
                self.stream.stop_stream()
                self.stream.close()
                self.stream = None
        elif state == 2:
            if self.stream is not None and self.stream.is_active():
                self.stream.stop_stream()
        elif state == 3:
            if self.stream is not None and not self.stream.is_active():
                self.stream.start_stream()
                for _ in range(5):
                    try:
                        self.stream.read(CHUNK, exception_on_overflow=False)
                    except:
                        pass

    # ---------- 语音合成 ----------
    def speak(self, text):
        print(f"[Robot]: {text}")
        self.set_recording(2)
        cache_path = self._get_cache_path(text)

        if os.path.exists(cache_path):
            if self._play_mp3(cache_path):
                self.set_recording(3)
                return
            else:
                os.remove(cache_path)

        try:
            resp = requests.post(TTS_URL, json={"model": "tts-1", "input": text, "speed": 1.0}, timeout=5)
            if resp.status_code == 200:
                with open(cache_path, 'wb') as f:
                    f.write(resp.content)
                if self._play_mp3(cache_path):
                    self.set_recording(3)
                    return
            else:
                print(f"[在线 TTS 错误] 状态码 {resp.status_code}")
        except Exception as e:
            print(f"[在线 TTS 异常] {e}")

        self.set_recording(3)
        print(f"[TTS Failed] Could not play: {text}")

    # ---------- 降噪 ----------
    def update_noise_floor(self, frame_energy, is_speech_vad):
        if not is_speech_vad:
            self.energy_history.append(frame_energy)
            if len(self.energy_history) >= 10:
                sorted_energies = sorted(self.energy_history)
                p20_index = max(1, int(len(sorted_energies) * 0.2)) - 1
                self.noise_floor = min(max(sorted_energies[p20_index] * 1.2, 20.0), 8000.0)

    def denoise(self, audio_frames):
        if not audio_frames:
            return audio_frames
        try:
            audio_bytes = b''.join(audio_frames)
            audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
            audio_float32 = audio_int16.astype(np.float32) / 32768.0
            noise_sample_len = int(0.5 * RATE)
            noise_sample = audio_float32[:min(noise_sample_len, len(audio_float32))]
            reduced = nr.reduce_noise(y=audio_float32, sr=RATE, y_noise=noise_sample if len(noise_sample) > 0 else None,
                                      prop_decrease=1.0, time_constant_s=2.0, freq_mask_smooth_hz=500,
                                      time_mask_smooth_ms=50, thresh_n_mult_nonstationary=2,
                                      sigmoid_slope_nonstationary=10, n_std_thresh_stationary=1.5,
                                      stationary=False, n_fft=1024, win_length=1024, hop_length=256)
            reduced_int16 = (reduced * 32768).astype(np.int16)
            frame_bytes = reduced_int16.tobytes()
            frame_size = CHUNK * 2
            return [frame_bytes[i:i+frame_size] for i in range(0, len(frame_bytes), frame_size) if len(frame_bytes[i:i+frame_size]) == frame_size]
        except:
            return audio_frames

    # ---------- 语音识别（增强版：AGC + 高通滤波） ----------
    def recognize(self, audio_frames):
        denoised = self.denoise(audio_frames)
        if not denoised:
            return ""

        # 合并为字节数组，转为 int16
        audio_bytes = b''.join(denoised)
        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)

        # 高通滤波（去除低频风扇/电机噪音）
        audio_int16 = self._highpass_filter(audio_int16)
        # 自动增益控制
        audio_int16 = self._apply_agc(audio_int16)

        # 重新分割为帧（用于保存临时文件）
        frame_size = CHUNK * 2
        frames = [audio_int16[i:i+frame_size].tobytes() for i in range(0, len(audio_int16), frame_size) if len(audio_int16[i:i+frame_size]) == frame_size]

        temp_dir = os.path.join(TTS_CACHE_DIR, "asr_temp")   # 或直接 temp_dir = TTS_CACHE_DIR
        os.makedirs(temp_dir, exist_ok=True)
        timestamp = int(time.time() * 1000)
        temp_path = os.path.join(temp_dir, f"temp_{timestamp}.wav")
        wf = wave.open(temp_path, 'wb')
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(self.audio.get_sample_size(FORMAT))
        wf.setframerate(RATE)
        wf.writeframes(b''.join(frames))
        wf.close()

        try:
            with open(temp_path, 'rb') as af:
                resp = requests.post(ASR_URL, files={"audio": ("audio.wav", af, "audio/wav")}, timeout=5)
                if resp.status_code == 200:
                    text = resp.json().get('text', '').strip()
                    text = re.sub(r'<\|[^>]+\|>', '', text)
                    text = re.sub(r'\b(uh|um|ah|eh|er|mm|hmm)\b', '', text, flags=re.IGNORECASE)
                    text = re.sub(r'[^\w\s\']', '', text)
                    return text.strip()
        except Exception as e:
            print(f"[ASR error] {e}")
        finally:
            self._cleanup_temp_files(50)
            try:
                os.unlink(temp_path)
            except:
                pass
        return ""

    # ---------- 录音 ----------
    def record(self):
        if self.stream is None:
            return []
        ring_buffer = collections.deque(maxlen=RING_BUFFER_MAXLEN)
        frames = []
        recording = False
        silence_frames = 0
        speech_frames = 0
        low_energy_frames = 0
        total_frames = 0
        max_silence_frames = int(SILENCE_TIMEOUT_MS / FRAME_DURATION_MS)
        max_total_frames = int(MAX_RECORD_DURATION_MS / FRAME_DURATION_MS)
        while True:
            data = self.stream.read(CHUNK, exception_on_overflow=False)
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
                else:
                    speech_frames = 0
            else:
                frames.append(data)
                if is_speech:
                    silence_frames = 0
                    low_energy_frames = 0
                else:
                    silence_frames += 1
                    low_energy_frames += 1
                    if silence_frames >= max_silence_frames or low_energy_frames >= MAX_LOW_ENERGY_FRAMES:
                        break
            if total_frames >= max_total_frames:
                break
        return frames

    # ---------- 临时文件清理 ----------
    def _cleanup_temp_files(self, max_files=50):
        temp_dir = "audio_cache"
        os.makedirs(temp_dir, exist_ok=True)
        try:
            files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if f.endswith('.wav')]
            if len(files) > max_files:
                files.sort(key=os.path.getmtime)
                for f in files[:-max_files]:
                    os.remove(f)
        except:
            pass

    # ---------- 关闭资源 ----------
    def close(self):
        self.set_recording(0)
        self.audio.terminate()

# ------------------ 全局实例和兼容接口 ------------------
VoiceAssistant.record_utterance = VoiceAssistant.record

voice_assistant = VoiceAssistant()
doorbell = DoorbellDetector()

def get_voice_assistant():
    return voice_assistant

def create_doorbell_detector(threshold=0.5, chunk_seconds=1.0):
    return DoorbellDetector(threshold, chunk_seconds)

# ------------------ 主函数（独立测试） ------------------
def main():
    try:
        doorbell = DoorbellDetector(threshold=0.5)
        doorbell.start()
    except Exception as e:
        print(f"门铃检测器启动失败: {e}，将使用手动触发")
        doorbell = None

    assistant = VoiceAssistant()
    assistant.set_recording(0)

    print("等待门铃... (按 Enter 手动触发)")
    if doorbell:
        wait_for_doorbell_with_fallback(doorbell)
    else:
        input("按 Enter 模拟门铃...")

    assistant.set_recording(1)

    if LANGUAGE == "en":
        name1 = ask_info(assistant,
                         "Hello, welcome to the party! What's your name?",
                         extract_name,
                         "Sorry, I didn't catch your name. Could you say it again?",
                         "Friend")
        drink1 = ask_info(assistant,
                          f"{name1}, what's your favorite drink?",
                          extract_drink,
                          "Sorry, I didn't catch that. What's your favorite drink?",
                          "Water")
        assistant.speak(f"Great, {name1}. Please have a seat.")
    else:
        name1 = ask_info(assistant,
                         "你好，欢迎来到派对！请问你叫什么名字？",
                         extract_name,
                         "抱歉，我没听清你的名字，能再说一遍吗？",
                         "朋友")
        drink1 = ask_info(assistant,
                          f"{name1}，你最喜欢的饮料是什么？",
                          extract_drink,
                          "抱歉，我没听清你喜欢的饮料，能再说一次吗？",
                          "水")
        assistant.speak(f"好的，{name1}，请坐。")

    assistant.set_recording(0)

    print("等待第二位客人门铃... (按 Enter 手动触发)")
    if doorbell:
        wait_for_doorbell_with_fallback(doorbell)
    else:
        input("按 Enter 模拟门铃...")

    assistant.set_recording(1)

    if LANGUAGE == "en":
        name2 = ask_info(assistant,
                         "Hello, welcome to the party! What's your name?",
                         extract_name,
                         "Sorry, I didn't catch your name. Could you say it again?",
                         "Friend")
        drink2 = ask_info(assistant,
                          f"{name2}, what's your favorite drink?",
                          extract_drink,
                          "Sorry, I didn't catch that. What's your favorite drink?",
                          "Water")
        assistant.speak(f"Great, {name2}. Please have a seat.")
        assistant.speak(f"{name2}, let me introduce {name1}, who likes to drink {drink1}.")
        assistant.speak(f"{name1}, this is {name2}, who likes to drink {drink2}.")
        assistant.speak("Thank you both for coming! Enjoy the party.")
    else:
        name2 = ask_info(assistant,
                         "你好，欢迎来到派对！请问你叫什么名字？",
                         extract_name,
                         "抱歉，我没听清你的名字，能再说一遍吗？",
                         "朋友")
        drink2 = ask_info(assistant,
                          f"{name2}，你最喜欢的饮料是什么？",
                          extract_drink,
                          "抱歉，我没听清你喜欢的饮料，能再说一次吗？",
                          "水")
        assistant.speak(f"好的，{name2}，请坐。")
        assistant.speak(f"{name2}，让我介绍{name1}，他喜欢喝{drink1}。")
        assistant.speak(f"{name1}，这位是{name2}，他喜欢喝{drink2}。")
        assistant.speak("感谢两位的光临！祝你们玩得开心。")

    assistant.close()
    if doorbell:
        doorbell.stop()
    print("程序结束。")

if __name__ == "__main__":
    main()
