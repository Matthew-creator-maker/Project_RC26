"""比赛播报业务层：中文名称、按点位扫描去重、抓取时后台播报。

本文件合并 V1 的 object_names.py 和 recognition_announcer.py。
主流程只负责提供目标英文标签和触发时机；本文件不调用相机、机械臂
或导航程序。真正的合成和音频播放由 speech_utils.py 负责。

导入本文件、创建 CompetitionAnnouncements 实例都不会启动线程、
连接网络或创建缓存。第一次提交抓取播报时才启动一个后台线程。
"""

import math
import queue
import threading
import time
from concurrent.futures import Future
from numbers import Real
from typing import Callable, Dict, Optional, Set


# 左边必须与 YOLO 的实际类别一致，右边是希望机器人说出的中文名称。
# 只在播报时转换名称；检测、任务匹配仍继续使用原来的英文标签。
# orange juice 中间有空格，按队伍的约定播报“芬达”。
OBJECT_NAMES_ZH: Dict[str, str] = {
    "biscuit": "饼干",
    "chip": "薯片",
    "lays": "乐事薯片",
    "cookie": "曲奇",
    "handwash": "洗手液",
    "dishsoap": "洗洁精",
    "water": "水",
    "sprite": "雪碧",
    "cola": "可乐",
    "orange juice": "芬达",
    "shampoo": "洗发水",
    "bread": "面包",
}


def normalize_label(label: str) -> str:
    """统一英文标签大小写和多余空白，例如 Orange  JUICE -> orange juice。"""
    if not isinstance(label, str):
        raise TypeError("物品标签必须是字符串，例如 'sprite'。")
    return " ".join(label.strip().lower().split())


def get_chinese_name(label: str) -> Optional[str]:
    """已配置类别返回中文；未知类别返回 None，不猜测物品名称。"""
    return OBJECT_NAMES_ZH.get(normalize_label(label))


def build_recognition_text(label: str) -> Optional[str]:
    """扫描和抓取共用句式，例如 sprite -> 识别到雪碧。"""
    name = get_chinese_name(label)
    return None if name is None else "识别到" + name


def normalize_station(station: str) -> str:
    """统一点位大小写和多余空白；LM2 与 ' lm2 ' 视为同一点位。

    点位必须提供真实名称，不能用空字符串代替，否则无法按点位去重。
    不删除名称内部正常的空格，不把不同的真实点位猜测成同一个点位。
    """
    if not isinstance(station, str):
        raise TypeError("扫描点位必须是字符串，例如 'LM2'。")
    result = " ".join(station.strip().upper().split())
    if not result:
        raise ValueError("扫描点位不能为空。")
    return result


def _default_speaker(text: str) -> bool:
    """真正需要出声时才导入播放层，导入业务层本身不会访问音频设备。"""
    from .speech_utils import speak_text_blocking

    return speak_text_blocking(text)


