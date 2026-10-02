"""第一阶段识别服务：RGB 类别识别、多帧确认、实时画面和截图。

识别分只需要物品类别，因此不要求深度有效，也不计算抓取坐标。
抓取阶段仍使用原 PerceptionPipeline 的深度与手眼标定，不能将此处结果直接抓取。
该服务不导入导航、机械臂或滑轨，比赛主流程和独立看图工具都可以调用它。
"""
from __future__ import annotations

from datetime import datetime
import json
import time

from .recognition_camera import RecognitionCameraSwitcher
from .recognition_config import RecognitionSettings, positive_number, validate_display


class ConsecutiveLabelTracker:
    """确认同一类别连续出现 N 帧；这里确认类别，不追踪某个物体的三维身份。"""
    def __init__(self, required_frames: int):
        self.required_frames = required_frames
        self.counts: dict[str, int] = {}

    def reset(self):
        self.counts.clear()

    def update(self, detections):
        labels = {str(item.get("label", "")).strip() for item in detections}
        labels.discard("")
        # 丢失的类别会清零。一个画面里有两瓶 cola，也只增加一次类别计数。
        self.counts = {label: self.counts.get(label, 0) + 1 for label in labels}
        output = []
        for detection in detections:
            item = dict(detection)
            label = str(item.get("label", "")).strip()
            item["label"] = label
            item["stable_count"] = self.counts.get(label, 0)
            item["stable"] = bool(label) and item["stable_count"] >= self.required_frames
            output.append(item)
        return output


class RecognitionWindow:
    """界面职责单独封装；不显示时完全不调用 OpenCV 的 GUI 函数。"""
    NAME = "competition_recognition"

    def __init__(self, detector, capture_dir):
        self.detector = detector
        self.capture_dir = capture_dir
        self.opened = False

    def show(self, image, results, station, spec, remaining):
        import cv2

        canvas = self.detector.draw_detections(image, results, text_scale=2.0)
        # 类别中文由原检测器的 Pillow 字体绘制；状态栏使用 ASCII 避免乱码。
        status = f"SCAN {station} | {spec.role} SN={spec.serial} | {remaining:.1f}s | s:save q:stop"
        cv2.rectangle(canvas, (0, 0), (canvas.shape[1], 30), (30, 30, 30), -1)
        cv2.putText(canvas, status, (6, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 255), 1, cv2.LINE_AA)
        cv2.imshow(self.NAME, canvas)
        self.opened = True
        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27) or cv2.getWindowProperty(self.NAME, cv2.WND_PROP_VISIBLE) < 1:
            raise KeyboardInterrupt("用户停止识别；不会继续进入抓取")
        if key == ord("s"):
            self.capture_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            stem = self.capture_dir / f"{stamp}_{spec.role}"
            if not cv2.imwrite(str(stem.with_suffix(".jpg")), canvas):
                raise RuntimeError("截图保存失败，请检查 captures 目录")
            stem.with_suffix(".json").write_text(json.dumps({
                "station": station, "camera_role": spec.role, "camera_serial": spec.serial,
                "detections": results, "captured_at": stamp,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[截图] {stem.with_suffix('.jpg')}", flush=True)

    def close(self):
        if self.opened:
            import cv2
            cv2.destroyWindow(self.NAME)
            self.opened = False


class RecognitionService:
    def __init__(self, settings: RecognitionSettings, detector, *, show: bool,
                 switcher=None, window=None, clock=time.monotonic):
        validate_display(show)
        self.settings = settings
        self.detector = detector
        self.show = show
        self.clock = clock
        self.switcher = switcher or RecognitionCameraSwitcher(settings)
        self.tracker = ConsecutiveLabelTracker(settings.required_frames)
        self.window = window or RecognitionWindow(detector, settings.capture_dir)

    def scan_station(self, station: str, seconds: float, guard=lambda: None, *, camera_role=None):
        """到站后按配置选相机，返回当前点识别到的稳定类别。

        seconds 是相机启动/预热完成后的有效观察时间。每次到新点先清零稳定计数。
        camera_role 只给独立调试工具使用；比赛调用不会让键盘改变点位分组。
        """
        seconds = positive_number(seconds, "识别时长")
        spec = (self.settings.cameras[camera_role] if camera_role is not None
                else self.settings.camera_for_station(station))
        self.switcher.select(spec, guard)
        self.tracker.reset()
        deadline = self.clock() + seconds
        best = {}
        received_frames = 0
        while self.clock() < deadline:
            guard()
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            frame = self.switcher.read(min(self.settings.frame_timeout_ms, max(1, int(remaining * 1000))))
            guard()
            if frame is None:
                # 无新帧不能算连续确认；之后收到图像时从头计数。
                self.tracker.reset()
                continue
            received_frames += 1
            results = self.tracker.update(self.detector.detect(frame["color_image"]))
            guard()
            for item in results:
                if item["stable"]:
                    label = item["label"]
                    if label not in best or item["confidence"] > best[label]["confidence"]:
                        best[label] = dict(item, station=station, camera_role=spec.role, camera_serial=spec.serial)
            if self.show:
                self.window.show(frame["color_image"], results, station, spec, max(0, deadline - self.clock()))
        guard()
        if received_frames == 0:
            raise RuntimeError(f"{station} 的 {spec.role} 相机整个观察期间没有有效图像，不能当作‘没有物品’")
        detected = sorted(best.values(), key=lambda item: item["confidence"], reverse=True)
        labels = ", ".join(item["label"] for item in detected) or "未发现稳定类别"
        print(f"[识别] {station} / {spec.role}: {labels}", flush=True)
        return detected

    def close(self):
        # 无论相机关闭是否成功，都尝试关闭窗口，并把错误交给上层报告。
        try:
            self.switcher.close()
        finally:
            self.tracker.reset()
            self.window.close()
