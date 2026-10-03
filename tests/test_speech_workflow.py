"""直接执行候选 main.py 的真实方法体，使用内存替身验证并行时机。

从 AST 提取函数可以避开导航 SDK、相机、机械臂的导入。函数体来自
实际候选文件，不另外编写一个模仿的抓取流程。Event 是测试屏障，
用来证明声音尚未结束时抓取已开始；真实 main 不等待这些测试事件。
"""
from __future__ import annotations

import ast
import contextlib
import io
import math
import statistics
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules.audio.speech import CompetitionAnnouncements


def load_actual_function(name, namespace=None, class_name=None):
    tree = ast.parse((ROOT / "app/main.py").read_text(encoding="utf-8"))
    body = tree.body
    if class_name:
        body = next(node for node in body if isinstance(node, ast.ClassDef)
                    and node.name == class_name).body
    function = next(node for node in body if isinstance(node, ast.FunctionDef)
                    and node.name == name)
    unit = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
    values = {"time": time}
    values.update(namespace or {})
    exec(compile(ast.fix_missing_locations(unit), str(ROOT / "app/main.py"), "exec"), values)
    return values[name]


class Vector(list):
    def tolist(self):
        return list(self)

    def __sub__(self, other):
        return Vector(a - b for a, b in zip(self, other))


class GraspWorkflowTests(unittest.TestCase):
    def make_scanner(self, speaker):
        events = []
        samples = {"count": 0}
        target = {"label": "sprite", "confidence": 0.95, "stable": True,
                  "localization_ok": True, "xyz_camera": [0.1, 0.2, 0.3]}
        nearby = dict(target, label="cola", confidence=0.5)

        def process_frame(*_args):
            samples["count"] += 1
            events.append("sample")
            return [nearby, target]

        def execute(_config, label, xyz, *_args, **_kwargs):
            events.append("grasp_execute")
            return {"target": label, "xyz_camera": xyz.tolist()}

        module = SimpleNamespace(
            np=SimpleNamespace(asarray=lambda values, **_kw: Vector(values),
                               linalg=SimpleNamespace(norm=lambda values: math.sqrt(sum(v * v for v in values)))),
            median_point=lambda points: Vector(statistics.median(row[i] for row in points) for i in range(3)),
            finite_vector=lambda values, *_args: list(values),
            find_stable_target=lambda results, label: next((item for item in results if item["label"] == label), None),
            execute_prelocalized_grasp=execute,
        )
        service = CompetitionAnnouncements(speaker=speaker, attempts=1, retry_delay_seconds=0)
        scanner = SimpleNamespace(
            vision_config={"grasp_test": {"stable_samples": 10}}, module=module,
            announcements=service,
            arm=SimpleNamespace(movej=lambda _pose: events.append("observation_move"),
                                get_current_pose=lambda: [0.0] * 6),
            camera=SimpleNamespace(get_aligned_frames=lambda **_kw: {"color_image": None, "depth_frame": None, "intrinsics": None}),
            pipeline=SimpleNamespace(reset_stability=lambda: None, process_frame=process_frame),
            _ensure_arm=lambda guard: guard(), _ensure_camera=lambda guard: guard(),
            _vision_config_for_station=lambda _station: {},
            _observation_pose_for_station=lambda _station: [0.0] * 6,
            _wait_with_guard=lambda _seconds, guard: guard(), settle_seconds=0, show=False,
        )

        def guarded(guard, call):
            guard()
            result = call()
            guard()
            return result

        scanner._guarded = guarded
        return scanner, events, samples

    def run_grasp(self, scanner, guard=lambda: None):
        return load_actual_function("_grasp_station_from_observation", class_name="CompetitionScanner")(
            scanner, "LM9", 10, 10, guard
        )

    def test_grasp_executes_after_ten_samples_while_audio_is_unfinished(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        texts = []

        def speaker(text):
            texts.append(text)
            self.assertEqual(samples["count"], 10)
            started.set()
            release.wait(2)
            finished.set()
            return True

        scanner, events, samples = self.make_scanner(speaker)
        actual_execute = scanner.module.execute_prelocalized_grasp

        def execute(*args, **kwargs):
            # 仅测试替身等待后台进入屏障，真实抓取函数没有这个等待。
            self.assertTrue(started.wait(1), "后台语音没有启动")
            self.assertFalse(finished.is_set(), "抓取在声音结束后才开始")
            return actual_execute(*args, **kwargs)

        scanner.module.execute_prelocalized_grasp = execute
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                result = self.run_grasp(scanner, guard=lambda: events.append("guard"))
                self.assertFalse(finished.is_set())
                self.assertEqual(result["target"], "sprite")
                self.assertEqual(texts, ["识别到雪碧"])
                self.assertEqual(events.count("sample"), 10)
                self.assertEqual(events.count("grasp_execute"), 1)
                self.assertLess(events.index("observation_move"), events.index("sample"))
            finally:
                release.set()
                scanner.announcements.close(wait=True)

    def test_guard_after_submission_can_still_prevent_grasp(self):
        scanner, events, _samples = self.make_scanner(lambda _text: True)
        scanner.announcements.close()
        submitted = {"value": False}

        def submit(_label, **_kwargs):
            submitted["value"] = True
            return object()

        scanner.announcements = SimpleNamespace(announce_before_grasp_async=submit)

        def guard():
            if submitted["value"]:
                raise RuntimeError("提交语音后点位检查失败")

        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "点位"):
                self.run_grasp(scanner, guard)
        self.assertNotIn("grasp_execute", events)

    def test_background_failure_does_not_change_grasp_result(self):
        scanner, events, _samples = self.make_scanner(lambda _text: False)
        with contextlib.redirect_stdout(io.StringIO()) as log:
            result = self.run_grasp(scanner)
            scanner.announcements.close(wait=True)
        self.assertEqual(result["target"], "sprite")
        self.assertIn("grasp_execute", events)
        self.assertIn("后台播放结果：失败", log.getvalue())
        self.assertEqual(scanner.announcements.announced_scan_labels, set())

    def test_ik_rejection_propagates_without_waiting_for_audio(self):
        release, finished = threading.Event(), threading.Event()

        def speaker(_text):
            release.wait(2)
            finished.set()
            return True

        scanner, _events, _samples = self.make_scanner(speaker)

        def reject(*_args, **_kwargs):
            raise RuntimeError("原运动检查：IK 无有效解")

        scanner.module.execute_prelocalized_grasp = reject
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                with self.assertRaisesRegex(RuntimeError, "IK"):
                    self.run_grasp(scanner)
                self.assertFalse(finished.is_set())
            finally:
                release.set()
                scanner.announcements.close(wait=True)


