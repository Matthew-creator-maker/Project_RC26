'''
原版中的speak不能非阻塞播放，因此额外封装、一个功能单元

后续也不要使用 原版的speak
只有都用这个脚本中的函数时，
他们共用一个锁，才能避免语音重叠播放
'''


import threading


_speak_lock = threading.Lock()


def speak_blocking(text: str) -> bool:
    """阻塞播报，使用 voice_assistant 单例。"""
    try:
        from modules.audio.voice_assiant import voice_assistant

        with _speak_lock:
            voice_assistant.speak(text)
        return True
    except Exception as exc:
        print(f'播报失败：{exc}')
        return False


def speak_async(text: str) -> threading.Thread:
    """非阻塞播报，后台线程使用 voice_assistant 单例。"""

    def _worker() -> None:
        speak_blocking(text)

    thread = threading.Thread(
        target=_worker,
        name="voice-speak-async",
        daemon=True,
    )
    thread.start()
    return thread
