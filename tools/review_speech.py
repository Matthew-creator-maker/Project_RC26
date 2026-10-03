"""默认只打印 V2 流程：跨点播报，以及语音未结束时抓取继续。

本工具不导入机器人主函数；点位、目标、抓取都只是演示数据。
--real-audio 才启用声音；--warm-cache 才联网准备音频而不播放。
"""
from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from modules.audio.speech import OBJECT_NAMES_ZH, CompetitionAnnouncements, build_recognition_text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="V2 默认只打印，不连接机器人或音响")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--label", help="例如 sprite 或带引号的 orange juice")
    selection.add_argument("--all", action="store_true", help="模拟全部 12 类")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--real-audio", action="store_true", help="手动启用真实语音；仍不控制机器人")
    modes.add_argument("--warm-cache", action="store_true", help="只准备音频，默认预缓存全部 12 句")
    args = parser.parse_args(argv)
    labels = list(OBJECT_NAMES_ZH) if args.all or (args.warm_cache and not args.label) else [args.label or "sprite"]
    if args.warm_cache:
        from modules.audio.speech_utils import ObjectVoice
        voice = ObjectVoice()
        success = True
        for label in labels:
            text = build_recognition_text(label)
            ok = text is not None and voice.cache_text(text)
            success = ok and success
            print(f"[缓存准备] {text or label}：{'成功' if ok else '失败'}")
        return 0 if success else 1

    spoken = []
    release_audio = threading.Event()
    async_mode = {"enabled": False}

    def print_only_speaker(text):
        spoken.append(text)
        print(f"[模拟声音开始] {text}")
        if async_mode["enabled"]:
            # 只是核验屏障：故意保持“声音未结束”，证明主线程还能继续模拟抓取。
            # 真实 main.py 没有这个事件，也不会等待 Future 结果。
            release_audio.wait(2)
        print(f"[模拟声音结束] {text}")
        return True

    service = CompetitionAnnouncements() if args.real_audio else CompetitionAnnouncements(speaker=print_only_speaker)
    success = True
    print("[真实声音模式]" if args.real_audio else "[默认模拟模式：不联网、不出声、不连接机器人]")
    try:
        print("\n--- LM2：一次到点扫描，同类多帧只播一次 ---")
        service.begin_scan("LM2")
        for label in labels:
            success = service.announce_scan(label, station="LM2") and success
        success = service.announce_scan(labels[0], station="LM2") and success

        print("\n--- LM9：发现同一类别，仍然播报 ---")
        service.begin_scan("LM9")
        success = service.announce_scan(labels[0], station="LM9") and success

        print("\n--- 再到 LM2：新的扫描访问也可以播报 ---")
        service.begin_scan("LM2")
        success = service.announce_scan(labels[0], station="LM2") and success

        print("\n--- LM9：抓取前采样完成，后台播报与模拟抓取并行 ---")
        async_mode["enabled"] = True
        future = service.announce_before_grasp_async(labels[0], station="LM9")
        print("[模拟抓取] 已立即进入抓取流程，没有等待声音结束。")
        if future is None:
            success = False
        elif not args.real_audio:
            print(f"[并行核验] 模拟抓取开始时，语音已完成？{future.done()}（预期 False）")
        release_audio.set()
        # 正常任务结束才收尾；真实 main 也只在整轮已经离场后等待最后一句。
        service.close(wait=True)
        if future is not None:
            success = (future.result() is True) and success  # 此处已经是演示收尾，不是抓取前。
        print("按点位成功播报记录：", service.announced_scan_by_station)
        if not args.real_audio:
            print(f"模拟声音总次数：{len(spoken)}，预期 {len(labels) + 3}。")
    finally:
        release_audio.set()
        service.close(wait=False)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
