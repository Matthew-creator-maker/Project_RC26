"""播报业务验证：假播放器 + 事件同步，无网络、无音响、无机器人。

在核验包的 robocup_embody 目录运行：
    python -m unittest discover -s tests -p test_speech_policy.py -v

测试用 Event 控制“正在播报但尚未播完”的状态，确认异步提交不会等
声音播放结束，而不是靠短暂 sleep 猜测线程的执行顺序。
"""

import contextlib
import importlib.util
import io
import threading
import unittest
from unittest import mock

from modules.audio import speech
from modules.audio.speech import (
    OBJECT_NAMES_ZH,
    CompetitionAnnouncements,
    build_recognition_text,
    get_chinese_name,
    normalize_label,
    normalize_station,
)


class FakeSpeaker:
    def __init__(self, results=None):
        self.texts = []
        self.results = list(results) if results is not None else []

    def __call__(self, text):
        self.texts.append(text)
        result = self.results.pop(0) if self.results else True
        if isinstance(result, Exception):
            raise result
        return result


class SpeechPolicyTests(unittest.TestCase):
    def setUp(self):
        self._stdout = contextlib.redirect_stdout(io.StringIO())
        self._stdout.__enter__()
        self.addCleanup(self._stdout.__exit__, None, None, None)

    def service(self, speaker, attempts=2):
        service = CompetitionAnnouncements(speaker, attempts, 0)
        self.addCleanup(service.close, False)
        return service

    def test_all_twelve_user_names_and_prefix(self):
        expected = {
            "biscuit": "饼干", "chip": "薯片", "lays": "乐事薯片",
            "cookie": "曲奇", "handwash": "洗手液", "dishsoap": "洗洁精",
            "water": "水", "sprite": "雪碧", "cola": "可乐",
            "orange juice": "芬达", "shampoo": "洗发水", "bread": "面包",
        }
        self.assertEqual(OBJECT_NAMES_ZH, expected)
        for label, name in expected.items():
            with self.subTest(label=label):
                self.assertEqual(get_chinese_name(label), name)
                self.assertEqual(build_recognition_text(label), "识别到" + name)

    def test_labels_and_stations_normalize_without_guessing(self):
        self.assertEqual(normalize_label(" Orange\t  JUICE \n"), "orange juice")
        self.assertEqual(normalize_station(" lm2 "), "LM2")
        self.assertEqual(build_recognition_text(" SPRITE "), "识别到雪碧")
        self.assertIsNone(build_recognition_text("orange_juice"))
        with self.assertRaises(TypeError):
            normalize_label(None)
        with self.assertRaises(ValueError):
            normalize_station("   ")

    def test_same_category_at_different_stations_is_announced_twice(self):
        speaker = FakeSpeaker()
        service = self.service(speaker)
        self.assertTrue(service.announce_scan("sprite", "LM2"))
        self.assertTrue(service.announce_scan("sprite", "LM9"))
        self.assertEqual(speaker.texts, ["识别到雪碧", "识别到雪碧"])
        self.assertEqual(service.announced_scan_by_station,
                         {"LM2": ["sprite"], "LM9": ["sprite"]})
        self.assertEqual(service.announced_scan_labels, {"sprite"})

    def test_same_station_multiple_frames_deduplicate_success(self):
        speaker = FakeSpeaker()
        service = self.service(speaker)
        self.assertTrue(service.begin_scan("LM2"))
        for _ in range(5):
            self.assertTrue(service.announce_scan(" SPRITE ", " lm2 "))
        self.assertEqual(speaker.texts, ["识别到雪碧"])

    def test_revisit_resets_visit_dedup_but_keeps_success_history(self):
        speaker = FakeSpeaker()
        service = self.service(speaker)
        service.begin_scan("LM2")
        service.announce_scan("sprite", "LM2")
        service.begin_scan("LM9")
        service.announce_scan("cola", "LM9")
        service.begin_scan("lm2")
        self.assertTrue(service.announce_scan("sprite", "LM2"))
        self.assertEqual(speaker.texts, ["识别到雪碧", "识别到可乐", "识别到雪碧"])
        self.assertEqual(service.announced_scan_by_station,
                         {"LM2": ["sprite"], "LM9": ["cola"]})

    def test_success_reports_are_defensive_copies(self):
        service = self.service(FakeSpeaker())
        service.announce_scan("sprite", "LM2")
        labels = service.announced_scan_labels
        report = service.announced_scan_by_station
        labels.add("cola")
        report["LM2"].append("cola")
        report["LM9"] = ["bread"]
        self.assertEqual(service.announced_scan_labels, {"sprite"})
        self.assertEqual(service.announced_scan_by_station, {"LM2": ["sprite"]})

    def test_scan_failure_does_not_register_and_next_frame_can_retry(self):
        speaker = FakeSpeaker([False, False, True])
        service = self.service(speaker)
        self.assertFalse(service.announce_scan("sprite", "LM2"))
        self.assertEqual(service.announced_scan_by_station, {})
        self.assertTrue(service.announce_scan("sprite", "LM2"))
        self.assertEqual(len(speaker.texts), 3)

    def test_scan_station_is_required_and_invalid_stations_do_not_play(self):
        speaker = FakeSpeaker()
        service = self.service(speaker)
        with self.assertRaises(TypeError):
            service.announce_scan("sprite")
        for station in (None, "", "  ", 2):
            with self.subTest(station=station):
                self.assertFalse(service.begin_scan(station))
                self.assertFalse(service.announce_scan("sprite", station))
        self.assertEqual(speaker.texts, [])

    def test_unknown_target_neither_plays_nor_starts_worker(self):
        speaker = FakeSpeaker()
        service = self.service(speaker)
        for label in ("orange_juice", "unknown", None):
            with self.subTest(label=label):
                self.assertFalse(service.announce_scan(label, "LM2"))
                self.assertIsNone(service.announce_before_grasp_async(label, "LM2"))
        self.assertEqual(speaker.texts, [])
        self.assertIsNone(service._worker)

    def test_grasp_is_announced_each_time_even_after_scan(self):
        speaker = FakeSpeaker()
        service = self.service(speaker)
        service.announce_scan("sprite", "LM2")
        first = service.announce_before_grasp_async("sprite", "LM2")
        second = service.announce_before_grasp_async("sprite", "LM2")
        service.close(wait=True)
        self.assertIs(first.result(timeout=1), True)
        self.assertIs(second.result(timeout=1), True)
        self.assertEqual(speaker.texts, ["识别到雪碧"] * 3)

    def test_async_submit_returns_before_first_playback_finishes(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_speaker(text):
            entered.set()
            return release.wait(timeout=2)

        service = self.service(blocked_speaker)
        self.addCleanup(release.set)
        future = service.announce_before_grasp_async("sprite", "LM2")
        self.assertTrue(entered.wait(timeout=1))
        self.assertFalse(future.done())
        # Future 已经返回；主流程现在可继续执行抓取，声音仍然没有播完。
        release.set()
        self.assertIs(future.result(timeout=1), True)
        service.close(wait=True)

    def test_busy_playback_does_not_block_next_grasp_submit(self):
        entered = threading.Event()
        release = threading.Event()
        submitted = threading.Event()
        holder = []
        texts = []

        def blocked_speaker(text):
            texts.append(text)
            if len(texts) == 1:
                entered.set()
                return release.wait(timeout=2)
            return True

        service = self.service(blocked_speaker)
        self.addCleanup(release.set)
        first = service.announce_before_grasp_async("sprite", "LM2")
        self.assertTrue(entered.wait(timeout=1))

        def submit_again():
            holder.append(service.announce_before_grasp_async("cola", "LM9"))
            submitted.set()

        submitter = threading.Thread(target=submit_again, daemon=True)
        submitter.start()
        self.assertTrue(submitted.wait(timeout=1), "提交被播放锁阻塞")
        self.assertFalse(first.done())
        self.assertFalse(holder[0].done())
        release.set()
        submitter.join(timeout=1)
        service.close(wait=True)
        self.assertIs(first.result(timeout=1), True)
        self.assertIs(holder[0].result(timeout=1), True)
        self.assertEqual(texts, ["识别到雪碧", "识别到可乐"])

    def test_sync_scan_playback_does_not_block_async_submit(self):
        entered = threading.Event()
        release = threading.Event()
        texts = []

        def blocked_speaker(text):
            texts.append(text)
            if len(texts) == 1:
                entered.set()
                return release.wait(timeout=2)
            return True

        service = self.service(blocked_speaker)
        self.addCleanup(release.set)
        scan = threading.Thread(target=service.announce_scan,
                                args=("sprite", "LM2"), daemon=True)
        scan.start()
        self.assertTrue(entered.wait(timeout=1))
        future = service.announce_before_grasp_async("cola", "LM9")
        self.assertFalse(future.done())
        release.set()
        scan.join(timeout=1)
        self.assertFalse(scan.is_alive())
        service.close(wait=True)
        self.assertIs(future.result(timeout=1), True)
        self.assertEqual(texts, ["识别到雪碧", "识别到可乐"])

    def test_background_queue_is_fifo_and_playback_does_not_overlap(self):
        active = 0
        maximum_active = 0
        texts = []
        guard = threading.Lock()

        def speaker(text):
            nonlocal active, maximum_active
            with guard:
                active += 1
                maximum_active = max(maximum_active, active)
                texts.append(text)
            with guard:
                active -= 1
            return True

        service = self.service(speaker)
        futures = [service.announce_before_grasp_async(label, "LM2")
                   for label in ("sprite", "cola", "bread")]
        service.close(wait=True)
        self.assertEqual(texts, ["识别到雪碧", "识别到可乐", "识别到面包"])
        self.assertEqual(maximum_active, 1)
        self.assertTrue(all(f.result(timeout=1) is True for f in futures))

    def test_background_failure_and_exception_become_false_then_next_task_runs(self):
        speaker = FakeSpeaker([RuntimeError("speaker disconnected"), False, True])
        service = self.service(speaker)
        failed = service.announce_before_grasp_async("sprite", "LM2")
        succeeded = service.announce_before_grasp_async("cola", "LM9")
        service.close(wait=True)
        self.assertIs(failed.result(timeout=1), False)
        self.assertIs(succeeded.result(timeout=1), True)
        self.assertEqual(service.announced_scan_labels, set())

    def test_only_real_boolean_true_confirms_playback(self):
        for result in (None, 1, "true", False):
            with self.subTest(result=result):
                service = self.service(FakeSpeaker([result]), attempts=1)
                self.assertFalse(service.announce_scan("sprite", "LM2"))
                self.assertEqual(service.announced_scan_labels, set())

    def test_close_false_returns_while_audio_is_still_running(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_speaker(text):
            entered.set()
            return release.wait(timeout=2)

        service = self.service(blocked_speaker)
        self.addCleanup(release.set)
        future = service.announce_before_grasp_async("sprite")
        self.assertTrue(entered.wait(timeout=1))
        service.close(wait=False)
        self.assertFalse(future.done())
        self.assertTrue(service._worker.daemon)
        release.set()
        service.close(wait=True)
        self.assertIs(future.result(timeout=1), True)

    def test_close_true_drains_tail_and_repeated_close_is_safe(self):
        speaker = FakeSpeaker()
        service = self.service(speaker)
        futures = [service.announce_before_grasp_async(label)
                   for label in ("sprite", "cola", "bread")]
        service.close(wait=False)
        service.close(wait=True)
        service.close(wait=True)
        self.assertEqual(speaker.texts, ["识别到雪碧", "识别到可乐", "识别到面包"])
        self.assertTrue(all(f.result(timeout=1) is True for f in futures))
        self.assertFalse(service._worker.is_alive())

    def test_closed_service_rejects_new_scan_and_grasp_without_starting_worker(self):
        speaker = FakeSpeaker()
        service = self.service(speaker)
        service.close(wait=True)
        self.assertFalse(service.begin_scan("LM2"))
        self.assertFalse(service.announce_scan("sprite", "LM2"))
        self.assertIsNone(service.announce_before_grasp_async("sprite", "LM2"))
        self.assertIsNone(service._worker)
        self.assertEqual(speaker.texts, [])

    def test_import_and_constructor_have_no_player_or_worker_side_effects(self):
        spec = importlib.util.spec_from_file_location("speech_policy_import_test", speech.__file__)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(threading, "Thread") as thread_class:
            spec.loader.exec_module(module)
            with mock.patch.object(module, "_default_speaker") as player:
                service = module.CompetitionAnnouncements()
                self.assertIsNone(service._worker)
                service.close(wait=True)
                player.assert_not_called()
            thread_class.assert_not_called()

    def test_invalid_retry_configuration_is_rejected(self):
        for attempts in (True, 1.5, 0):
            with self.subTest(attempts=attempts):
                with self.assertRaises((TypeError, ValueError)):
                    CompetitionAnnouncements(attempts=attempts)
        for delay in (True, "0", -1, float("inf"), float("nan")):
            with self.subTest(delay=delay):
                with self.assertRaises((TypeError, ValueError)):
                    CompetitionAnnouncements(retry_delay_seconds=delay)


if __name__ == "__main__":
    unittest.main()
