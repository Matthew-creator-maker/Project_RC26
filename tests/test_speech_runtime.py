"""启动入口的故障与清理测试：所有服务/机器人/播放器均使用替身。"""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import unittest
from unittest.mock import MagicMock, patch
from urllib import error

from modules.audio import speech_utils as audio
import run as entry


INFO = {"service": audio.TTS_SERVICE_ID, "protocol": audio.TTS_PROTOCOL, "pid": 1234}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = audio.SpeechServiceRuntime(Path(__file__).resolve().parents[1])
        self.child = MagicMock(pid=1234)
        self.child.poll.return_value = None
        self.child.wait.return_value = 0
        self.which = patch.object(audio.shutil, "which", return_value="/usr/bin/mpg123")
        self.spec = patch.object(audio.importlib.util, "find_spec", return_value=object())
        self.popen = patch.object(audio.subprocess, "Popen", return_value=self.child)
        self.spawn = self.popen.start()
        self.which.start()
        self.spec.start()
        self.addCleanup(self.popen.stop)
        self.addCleanup(self.spec.stop)
        self.addCleanup(self.which.stop)
        self.addCleanup(self.runtime.close)

    def test_construction_has_no_process_or_cache_side_effects(self):
        self.spawn.assert_not_called()
        self.assertIsNone(self.runtime._saved_proxy_env)

    def test_existing_service_preserved_and_proxy_entries_restored(self):
        original = {"NO_PROXY": "example.com", "no_proxy": "internal",
                    "HTTPS_PROXY": "http://127.0.0.1:7890"}
        # 使用普通字典模拟 Linux 环境；Windows 的 os.environ 不区分键名大小写。
        with patch.object(os, "environ", original.copy()), patch.object(
                self.runtime, "_probe", return_value=INFO):
            with self.runtime:
                self.assertIn("example.com", os.environ["NO_PROXY"])
                self.assertIn("internal", os.environ["NO_PROXY"])
                self.assertIn("127.0.0.1", os.environ["no_proxy"])
                self.assertEqual(os.environ["HTTPS_PROXY"], original["HTTPS_PROXY"])
            self.assertEqual(dict(os.environ), original)
        self.spawn.assert_not_called()
        self.child.terminate.assert_not_called()

    def test_own_service_started_with_current_interpreter_and_closed(self):
        with patch.object(self.runtime, "_probe", side_effect=[None, INFO]), self.runtime:
            self.assertEqual(self.spawn.call_args.args[0],
                             [audio.sys.executable, "-B", "-m", "modules.audio.chat"])
            self.assertNotIn("shell", self.spawn.call_args.kwargs)
        self.child.terminate.assert_called_once()
        self.child.wait.assert_called_once_with(timeout=5)
        self.child.kill.assert_not_called()

    def test_body_failure_preserved_and_child_cleaned(self):
        with patch.object(self.runtime, "_probe", side_effect=[None, INFO]):
            with self.assertRaisesRegex(RuntimeError, "robot failure"):
                with self.runtime:
                    raise RuntimeError("robot failure")
        self.child.terminate.assert_called_once()

    def test_interrupt_during_startup_also_cleans_child_and_environment(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
                self.runtime, "_probe", side_effect=[None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.__enter__()
            self.assertEqual(dict(os.environ), {})
        self.child.terminate.assert_called_once()

    def test_missing_player_fails_before_process_or_mission(self):
        with patch.object(audio.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "mpg123"):
                self.runtime.__enter__()
        self.spawn.assert_not_called()

    def test_missing_dependency_fails_before_spawn(self):
        with patch.object(self.runtime, "_probe", return_value=None), patch.object(
                audio.importlib.util, "find_spec", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "requirements-speech"):
                self.runtime.__enter__()
        self.spawn.assert_not_called()

    def test_conflicting_service_never_terminated(self):
        with patch.object(self.runtime, "_probe", side_effect=RuntimeError("conflict")):
            with self.assertRaisesRegex(RuntimeError, "conflict"):
                self.runtime.__enter__()
        self.spawn.assert_not_called()
        self.child.terminate.assert_not_called()

    def test_child_exits_before_readiness(self):
        self.child.poll.return_value = 1
        with patch.object(self.runtime, "_probe", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "启动后退出"):
                self.runtime.__enter__()
        self.child.terminate.assert_not_called()

    def test_port_race_cannot_be_mistaken_for_own_process(self):
        with patch.object(self.runtime, "_probe", side_effect=[None, dict(INFO, pid=999)]):
            with self.assertRaisesRegex(RuntimeError, "端口竞争"):
                self.runtime.__enter__()
        self.child.terminate.assert_called_once()

    def test_startup_timeout_cleans_process(self):
        with patch.object(self.runtime, "_probe", return_value=None), patch.object(
                audio.time, "monotonic", side_effect=[0, 21]):
            with self.assertRaisesRegex(RuntimeError, "启动超时"):
                self.runtime.__enter__()
        self.child.terminate.assert_called_once()

    def test_slow_child_is_killed_only_after_grace_period(self):
        self.child.wait.side_effect = [subprocess.TimeoutExpired("tts", 5), 0]
        with patch.object(self.runtime, "_probe", side_effect=[None, INFO]), self.runtime:
            pass
        self.child.kill.assert_called_once()
        self.assertEqual([call.kwargs["timeout"] for call in self.child.wait.call_args_list], [5, 2])
        self.runtime.close()
        self.child.terminate.assert_called_once()

    def test_cleanup_warning_does_not_mask_original_error(self):
        self.child.terminate.side_effect = OSError("already gone")
        with patch.object(self.runtime, "_probe", side_effect=[None, INFO]):
            with self.assertRaisesRegex(ValueError, "mission error"):
                with self.runtime:
                    raise ValueError("mission error")

    def test_startup_never_synthesizes_or_modifies_cache(self):
        with patch.object(audio, "write_bytes_atomic") as write, patch.object(
                audio.ObjectVoice, "cache_text") as cache, patch.object(
                self.runtime, "_probe", side_effect=[None, INFO]), self.runtime:
            pass
        write.assert_not_called()
        cache.assert_not_called()

    def test_probe_checks_identity_without_proxy(self):
        response = MagicMock(status=200)
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(INFO).encode()
        with patch.object(self.runtime._opener, "open", return_value=response) as get:
            self.assertEqual(self.runtime._probe(), INFO)
        get.assert_called_once_with(audio.TTS_HEALTH_URL, timeout=2.0)
        for bad in (dict(INFO, service="other"), dict(INFO, protocol=True),
                    dict(INFO, pid=False), {}, [], "not json"):
            response.read.return_value = json.dumps(bad).encode()
            with patch.object(self.runtime._opener, "open", return_value=response):
                with self.assertRaises(RuntimeError):
                    self.runtime._probe()

    def test_probe_refused_is_absent_but_404_or_timeout_is_not(self):
        with patch.object(self.runtime._opener, "open",
                          side_effect=error.URLError(ConnectionRefusedError())):
            self.assertIsNone(self.runtime._probe())
        for exc in (error.HTTPError(audio.TTS_HEALTH_URL, 404, "Not found", {}, io.BytesIO()),
                    error.URLError(TimeoutError())):
            with patch.object(self.runtime._opener, "open", side_effect=exc):
                with self.assertRaises(RuntimeError):
                    self.runtime._probe()


class EntrypointTests(unittest.TestCase):
    def test_preview_help_navigation_and_unconfirmed_do_not_start_service(self):
        variants = ([], ["--execute"], ["--navigation-only", "--execute"],
                    ["--legacy-demo", "--execute", "--confirm-calibration"])
        with patch.object(entry, "mission_main", return_value="preview") as mission, patch.object(
                entry, "SpeechServiceRuntime") as runtime:
            for args in variants:
                self.assertEqual(entry.main(args), "preview")
            with self.assertRaises(SystemExit) as caught:
                entry.main(["--help"])
            self.assertEqual(caught.exception.code, 0)
            runtime.assert_not_called()
            self.assertEqual(mission.call_count, len(variants))

    def test_mission_failure_cleans_context_and_restores_signal(self):
        args = ["--execute", "--confirm-calibration"]
        old = signal.getsignal(signal.SIGTERM)
        with patch.object(entry, "SpeechServiceRuntime") as runtime, patch.object(
                entry, "mission_main", side_effect=ValueError("robot error")) as mission:
            runtime.return_value.__exit__.return_value = False
            with self.assertRaisesRegex(ValueError, "robot error"):
                entry.main(args)
            mission.assert_called_once_with(args)
            runtime.return_value.__exit__.assert_called_once()
        self.assertEqual(signal.getsignal(signal.SIGTERM), old)

    def test_service_failure_prevents_mission(self):
        with patch.object(entry, "SpeechServiceRuntime") as runtime, patch.object(
                entry, "mission_main") as mission:
            runtime.return_value.__enter__.side_effect = RuntimeError("startup failed")
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                entry.main(["--execute", "--confirm-calibration"])
            mission.assert_not_called()

    def test_sigterm_uses_exception_for_normal_finally_cleanup(self):
        with self.assertRaises(SystemExit) as caught:
            entry._on_terminate(signal.SIGTERM, None)
        self.assertEqual(caught.exception.code, 128 + signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
