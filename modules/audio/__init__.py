"""语音包的轻量入口：导入物品播报时不加载旧录音、门铃助手。

这里属于 Python 包入口，不存放物品名称或播放逻辑。
旧 voice_assistant / doorbell / extract_name 只有被主动请求时才加载，
因此本 V2 候选无需修改或复制原项目的大型 voice_assiant.py。
"""
from importlib import import_module


__all__ = ["voice_assistant", "doorbell", "extract_name"]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    legacy_module = import_module(f"{__name__}.voice_assiant")
    value = getattr(legacy_module, name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