class MissionScanReportTests(unittest.TestCase):
    def test_actual_mission_announces_same_label_at_two_stations_and_records_both(self):
        texts = []
        service = CompetitionAnnouncements(speaker=lambda text: texts.append(text) or True)
        plan = SimpleNamespace(start_station="LM1", scan_route=["LM2", "LM9"],
                               scan_nav_stations={}, home_station="LM9", grasp_priority=[],
                               grasp_retry_route=[], score_station="LM7", exit_station="LM8")
        args = SimpleNamespace(execute=True, confirm_calibration=True, navigation_only=False,
                               scan_seconds=6, match_seconds=600, grasp_timeout_seconds=8,
                               grasp_sample_timeout_seconds=20)

        class Navigator:
            station = "LM1"
            def connect(self): pass
            def current_station(self): return self.station
            def go_to_station(self, destination): self.station = destination
            def assert_at(self, station):
                if self.station != station: raise RuntimeError("模拟底盘不在要求点位")
            def close(self): pass

        class Scanner:
            def transport(self, guard): guard()
            def scan_station(self, station, _seconds, guard):
                guard()
                return [{"label": "sprite"}, {"label": "sprite"}]
            def finish_recognition(self): pass
            def close(self): pass

        nav, scanner = Navigator(), Scanner()
        rail = SimpleNamespace(lowered=False, travel=lambda: None)
        namespace = {
            "Navigator": Navigator, "GraspProgram": object, "CompetitionScanner": Scanner,
            "CompetitionAnnouncements": CompetitionAnnouncements,
            "competition_plan": lambda _config: plan,
            "GRASP_SAMPLE_TIMEOUT_SECONDS": 20,
            "_remaining": lambda *_args: 100,
            "_navigate_or_skip": lambda navigator, destination, _phase: (navigator.go_to_station(destination) or True),
            "_safe_close": lambda program: None if program is None else program.close(),
            "sys": sys,
        }
        run = load_actual_function("run_competition", namespace=namespace)
        with contextlib.redirect_stdout(io.StringIO()):
            result = run({"navigation": {}}, args,
                         navigator_factory=lambda _cfg: nav,
                         scanner_factory=lambda _cfg, _args: scanner,
                         rail_factory=lambda: rail, announcements=service)
        self.assertEqual(texts, ["识别到雪碧", "识别到雪碧"])
        self.assertEqual(result["object_station"], {"sprite": "LM2"})
        self.assertEqual(result["object_stations"], {"sprite": ["LM2", "LM9"]})
        self.assertEqual(result["announced_by_station"], {"LM2": ["sprite"], "LM9": ["sprite"]})
        self.assertEqual(result["announced"], ["sprite"])
        self.assertIsNone(service.announce_before_grasp_async("sprite", station="LM9"))


class ImportIsolationTests(unittest.TestCase):
    def test_imports_and_construction_do_not_start_audio_or_threads(self):
        script = '''
import sys
from unittest.mock import patch
with patch("urllib.request.urlopen", side_effect=AssertionError("禁止 HTTP")), \\
     patch("subprocess.run", side_effect=AssertionError("禁止播放器")), \\
     patch("pathlib.Path.mkdir", side_effect=AssertionError("禁止缓存创建")), \\
     patch("threading.Thread.start", side_effect=AssertionError("禁止自动启动线程")):
    import modules.audio.speech
    import modules.audio.speech_utils
    modules.audio.speech.CompetitionAnnouncements()
    modules.audio.speech_utils.ObjectVoice()
    assert "modules.audio.voice_assiant" not in sys.modules
    assert "pyaudio" not in sys.modules
    assert "webrtcvad" not in sys.modules
'''
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_package_keeps_legacy_exports_lazy(self):
        import modules.audio as audio
        exports = {name: object() for name in audio.__all__}
        with patch.object(audio, "import_module", return_value=SimpleNamespace(**exports)) as loader:
            try:
                for name, value in exports.items():
                    self.assertIs(getattr(audio, name), value)
                    self.assertIs(getattr(audio, name), value)
                self.assertEqual(loader.call_count, 3)
            finally:
                for name in exports:
                    audio.__dict__.pop(name, None)


if __name__ == "__main__":
    unittest.main()
