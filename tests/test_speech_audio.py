"""V2 音频工具与 TTS 处理测试：全程使用替身，不联网、不发声、不启动服务。

该文件汇总缓存、播放器、HTTP 请求参数和本地 TTS 处理逻辑的测试。
FastAPI 和 Edge TTS 使用测试替身，因此不验证框架请求校验或真实音质。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

"""缓存纯函数测试：不请求网络、不启动 TTS、不播放声音。"""

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.audio.speech_utils import (
    cache_filename,
    is_probably_mp3,
    read_valid_mp3,
    validate_speed,
    write_bytes_atomic,
)


# 只用于测试格式分支，不是供扬声器播放的真实语音。
FAKE_MP3 = b"\xff\xfb\x90\x64" + b"\x00" * 413


class SpeechCacheTests(unittest.TestCase):
    def test_same_parameters_have_same_safe_filename(self):
        name = cache_filename("识别到雪碧/../", "zh-CN-XiaoxiaoNeural", 1.2)
        self.assertRegex(name, re.compile(r"^[0-9a-f]{64}\.mp3$"))
        self.assertEqual(name, cache_filename("识别到雪碧/../", "zh-CN-XiaoxiaoNeural", 1.2))

    def test_text_voice_and_speed_each_change_cache_key(self):
        names = {
            cache_filename("识别到雪碧", "voice-A", 1.0),
            cache_filename("识别到可乐", "voice-A", 1.0),
            cache_filename("识别到雪碧", "voice-B", 1.0),
            cache_filename("识别到雪碧", "voice-A", 1.2),
        }
        self.assertEqual(len(names), 4)
        self.assertEqual(cache_filename("水", "v", 1), cache_filename("水", "v", 1.0))

    def test_speed_validation_rejects_invalid_numbers(self):
        for value in (0, -1, 3, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_speed(value)

    def test_audio_validation_rejects_html_json_and_truncated_data(self):
        for audio in (b"", b"ID3", b"<html>" + b"x" * 200, b'{"error":' + b"x" * 200):
            with self.subTest(audio=audio[:12]):
                self.assertFalse(is_probably_mp3(audio))
        self.assertTrue(is_probably_mp3(FAKE_MP3))

    def test_id3_metadata_is_skipped_before_frame_check(self):
        header = b"ID3\x04\x00\x00\x00\x00\x00\x04"
        self.assertTrue(is_probably_mp3(header + b"test" + FAKE_MP3))
        self.assertFalse(is_probably_mp3(header + b"test" + b"x" * 200))

    def test_atomic_write_creates_parent_and_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new" / "speech.mp3"
            write_bytes_atomic(path, FAKE_MP3)
            self.assertEqual(read_valid_mp3(path), FAKE_MP3)
            updated = FAKE_MP3 + b"changed"
            write_bytes_atomic(path, updated)
            self.assertEqual(path.read_bytes(), updated)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_failed_atomic_replace_preserves_existing_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "speech.mp3"
            path.write_bytes(FAKE_MP3)
            with patch("modules.audio.speech_utils.os.replace", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    write_bytes_atomic(path, FAKE_MP3 + b"new")
            self.assertEqual(path.read_bytes(), FAKE_MP3)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_read_invalid_or_missing_cache_returns_none(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "speech.mp3"
            self.assertIsNone(read_valid_mp3(path))
            path.write_bytes(b"<html>" + b"x" * 100)
            self.assertIsNone(read_valid_mp3(path))



"""播放层的可控测试：用替身验证结果传播，绝不访问网络或扬声器。"""

import importlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from modules.audio.speech_utils import ObjectVoice
from modules.audio.speech_utils import cache_filename


FAKE_MP3 = b"\xff\xfb\x90\x64" + b"\x00" * 413


class ObjectVoiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache_dir = Path(self.directory.name) / "cache"
        self.synthesizer = Mock(return_value=FAKE_MP3)
        self.player = Mock(return_value=True)
        self.voice = ObjectVoice(
            cache_dir=self.cache_dir,
            synthesizer=self.synthesizer,
            player=self.player,
        )

    def test_construction_does_not_create_cache_or_call_dependencies(self):
        self.assertFalse(self.cache_dir.exists())
        self.synthesizer.assert_not_called()
        self.player.assert_not_called()

    def test_import_does_not_request_audio_or_create_directory(self):
        with patch("urllib.request.urlopen") as http, patch.object(Path, "mkdir") as mkdir:
            importlib.reload(importlib.import_module("modules.audio.speech_utils"))
            http.assert_not_called()
            mkdir.assert_not_called()

    def test_success_requires_real_player_success_and_reuses_cache(self):
        self.assertTrue(self.voice.speak("识别到雪碧"))
        self.assertTrue(self.voice.speak("识别到雪碧"))
        self.synthesizer.assert_called_once_with("识别到雪碧", "zh-CN-XiaoxiaoNeural", 1.2)
        self.assertEqual(self.player.call_count, 2)

    def test_synthesis_failure_is_false_without_playing(self):
        self.synthesizer.side_effect = TimeoutError("TTS 不可用")
        self.assertFalse(self.voice.speak("识别到水"))
        self.player.assert_not_called()

    def test_invalid_synthesis_data_is_not_cached_or_played(self):
        self.synthesizer.return_value = b'{"error":"not audio"}' + b"x" * 100
        self.assertFalse(self.voice.speak("识别到水"))
        self.assertFalse(self.cache_dir.exists())
        self.player.assert_not_called()

    def test_playback_false_or_exception_never_reports_success(self):
        self.player.return_value = False
        self.assertFalse(self.voice.speak("识别到饼干"))
        self.player.side_effect = OSError("声卡不可用")
        self.assertFalse(self.voice.speak("识别到薯片"))

    def test_failed_cached_playback_preserves_cache_without_network(self):
        self.assertTrue(self.voice.cache_text("识别到面包"))
        self.synthesizer.reset_mock()
        self.synthesizer.side_effect = TimeoutError("现在离线，无法合成")
        path = self.cache_dir / cache_filename("识别到面包", self.voice.voice, self.voice.speed)
        original_cache = path.read_bytes()
        self.player.return_value = False
        self.assertFalse(self.voice.speak("识别到面包"))
        self.synthesizer.assert_not_called()
        self.player.assert_called_once_with(path)
        self.assertEqual(path.read_bytes(), original_cache)

        # 声卡/播放器恢复后，原有效缓存仍可直接使用，不需要网络。
        self.player.return_value = True
        self.assertTrue(self.voice.speak("识别到面包"))
        self.synthesizer.assert_not_called()
        self.assertEqual(self.player.call_count, 2)

    def test_playback_failure_is_bounded_to_once_per_call(self):
        self.assertTrue(self.voice.cache_text("识别到曲奇"))
        self.synthesizer.reset_mock()
        self.player.return_value = False
        self.assertFalse(self.voice.speak("识别到曲奇"))
        self.assertFalse(self.voice.speak("识别到曲奇"))
        self.synthesizer.assert_not_called()
        self.assertEqual(self.player.call_count, 2)

        # 没有缓存时，也只合成一次、播放一次；不会内部无限重试。
        self.player.reset_mock()
        self.assertFalse(self.voice.speak("识别到乐事薯片"))
        self.synthesizer.assert_called_once()
        self.player.assert_called_once()

    def test_corrupt_cache_is_replaced(self):
        self.cache_dir.mkdir()
        path = self.cache_dir / cache_filename("识别到洗手液", self.voice.voice, self.voice.speed)
        path.write_bytes(b"broken")
        self.assertTrue(self.voice.speak("识别到洗手液"))
        self.assertEqual(path.read_bytes(), FAKE_MP3)
        self.synthesizer.assert_called_once()

    def test_warm_cache_does_not_play_or_resynthesize_valid_cache(self):
        self.assertTrue(self.voice.cache_text("识别到洗洁精"))
        self.assertTrue(self.voice.cache_text("识别到洗洁精"))
        self.synthesizer.assert_called_once()
        self.player.assert_not_called()

    def test_empty_text_fails_without_side_effects(self):
        self.assertFalse(self.voice.speak("  "))
        self.assertFalse(self.voice.cache_text(""))
        self.synthesizer.assert_not_called()
        self.player.assert_not_called()

    def test_http_request_uses_existing_protocol_and_timeout(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = FAKE_MP3
        with patch("modules.audio.speech_utils.request.urlopen", return_value=response) as http:
            audio = self.voice._request_audio("识别到雪碧", "zh-CN-XiaoxiaoNeural", 1.2)
        self.assertEqual(audio, FAKE_MP3)
        req = http.call_args.args[0]
        self.assertEqual(req.full_url, "http://127.0.0.1:8002/v1/audio/speech")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(http.call_args.kwargs["timeout"], 8)
        self.assertEqual(json.loads(req.data.decode("utf-8")), {
            "model": "tts-1", "input": "识别到雪碧",
            "voice": "zh-CN-XiaoxiaoNeural", "speed": 1.2,
        })

    def test_missing_mpg123_reports_failure_without_subprocess(self):
        with patch("modules.audio.speech_utils.shutil.which", return_value=None), patch(
            "modules.audio.speech_utils.subprocess.run"
        ) as run:
            self.assertFalse(ObjectVoice._play_audio(Path("test.mp3")))
            run.assert_not_called()

    def test_mpg123_return_code_is_propagated(self):
        for returncode, expected in ((0, True), (1, False)):
            with self.subTest(returncode=returncode), patch(
                "modules.audio.speech_utils.shutil.which", return_value="/usr/bin/mpg123"
            ), patch(
                "modules.audio.speech_utils.subprocess.run",
                return_value=Mock(returncode=returncode),
            ) as run:
                self.assertIs(ObjectVoice._play_audio(Path("test.mp3")), expected)
                self.assertEqual(run.call_args.args[0], ["/usr/bin/mpg123", "-a", "default", "test.mp3"])
                self.assertEqual(run.call_args.kwargs["timeout"], 15)

    def test_two_instances_share_playback_lock(self):
        state = {"active": 0, "peak": 0}
        guard = threading.Lock()
        start = threading.Barrier(3)
        results = []

        def controlled_player(path):
            with guard:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.03)
            with guard:
                state["active"] -= 1
            return True

        first = ObjectVoice(cache_dir=self.cache_dir / "a", synthesizer=self.synthesizer, player=controlled_player)
        second = ObjectVoice(cache_dir=self.cache_dir / "b", synthesizer=self.synthesizer, player=controlled_player)

        def worker(voice, text):
            start.wait()
            results.append(voice.speak(text))

        threads = [
            threading.Thread(target=worker, args=(first, "识别到可乐")),
            threading.Thread(target=worker, args=(second, "识别到芬达")),
        ]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, [True, True])
        self.assertEqual(state["peak"], 1)



"""使用第三方库替身执行真实 chat.py 处理函数，不启动 HTTP 服务或访问 Edge。

