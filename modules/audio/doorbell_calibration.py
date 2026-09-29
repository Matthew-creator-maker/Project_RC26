#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
门铃模板校准工具。

典型使用流程：

1. 查看机器人麦克风设备：
   python3 common/skills/audio_module/doorbell_calibration.py devices

2. 用比赛实际播放设备播放门铃，并用机器人麦克风录一段原始模板：
   python3 common/skills/audio_module/doorbell_calibration.py record-template --duration 3

3. 把原始模板裁成干净的门铃片段，并写入主模板：
   python3 common/skills/audio_module/doorbell_calibration.py crop-template \
       --input common/skills/audio_module/doorbell_recordings/template_raw.wav \
       --output models/doorbell_template.wav

4. 录多个距离/音量下的模板，放入多模板目录：
   python3 common/skills/audio_module/doorbell_calibration.py record-template \
       --duration 3 --output common/skills/audio_module/doorbell_recordings/template_near_raw.wav
   python3 common/skills/audio_module/doorbell_calibration.py crop-template \
       --input common/skills/audio_module/doorbell_recordings/template_near_raw.wav \
       --output models/doorbell_templates/template_near.wav

5. 录制负样本，包含无门铃的人声、脚步、机器人运动声、TTS 回声：
   python3 common/skills/audio_module/doorbell_calibration.py record-negative --label speech --duration 20
   python3 common/skills/audio_module/doorbell_calibration.py record-negative --label robot_motion --duration 20