class CompetitionAnnouncements:
    """一轮比赛使用一个实例：扫描阻塞确认，抓取后台排队播放。

    speaker 是“播放结束后才返回”的函数，只有返回 True 才算成功。
    测试时可以替换成假播放器，不需要音响和 TTS 服务。
    真实播放函数必须自带网络和进程超时，不能无限等待硬件。

    三种锁职责不同，避免主流程等待后台播报：
    * _state_lock 只保护少量状态和队列提交，不覆盖播放或网络请求；
    * _scan_lock 保护一次访问中的“检查 -> 播放 -> 登记”扫描步骤；
    * _playback_lock 串行播放句子，防止同步扫描和后台语音重叠。
    抓取提交函数只使用 _state_lock，绝不获取另两种锁。
    """

    def __init__(
        self,
        speaker: Optional[Callable[[str], bool]] = None,
        attempts: int = 2,
        retry_delay_seconds: float = 0.15,
    ) -> None:
        if speaker is not None and not callable(speaker):
            raise TypeError("speaker 必须是可调用的阻塞播放函数，或不填写。")
        if isinstance(attempts, bool) or not isinstance(attempts, int):
            raise TypeError("attempts 必须是整数，例如 2。")
        if attempts < 1:
            raise ValueError("attempts 至少为 1。")
        if isinstance(retry_delay_seconds, bool) or not isinstance(
            retry_delay_seconds, Real
        ):
            raise TypeError("retry_delay_seconds 必须是非负数字，例如 0.15。")
        if not math.isfinite(retry_delay_seconds) or retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds 必须是有限的非负数字。")

        self._speaker = _default_speaker if speaker is None else speaker
        self._attempts = attempts
        self._retry_delay_seconds = float(retry_delay_seconds)
        self._state_lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._playback_lock = threading.Lock()
        # 当前访问的去重记录与整场成功历史分开，重访不会丢掉统计。
        self._scan_visit_labels: Dict[str, Set[str]] = {}
        self._scan_success_history: Dict[str, Set[str]] = {}
        self._tasks = queue.Queue()
        self._stop = object()
        self._worker: Optional[threading.Thread] = None
        self._closed = False

    @property
    def announced_scan_labels(self) -> Set[str]:
        """整轮成功扫描播报类别的副本，仅用于汇总，不能据此跨点位去重。"""
        with self._state_lock:
            return {
                label
                for labels in self._scan_success_history.values()
                for label in labels
            }

    @property
    def announced_scan_by_station(self) -> Dict[str, list]:
        """成功历史的独立快照，例如 {'LM2': ['sprite'], 'LM9': ['sprite']}。

        begin_scan 清除当前访问的去重集合，历史报告仍保留成功记录。
        返回新字典和新列表，外部修改报告不会改变内部记录。
        """
        with self._state_lock:
            return {
                station: sorted(labels)
                for station, labels in sorted(self._scan_success_history.items())
            }

    def begin_scan(self, station: str) -> bool:
        """每次到点准备扫描时调用一次，开启该点位的一次新访问。

        例如第一次 LM2 扫描已播过雪碧，离开后又回 LM2：先调用本方法，
        新访问再次发现雪碧就会播报。不要在每帧调用，否则每帧都会重置。
        即使主流程未调用本方法，跨点位也会分别播报；只有重访重置依赖它。
        """
        normalized_station = self._resolve_station(station)
        if normalized_station is None:
            return False
        # 与扫描共用此锁，避免上一访问正在播放时就被新访问重置。
        # 此锁不会被异步提交函数获取，所以不拖慢抓取任务提交。
        with self._scan_lock:
            with self._state_lock:
                if self._closed:
                    return False
                self._scan_visit_labels[normalized_station] = set()
        return True

    def announce_scan(self, label: str, station: str) -> bool:
        """扫描阶段：同一次访问、同点同类成功播一次，不同点位分别播。

        station 是必填参数。LM2 的 sprite 成功不会跳过 LM9 的 sprite。
        本方法等待播报完成；失败不登记，后续再识别到时仍可尝试。
        已在当前访问成功播过则直接返回 True，不重复播放。
        """
        normalized_station = self._resolve_station(station)
        target = self._resolve_target(label, "扫描", station)
        if normalized_station is None or target is None:
            return False
        normalized_label, text = target
        with self._scan_lock:
            with self._state_lock:
                if self._closed:
                    return False
                visit = self._scan_visit_labels.setdefault(normalized_station, set())
                if normalized_label in visit:
                    return True
            # 不持状态锁播放；后台抓取提交可立即进入自己的队列。
            success = self._speak_with_retry(text, "扫描", normalized_station)
            if success:
                with self._state_lock:
                    self._scan_visit_labels[normalized_station].add(normalized_label)
                    self._scan_success_history.setdefault(
                        normalized_station, set()
                    ).add(normalized_label)
            return success

    def announce_before_grasp_async(
        self, label: str, station: Optional[str] = None
    ) -> Optional[Future]:
        """稳定采样完成后提交本次目标播报，立即返回供主流程继续抓取。

        每次确认一个抓取任务调用一次；此阶段不去重，不播画面其他物品。
        Future 表示后台任务，可供测试或事后查看结果，主流程不要立即
        调用 .result()、.join() 或等待事件，否则又会等语音结束再抓取。
        返回 None 表示标签无效或服务已关闭，表示本次没有提交播报。
        返回 Future 只表示已提交，不能当作“已经播放成功”。

        一个后台线程按先后顺序播放，因此抓取与语音并行，但多个语音
        句子不会互相重叠。即使播放器正忙，本函数也不等待播放器的锁。
        """
        target = self._resolve_target(label, "抓取前", station)
        if target is None:
            return None
        _, text = target
        with self._state_lock:
            if self._closed:
                print("[播报/抓取前] 服务已关闭，本次未提交。", flush=True)
                return None
            if self._worker is None:
                # daemon 使异常退出时无需等待网络/音频线程结束。
                # 正常结束仍必须 close(wait=True)，保证队列尾句播放完毕。
                self._worker = threading.Thread(
                    target=self._run_worker,
                    name="robocup-speech-worker",
                    daemon=True,
                )
                self._worker.start()
            future = Future()
            self._tasks.put((future, text, "抓取前", station))
        return future

    def close(self, wait: bool = True) -> None:
        """关闭后台队列；可重复调用，关闭后拒绝新扫描/抓取播报。

        正常任务结束：close(wait=True) 等已提交句子播放完成，然后退出
        工作线程。放在整轮结束位置，不要放在每次抓取之前或之后。
        异常清理：close(wait=False) 只提交队尾停止标记，立即返回；
        若主进程随即退出，daemon 线程的尾句可能被截断。这种清理方式
        不会为了等待播报而卡住异常退出，也不控制或停止机械臂。

        两种方式均不取消已提交任务。先调用 wait=False 后仍可在正常
        条件下调用 wait=True，等待同一线程收尾。不要从播放器回调里
        等待自身线程；这里会跳过对当前线程的 join，避免自锁。
        """
        with self._state_lock:
            if not self._closed:
                self._closed = True
                if self._worker is not None:
                    self._tasks.put(self._stop)
            worker = self._worker
        # 不持状态锁等待，否则工作线程或其他提交者可能无法读取状态。
        if wait and worker is not None and worker is not threading.current_thread():
            worker.join()

    def _run_worker(self) -> None:
        """唯一后台消费者：FIFO 顺序处理任务，停止标记位于已有任务之后。"""
        while True:
            task = self._tasks.get()
            try:
                if task is self._stop:
                    return
                future, text, stage, station = task
                # 允许调用者在任务尚未执行时取消 Future，不播放被取消任务。
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = self._speak_with_retry(text, stage, station)
                except Exception as exc:
                    # 单条意外错误不退出线程，不影响后续已提交任务。
                    print("[播报/后台] 任务异常：{}".format(exc), flush=True)
                    result = False
                future.set_result(result is True)
                print(
                    "{} 后台播放结果：{}".format(
                        self._location(stage, station),
                        "成功" if result is True else "失败",
                    ),
                    flush=True,
                )
            finally:
                self._tasks.task_done()

    @staticmethod
    def _resolve_station(station: str) -> Optional[str]:
        try:
            return normalize_station(station)
        except (TypeError, ValueError) as exc:
            print("[播报/扫描] 点位无效：{}；本次不播报。".format(exc), flush=True)
            return None

    @staticmethod
    def _location(stage: str, station: Optional[str]) -> str:
        return "[播报/{}/点位 {}]".format(
            stage, "未提供" if station is None else station
        )

    def _resolve_target(self, label: str, stage: str, station: Optional[str]):
        prefix = self._location(stage, station)
        try:
            normalized_label = normalize_label(label)
            text = build_recognition_text(normalized_label)
        except TypeError as exc:
            print("{} 标签无效：{}；本次不播报。".format(prefix, exc), flush=True)
            return None
        if text is None:
            print(
                "{} 未配置中文名称：{!r}；本次不播报，请检查名称映射。".format(
                    prefix, label
                ),
                flush=True,
            )
            return None
        return normalized_label, text

    def _speak_with_retry(self, text: str, stage: str, station: Optional[str]) -> bool:
        """播放结束严格检查 True；异常算失败，有限重试，不修改扫描记录。"""
        prefix = self._location(stage, station)
        with self._playback_lock:
            for index in range(self._attempts):
                print(
                    "{} {}（第 {}/{} 次）".format(
                        prefix, text, index + 1, self._attempts
                    ),
                    flush=True,
                )
                try:
                    result = self._speaker(text)
                except Exception as exc:
                    print("{} 播放异常：{}".format(prefix, exc), flush=True)
                    result = False
                if result is True:
                    return True
                if index + 1 < self._attempts and self._retry_delay_seconds > 0:
                    time.sleep(self._retry_delay_seconds)
        print("{} 未确认播放成功；本次返回 False。".format(prefix), flush=True)
        return False
