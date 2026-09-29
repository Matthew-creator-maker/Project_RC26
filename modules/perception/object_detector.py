"""YOLO11 物品检测模块。

功能：
1. YOLO11 目标检测
2. 返回类别、置信度、检测框、像素中心
3. 绘制检测框
4. 使用 Pillow 绘制类别和置信度，支持中文
5. 支持 text_scale 调整标签大小

离线检测建议 text_scale=10.0。
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from ultralytics import YOLO

from .camera_manager import load_config


ANNOTATION_VERSION = "BIG_LABEL_V3"


class ObjectDetector:
    """对一张 BGR 图像执行 YOLO 检测。"""

    def __init__(self, config: Dict[str, Any]):
        model_cfg = config.get("model", {})
        config_dir = Path(config.get("_config_dir", Path.cwd()))

        weights = Path(str(model_cfg.get("weights", "")))
        if not weights.is_absolute():
            weights = (config_dir / weights).resolve()

        if not weights.exists():
            raise FileNotFoundError(
                f"找不到 YOLO 权重文件: {weights}\n"
                "请检查 config.yaml 中的 model.weights。"
            )

        self.confidence = float(model_cfg.get("confidence", 0.5))
        self.iou = float(model_cfg.get("iou", 0.45))
        self.device = model_cfg.get("device", "cpu")
        self.weights_path = weights

        self.model = YOLO(str(weights))

        names = self.model.names
        if isinstance(names, dict):
            self.class_names = {
                int(key): str(value)
                for key, value in names.items()
            }
        else:
            self.class_names = {
                index: str(value)
                for index, value in enumerate(names)
            }

        self.font_path = self._find_font()

        print(f"[{ANNOTATION_VERSION}]")
        print(f"ObjectDetector 文件: {Path(__file__).resolve()}")
        print(f"YOLO 权重: {self.weights_path}")
        print(f"检测类别: {self.class_names}")

        if self.font_path:
            print(f"标签字体: {self.font_path}")
        else:
            print("未找到中文字体，将使用 Pillow 默认字体。")

    @staticmethod
    def _find_font() -> Optional[str]:
        """寻找 Ubuntu 和 Windows 常见字体。"""
        candidates = [
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("C:/Windows/Fonts/msyhbd.ttc"),
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("C:/Windows/Fonts/simhei.ttf"),
            Path("C:/Windows/Fonts/simsun.ttc"),
        ]

        for path in candidates:
            if path.exists():
                return str(path)

        return None

    def _get_font(self, font_size: int):
        if self.font_path:
            try:
                return ImageFont.truetype(
                    self.font_path,
                    font_size,
                )
            except OSError:
                pass

        return ImageFont.load_default()

    def detect(
        self,
        image: np.ndarray,
    ) -> List[Dict[str, Any]]:
        """返回图像中的全部 YOLO 检测结果。"""
        if image is None or image.size == 0:
            return []

        results = self.model(
            image,
            conf=self.confidence,
            iou=self.iou,
            device=self.device,
            verbose=False,
        )

        if not results:
            return []

        result = results[0]

        if result.boxes is None or len(result.boxes) == 0:
            return []

        boxes = result.boxes.xyxy.cpu().numpy()
        class_ids = result.boxes.cls.cpu().numpy().astype(int)
        confidences = result.boxes.conf.cpu().numpy()

        image_height, image_width = image.shape[:2]

        detections: List[Dict[str, Any]] = []

        for box, class_id, confidence in zip(
            boxes,
            class_ids,
            confidences,
        ):
            x1, y1, x2, y2 = [
                float(value)
                for value in box
            ]

            x1 = max(0.0, min(x1, image_width - 1))
            y1 = max(0.0, min(y1, image_height - 1))
            x2 = max(0.0, min(x2, image_width - 1))
            y2 = max(0.0, min(y2, image_height - 1))

            center_u = int(round((x1 + x2) / 2.0))
            center_v = int(round((y1 + y2) / 2.0))

            detections.append(
                {
                    "label": self.class_names.get(
                        int(class_id),
                        f"class_{int(class_id)}",
                    ),
                    "class_id": int(class_id),
                    "confidence": float(confidence),
                    "bbox": [
                        x1,
                        y1,
                        x2,
                        y2,
                    ],
                    "pixel_center": [
                        center_u,
                        center_v,
                    ],
                }
            )

        return detections

    def draw_detections(
        self,
        image: np.ndarray,
        detections: List[Dict[str, Any]],
        text_scale: float = 1.0,
    ) -> np.ndarray:
        """绘制框、类别、置信度以及中心点。

        text_scale=1.0 约等于原始字号。
        text_scale=10.0 约等于原始字号的 10 倍。
        """
        if image is None or image.size == 0:
            return image

        image_height, image_width = image.shape[:2]

        # 原代码字体在高分辨率照片中大约只有十几像素高。
        # 这里以约 11 px 为基准，再乘 text_scale。
        base_font_size = max(
            11,
            int(image_height * 0.0055),
        )

        font_size = max(
            10,
            int(base_font_size * float(text_scale)),
        )

        font = self._get_font(font_size)

        # 随图片大小自动调整线条粗细
        box_width = max(
            3,
            int(min(image_height, image_width) * 0.003),
        )

        center_radius = max(
            5,
            int(min(image_height, image_width) * 0.005),
        )

        # OpenCV BGR -> Pillow RGB
        rgb = cv2.cvtColor(
            image.copy(),
            cv2.COLOR_BGR2RGB,
        )
        pil_image = Image.fromarray(rgb)
        draw = ImageDraw.Draw(pil_image)

        # Pillow 使用 RGB
        box_color = (0, 255, 0)
        label_bg_color = (0, 255, 0)
        label_text_color = (0, 0, 0)
        center_color = (255, 0, 0)

        for detection in detections:
            x1, y1, x2, y2 = [
                int(round(value))
                for value in detection["bbox"]
            ]

            # 检测框
            draw.rectangle(
                [(x1, y1), (x2, y2)],
                outline=box_color,
                width=box_width,
            )

            label = str(detection["label"])
            confidence = float(
                detection["confidence"]
            )

            # 保持你当前习惯的 0.72 格式
            text = f"{label}  {confidence:.2f}"

            text_bbox = draw.textbbox(
                (0, 0),
                text,
                font=font,
            )

            text_width = (
                text_bbox[2] - text_bbox[0]
            )
            text_height = (
                text_bbox[3] - text_bbox[1]
            )

            padding_x = max(
                8,
                int(font_size * 0.12),
            )
            padding_y = max(
                5,
                int(font_size * 0.08),
            )

            label_x = x1

            # 优先放在检测框上方
            label_y = (
                y1
                - text_height
                - 2 * padding_y
                - box_width
            )

            # 顶部放不下就放在框内顶部
            if label_y < 0:
                label_y = y1 + box_width

            # 防止标签超出图片右边界
            total_width = (
                text_width
                + 2 * padding_x
            )

            if label_x + total_width >= image_width:
                label_x = max(
                    0,
                    image_width - total_width - 1,
                )

            bg_left = label_x
            bg_top = label_y
            bg_right = (
                label_x
                + text_width
                + 2 * padding_x
            )
            bg_bottom = (
                label_y
                + text_height
                + 2 * padding_y
            )

            # 标签绿色背景
            draw.rectangle(
                [
                    (bg_left, bg_top),
                    (bg_right, bg_bottom),
                ],
                fill=label_bg_color,
            )

            # 类别 + 置信度
            draw.text(
                (
                    label_x + padding_x,
                    label_y + padding_y,
                ),
                text,
                font=font,
                fill=label_text_color,
            )

            # 中心点
            center_u, center_v = (
                detection["pixel_center"]
            )

            draw.ellipse(
                [
                    (
                        center_u - center_radius,
                        center_v - center_radius,
                    ),
                    (
                        center_u + center_radius,
                        center_v + center_radius,
                    ),
                ],
                fill=center_color,
            )

        output = cv2.cvtColor(
            np.asarray(pil_image),
            cv2.COLOR_RGB2BGR,
        )

        return output


def create_detector(
    config_path: Optional[str] = None,
) -> ObjectDetector:
    return ObjectDetector(
        load_config(config_path)
    )