voice_assiant.py 会自动加载：
- models/doorbell_template.wav
- models/doorbell_templates/*.wav
- models/doorbell_negative_samples/*.wav
"""

from __future__ import annotations

import argparse
import datetime as _dt
import wave
from pathlib import Path

try:
    import numpy as np
except ImportError:
    np = None


RATE = 16000
CHANNELS = 1
PROJECT_ROOT = Path(__file__).resolve().parents[3]
MODULE_DIR = Path(__file__).resolve().parent
RAW_RECORDING_DIR = MODULE_DIR / "doorbell_recordings"
DEFAULT_TEMPLATE_PATH = PROJECT_ROOT / "models" / "doorbell_template.wav"
DEFAULT_TEMPLATE_DIR = PROJECT_ROOT / "models" / "doorbell_templates"
DEFAULT_NEGATIVE_DIR = PROJECT_ROOT / "models" / "doorbell_negative_samples"


def _load_sounddevice():
    """按需导入 sounddevice，避免静态检查时访问音频设备。"""
    import sounddevice as sd

    return sd


def _require_numpy():
    """按需导入 numpy，并在缺失时给出清晰错误。"""
    global np
    if np is None:
        try:
            import numpy as _np
        except ImportError as exc:
            raise RuntimeError("当前 Python 环境缺少 numpy，无法处理音频数据") from exc
        np = _np
    return np


def _timestamp() -> str:
    """生成文件名用的时间戳。"""
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _ensure_parent(path: Path) -> None:
    """确保输出文件所在目录存在。"""
    path.parent.mkdir(parents=True, exist_ok=True)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """读取 mono/stereo PCM WAV，返回 float32 mono 波形和采样率。"""
    np = _require_numpy()
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    if sample_width == 2:
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 4:
        audio = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"不支持的 WAV 采样宽度: {sample_width} bytes")

    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio.astype(np.float32), sample_rate


def write_wav(path: Path, audio: np.ndarray, sample_rate: int = RATE) -> None:
    """写入 16-bit PCM mono WAV。"""
    np = _require_numpy()
    _ensure_parent(path)
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767.0).astype(np.int16)

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


def resample_if_needed(audio: np.ndarray, source_rate: int, target_rate: int = RATE) -> np.ndarray:
    """必要时用 librosa 重采样到检测器采样率。"""
    np = _require_numpy()
    if source_rate == target_rate:
        return audio.astype(np.float32)

    import librosa

    return librosa.resample(audio.astype(np.float32), orig_sr=source_rate, target_sr=target_rate)


def list_devices(_args) -> None:
    """列出 sounddevice 可见的音频设备。"""
    sd = _load_sounddevice()
    print(sd.query_devices())


def record_audio(duration: float, device_index: int | None) -> np.ndarray:
    """从指定输入设备录制 float32 mono 音频。"""
    np = _require_numpy()
    sd = _load_sounddevice()
    print(f"[门铃校准] 开始录音 {duration:.1f}s，device={device_index}")
    audio = sd.rec(
        int(duration * RATE),
        samplerate=RATE,
        channels=CHANNELS,
        dtype="float32",
        device=device_index,
    )
    sd.wait()
    print("[门铃校准] 录音完成")
    return audio.reshape(-1).astype(np.float32)


def record_template(args) -> None:
    """录制一段原始门铃模板。"""
    output = Path(args.output) if args.output else RAW_RECORDING_DIR / "template_raw.wav"
    audio = record_audio(args.duration, args.device)
    write_wav(output, audio, RATE)
    print(f"[门铃校准] 原始模板已保存: {output}")


def record_negative(args) -> None:
    """录制一段现场负样本。"""
    safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in args.label).strip("_")
    safe_label = safe_label or "negative"
    output_dir = Path(args.output_dir)
    output = output_dir / f"{safe_label}_{_timestamp()}.wav"
    audio = record_audio(args.duration, args.device)
    write_wav(output, audio, RATE)
    print(f"[门铃校准] 负样本已保存: {output}")


def _frame_rms(audio: np.ndarray, frame_len: int, hop_len: int) -> np.ndarray:
    """计算短时 RMS，用于自动裁剪门铃有效片段。"""
    np = _require_numpy()
    if len(audio) < frame_len:
        return np.array([float(np.sqrt(np.mean(audio**2)))], dtype=np.float32)

    rms_values = []
    for start in range(0, len(audio) - frame_len + 1, hop_len):
        frame = audio[start:start + frame_len]
        rms_values.append(float(np.sqrt(np.mean(frame**2))))
    return np.asarray(rms_values, dtype=np.float32)


def _find_active_region(
    audio: np.ndarray,
    sample_rate: int,
    threshold_ratio: float,
    absolute_threshold: float,
    padding_ms: float,
) -> tuple[int, int]:
    """根据短时能量找到门铃主体区域。"""
    np = _require_numpy()
    frame_len = max(1, int(0.02 * sample_rate))
    hop_len = max(1, int(0.01 * sample_rate))
    rms = _frame_rms(audio, frame_len, hop_len)
    noise_floor = float(np.percentile(rms, 20)) if rms.size else 0.0
    threshold = max(noise_floor * threshold_ratio, absolute_threshold)
    active = np.flatnonzero(rms > threshold)

    if active.size == 0:
        print("[门铃校准] 未找到明显活动段，保留原始音频")
        return 0, len(audio)

    pad = int(padding_ms * sample_rate / 1000.0)
    start = max(0, int(active[0] * hop_len) - pad)
    end = min(len(audio), int(active[-1] * hop_len + frame_len) + pad)
    print(
        "[门铃校准] 裁剪: "
        f"noise_floor={noise_floor:.5f}, threshold={threshold:.5f}, "
        f"start={start / sample_rate:.2f}s, end={end / sample_rate:.2f}s"
    )
    return start, end


def crop_template(args) -> None:
    """自动裁剪原始模板并写出标准检测模板。"""
    np = _require_numpy()
    input_path = Path(args.input)
    output_path = Path(args.output)
    audio, sample_rate = read_wav(input_path)
    audio = resample_if_needed(audio, sample_rate, RATE)

    start, end = _find_active_region(
        audio=audio,
        sample_rate=RATE,
        threshold_ratio=args.threshold_ratio,
        absolute_threshold=args.absolute_threshold,
        padding_ms=args.padding_ms,
    )
    cropped = audio[start:end]

    if args.max_duration and len(cropped) > int(args.max_duration * RATE):
        cropped = cropped[:int(args.max_duration * RATE)]
        print(f"[门铃校准] 已按 max-duration 截断到 {args.max_duration:.2f}s")

    peak = float(np.max(np.abs(cropped))) if cropped.size else 0.0
    if args.normalize and peak > 1e-6:
        cropped = cropped * min(1.0 / peak * 0.8, 4.0)

    write_wav(output_path, cropped, RATE)
    print(f"[门铃校准] 裁剪模板已保存: {output_path}, duration={len(cropped) / RATE:.2f}s")


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="门铃模板和负样本校准工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    devices_parser = subparsers.add_parser("devices", help="列出音频输入/输出设备")
    devices_parser.set_defaults(func=list_devices)

    record_template_parser = subparsers.add_parser("record-template", help="录制原始门铃模板")
    record_template_parser.add_argument("--duration", type=float, default=3.0, help="录音时长，秒")
    record_template_parser.add_argument("--device", type=int, default=None, help="sounddevice 输入设备编号")
    record_template_parser.add_argument("--output", default=str(RAW_RECORDING_DIR / "template_raw.wav"))
    record_template_parser.set_defaults(func=record_template)

    crop_parser = subparsers.add_parser("crop-template", help="裁剪原始模板为检测模板")
    crop_parser.add_argument("--input", required=True, help="原始模板 WAV 路径")
    crop_parser.add_argument("--output", default=str(DEFAULT_TEMPLATE_PATH), help="输出模板 WAV 路径")
    crop_parser.add_argument("--threshold-ratio", type=float, default=3.0, help="活动段阈值相对噪声倍数")
    crop_parser.add_argument("--absolute-threshold", type=float, default=0.01, help="活动段绝对 RMS 阈值")
    crop_parser.add_argument("--padding-ms", type=float, default=120.0, help="裁剪前后保留静音，毫秒")
    crop_parser.add_argument("--max-duration", type=float, default=1.8, help="最大模板时长，秒；0 表示不截断")
    crop_parser.add_argument("--normalize", action="store_true", help="把峰值归一化到约 0.8")
    crop_parser.set_defaults(func=crop_template)

    negative_parser = subparsers.add_parser("record-negative", help="录制现场负样本")
    negative_parser.add_argument("--label", required=True, help="负样本标签，如 speech/footstep/robot_motion")
    negative_parser.add_argument("--duration", type=float, default=20.0, help="录音时长，秒")
    negative_parser.add_argument("--device", type=int, default=None, help="sounddevice 输入设备编号")
    negative_parser.add_argument("--output-dir", default=str(DEFAULT_NEGATIVE_DIR), help="负样本输出目录")
    negative_parser.set_defaults(func=record_negative)
    return parser


def main() -> None:
    """命令行入口。"""
    parser = build_parser()
    args = parser.parse_args()
    if hasattr(args, "max_duration") and args.max_duration == 0:
        args.max_duration = None
    args.func(args)


if __name__ == "__main__":
    main()
