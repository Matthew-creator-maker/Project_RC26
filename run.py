"""Project entrypoint.

Examples:
    python run.py
    python run.py --execute --confirm-calibration
    python run.py --navigation-only --execute
"""
from pathlib import Path
import signal
import threading

from app.main import build_parser, main as mission_main
from modules.audio.speech_utils import SpeechServiceRuntime


def main(argv=None):
    """统一启动入口；机器人业务仍交给 app/main.py。

    默认预览、--help、仅导航、旧演示、未确认标定均不启动语音服务。
    实机比赛先确认服务可用，再进入原任务。with 保证正常结束和异常时
    清理自己启动的服务，原有机械臂/底盘 finally 先完成自己的清理。
    不调用预缓存工具，不修改比赛参数，不自动安装依赖。
    """
    args = build_parser().parse_args(argv)
    needs_speech = (args.execute and args.confirm_calibration
                    and not args.navigation_only and not args.legacy_demo)
    if not needs_speech:
        return mission_main(argv)

    # Ctrl+C 使用 Python 默认 KeyboardInterrupt，with 会完成清理。
    # 普通 SIGTERM 也转换为退出异常，让业务层 finally 和服务清理依次执行。
    # SIGKILL、断电不执行 Python 清理；不要把它们当作常规停止方式。
    old_handler = None
    can_handle_term = threading.current_thread() is threading.main_thread()
    if can_handle_term:
        old_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _on_terminate)
    try:
        with SpeechServiceRuntime(Path(__file__).resolve().parent):
            return mission_main(argv)
    finally:
        if can_handle_term:
            signal.signal(signal.SIGTERM, old_handler)


def _on_terminate(signum, frame):
    raise SystemExit(128 + signum)

if __name__ == "__main__":
    main()
