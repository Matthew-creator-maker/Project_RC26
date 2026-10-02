"""队友版本的专用回归：模拟导航/视觉/机械臂，无真实机器人动作。"""
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import main
from modules.perception.recognition_config import load_recognition_settings


class TeammateMergeTests(unittest.TestCase):
    def test_current_settings_keep_latest_route_and_six_second_scan(self):
        config = main.load_settings(ROOT / "config/task_config.json")
        plan = main.competition_plan(config)
        self.assertEqual(plan.scan_nav_stations, {})
        self.assertEqual(main.build_parser().parse_args([]).scan_seconds, 6)
        settings = load_recognition_settings(config)
        self.assertEqual(settings.camera_for_station("LM6").serial, "151222073707")
        self.assertEqual(settings.cameras["head"].serial, "151222072331")
        # 当前 LM15 不在识别路线中，不恢复上一版的示例导航映射。
        with self.assertRaisesRegex(ValueError, "未分配"):
            settings.camera_for_station("LM15")

    def test_transport_before_navigation_and_real_labels_announced_once(self):
        config = main.load_settings(ROOT / "config/task_config.json")
        args = main.build_parser().parse_args(["--execute", "--confirm-calibration", "--no-show"])
        events = []
        class Navigator:
            station = "LM1"
            def connect(self): pass
            def current_station(self): return self.station
            def assert_at(self, station):
                if self.station != station: raise RuntimeError("wrong station")
            def go_to_station(self, station):
                events.append(("nav", station))
                self.station = station
            def close(self): pass
        nav = Navigator()
        class Scanner:
            def transport(self, guard):
                guard()
                events.append(("transport", nav.station))
            def scan_station(self, station, seconds, guard):
                guard()
                events.append(("scan", station, nav.station, seconds))
                # 同类别跨点重复出现，合并后应只成功播报一次。
                return [{"label": "cola", "confidence": 0.9}]
            def finish_recognition(self): events.append(("finish_recognition",))
            def grasp_station_once(self, station, *_args, guard, **_kwargs):
                guard()
                events.append(("grasp", station, nav.station))
                raise TimeoutError("本点没有可抓取物品")
            def close(self): pass
        class Rail:
            lowered = False
            def travel(self): pass
            def prepare(self, station): pass
        with patch.object(main, "speak_blocking", side_effect=lambda text: events.append(("speak", text)) or True):
            result = main.run_competition(config, args, navigator_factory=lambda _: nav,
                                         scanner_factory=lambda *_: Scanner(), rail_factory=Rail)
        self.assertLess(events.index(("transport", "LM1")), events.index(("nav", "LM2")))
        self.assertIn(("scan", "LM6", "LM6", 6), events)
        self.assertIn(("grasp", "LM6", "LM6"), events)
        self.assertLess(events.index(("finish_recognition",)), events.index(("grasp", "LM6", "LM6")))
        self.assertEqual(len([e for e in events if e[0] == "speak"]), 1)
        self.assertEqual(result["announced"], ["cola"])




if __name__ == "__main__":
    unittest.main()