这验证合成/缓存/并发/失败处理，不验证 FastAPI 自身的请求校验或 Edge 网络。
实际 HTTP 服务仍需在机器人现有 Python 环境中单独核验。
"""

import asyncio
import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules.audio.speech_utils import ObjectVoice


# 只用于模拟返回的数据；不会送给真实播放器。
FAKE_MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 124


class FakeResponse:
    def __init__(self, content, media_type):
        self.content = content
        self.media_type = media_type


class FakeHTTPException(Exception):
    def __init__(self, *, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code


class FakeFastAPI:
    def __init__(self, **_kwargs):
        self.routes = {}

    def post(self, path):
        def register(function):
            self.routes[path] = function
            return function
        return register


def load_actual_chat():
    fastapi = ModuleType("fastapi")
    fastapi.FastAPI = FakeFastAPI
    fastapi.Body = lambda default=None, **_kwargs: default
    fastapi.HTTPException = FakeHTTPException
    fastapi.Response = FakeResponse
    edge = ModuleType("edge_tts")
    uvicorn = ModuleType("uvicorn")
    uvicorn.run = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("禁止启动服务"))
    spec = importlib.util.spec_from_file_location("review_chat_under_test", ROOT / "modules/audio/chat.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"fastapi": fastapi, "edge_tts": edge, "uvicorn": uvicorn}):
        spec.loader.exec_module(module)
    return module, edge


class ChatServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.chat, self.edge = load_actual_chat()
        self.chat.AUDIO_DIR = Path(self.directory.name) / "service_cache"
        self.calls = []
        self.audio = FAKE_MP3
        self.error = None
        owner = self

        class FakeCommunicate:
            def __init__(self, text, voice, *, rate):
                owner.calls.append((text, voice, rate))

            async def save(self, filename):
                # 让并发请求有机会同时进入，验证同键锁确实避免重复合成。
                await asyncio.sleep(0)
                if owner.error:
                    raise owner.error
                Path(filename).write_bytes(owner.audio)

        self.edge.Communicate = FakeCommunicate

    async def request(self, text="识别到雪碧", voice="zh-CN-XiaoxiaoNeural", speed=1.2):
        return await self.chat.speech(model="tts-1", input=text, voice=voice, speed=speed)

    def test_chinese_protocol_and_cache_reuse(self):
        self.assertIn("/v1/audio/speech", self.chat.app.routes)
        first = asyncio.run(self.request())
        second = asyncio.run(self.request())
        self.assertEqual(first.content, FAKE_MP3)
        self.assertEqual(second.content, FAKE_MP3)
        self.assertEqual(first.media_type, "audio/mpeg")
        self.assertEqual(self.calls, [("识别到雪碧", "zh-CN-XiaoxiaoNeural", "+20%")])
        self.assertEqual(len(list(self.chat.AUDIO_DIR.glob("*.mp3"))), 1)

    def test_changed_voice_or_speed_generates_new_audio(self):
        asyncio.run(self.request())
        asyncio.run(self.request(voice="zh-CN-YunxiNeural"))
        asyncio.run(self.request(speed=1.0))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(list(self.chat.AUDIO_DIR.glob("*.mp3"))), 3)

    def test_concurrent_same_text_only_synthesizes_once(self):
        async def concurrent():
            return await asyncio.gather(self.request(), self.request())
        results = asyncio.run(concurrent())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([r.content for r in results], [FAKE_MP3, FAKE_MP3])

    def test_synthesis_failure_returns_502_and_cleans_temporary_files(self):
        self.error = RuntimeError("模拟 Edge 服务不可用")
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(FakeHTTPException) as captured:
                asyncio.run(self.request())
        self.assertEqual(captured.exception.status_code, 502)
        self.assertEqual(list(self.chat.AUDIO_DIR.iterdir()), [])

    def test_error_response_is_not_saved_as_audio(self):
        self.audio = b'{"error": "not audio"}'
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(FakeHTTPException) as captured:
                asyncio.run(self.request())
        self.assertEqual(captured.exception.status_code, 502)
        self.assertEqual(list(self.chat.AUDIO_DIR.iterdir()), [])

    def test_blank_input_does_not_synthesize(self):
        with self.assertRaises(FakeHTTPException) as captured:
            asyncio.run(self.request(text="  "))
        self.assertEqual(captured.exception.status_code, 422)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.chat.AUDIO_DIR.exists())

    def test_client_can_consume_server_response_and_reuse_its_cache(self):
        played = []

        def synthesize(text, voice, speed):
            return asyncio.run(self.request(text, voice, speed)).content

        client = ObjectVoice(cache_dir=Path(self.directory.name) / "client_cache",
                             synthesizer=synthesize, player=lambda path: played.append(path.read_bytes()) or True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(client.speak("识别到雪碧"))
            self.assertTrue(client.speak("识别到雪碧"))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(played, [FAKE_MP3, FAKE_MP3])



if __name__ == "__main__":
    unittest.main()
