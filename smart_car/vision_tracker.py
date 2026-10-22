# vision_tracker.py
# 跨平台 (CUDA / Apple Silicon MPS / CPU) 本地即時視覺伺服核心
import math
import numpy as np
from typing import Optional, Dict, Any, Tuple
from PIL import Image

try:
    import torch
    if torch.cuda.is_available():
        DEVICE = "cuda"
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        DEVICE = "mps"
    else:
        DEVICE = "cpu"
except ImportError:
    DEVICE = "cpu"

class VisualServoTracker:
    """
    基於 YOLO 的高頻即時視覺伺服與單目測距追蹤器
    支援跨平台 GPU/NPU/CPU 加速，即時計算轉向 PID 與前進線速度。
    """
    def __init__(self, model_name: str = "yolov8n.pt", frame_width: int = 640, frame_height: int = 480):
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.center_x = frame_width / 2.0  # 320.0
        self.center_y = frame_height / 2.0  # 240.0

        # 相機焦距等效值 (OV2640 VGA 640x480 視角約 65 度 -> 焦距約 500 像素)
        self.focal_length_px = 500.0

        # PID 控制器參數
        self.kp = 0.35
        self.kd = 0.08
        self.prev_error_u = 0.0

        # 自動下載並載入 YOLO 模型
        print(f"[INFO] 初始化視覺追蹤器，目標設備: [{DEVICE.upper()}] ...")
        from ultralytics import YOLO
        self.model = YOLO(model_name)
        print("[INFO] YOLO 視覺模型載入成功！")

    def detect_and_track(
        self,
        img: Image.Image,
        target_class: str = "bottle",
        target_classes: Optional[list] = None,
        real_height_mm: float = 210.0,
        stop_dist_threshold_mm: float = 200.0,
        conf_threshold: float = 0.15,
        imgsz: int = 480
    ) -> Dict[str, Any]:
        """
        對單張影像進行物體定位、單目測距與底盤閉環速度解算 (支援多候選類別匹配與全景視野物件提取)
        """
        # 1. 執行推論 (conf=0.15, imgsz=480 兼顧即時性與高召回率)
        results = self.model.predict(source=img, device=DEVICE, conf=conf_threshold, imgsz=imgsz, verbose=False)
        boxes = results[0].boxes
        names = self.model.names

        # 整理當前視野中所有檢測到的物體 (供 Dashboard 即時觀察與除錯)
        all_detected = []
        if boxes is not None and len(boxes) > 0:
            for b in boxes:
                cid = int(b.cls[0].item())
                cname = names.get(cid, "unknown")
                cf = float(b.conf[0].item())
                xy = [int(v) for v in b.xyxy[0].cpu().numpy().tolist()]
                all_detected.append({"class": cname, "conf": round(cf, 2), "bbox": xy})

        if not all_detected:
            self.prev_error_u = 0.0
            return {
                "target_found": False,
                "detected_class": None,
                "all_detected": [],
                "command": {"action": "spin_left", "speed": 180, "duration": 400},
                "status": "SEARCHING",
                "info": f"未搜尋到目標【{target_class}】，執行微幅旋轉掃描"
            }

        # 2. 尋找與 target_class 或其候選同類最相符的目標
        valid_targets = [c.lower() for c in (target_classes or [target_class])]
        matched_box = None
        matched_class = None
        highest_conf = -1.0

        # 第一優先：精準匹配 target_class
        for b in boxes:
            cid = int(b.cls[0].item())
            cname = names.get(cid, "")
            cf = float(b.conf[0].item())
            if cname.lower() == target_class.lower() and cf > highest_conf:
                highest_conf = cf
                matched_box = b
                matched_class = cname

        # 第二優先：若無精準命中，匹配相容候選類別 (例如 cup 與 bottle 互為飲品容器候選)
        if matched_box is None and len(valid_targets) > 1:
            for b in boxes:
                cid = int(b.cls[0].item())
                cname = names.get(cid, "")
                cf = float(b.conf[0].item())
                if cname.lower() in valid_targets and cf > highest_conf:
                    highest_conf = cf
                    matched_box = b
                    matched_class = cname

        if matched_box is None:
            self.prev_error_u = 0.0
            detected_summary = ", ".join([f"{d['class']}({int(d['conf']*100)}%)" for d in all_detected[:3]])
            return {
                "target_found": False,
                "detected_class": None,
                "all_detected": all_detected,
                "command": {"action": "spin_left", "speed": 180, "duration": 400},
                "status": "SEARCHING",
                "info": f"未匹配到【{target_class}】 (視野辨識到: {detected_summary})"
            }

        # 3. 提取邊界框並計算幾何中心
        xyxy = matched_box.xyxy[0].cpu().numpy()
        x1, y1, x2, y2 = xyxy
        box_w = x2 - x1
        box_h = y2 - y1
        u = (x1 + x2) / 2.0
        v = (y1 + y2) / 2.0

        # 4. 單目小孔成像擬真測距 (模擬雷射測距 d)
        # d = (f * H) / h
        estimated_dist_mm = (self.focal_length_px * real_height_mm) / max(1.0, box_h)
        estimated_dist_mm = max(50.0, min(3000.0, estimated_dist_mm))

        # 5. 計算目標偏離相機垂直中心線的誤差 (像素)
        error_u = u - self.center_x
        d_error = error_u - self.prev_error_u
        self.prev_error_u = error_u

        # 6. PID 偏航伺服轉向計算
        steer_adjustment = (self.kp * error_u) + (self.kd * d_error)

        # 7. 判斷是否已進入夾取煞停距離 (例如 <= 200 mm)
        if estimated_dist_mm <= stop_dist_threshold_mm:
            return {
                "target_found": True,
                "detected_class": matched_class,
                "conf": round(highest_conf, 2),
                "all_detected": all_detected,
                "status": "READY_FOR_ARM",
                "distance_mm": round(estimated_dist_mm, 1),
                "bbox": [int(x1), int(y1), int(x2), int(y2)],
                "center": (round(u, 1), round(v, 1)),
                "command": {"action": "brake", "speed": 0},
                "info": f"🎯 已抵達抓取距離 ({round(estimated_dist_mm / 10, 1)}cm)，立即煞停進行二次採樣！"
            }

        # 8. 尚未達到抓取距離：持續視覺伺服前進閉環
        base_speed = 185
        left_pwm = int(round(base_speed + steer_adjustment))
        right_pwm = int(round(base_speed - steer_adjustment))

        left_pwm = max(80, min(255, left_pwm))
        right_pwm = max(80, min(255, right_pwm))

        # 若偏差在正負 30px 內，代表已高度對齊中心線，直行前進
        if abs(error_u) <= 30:
            command = {"action": "forward", "speed": base_speed, "duration": 250}
        elif error_u < 0:
            # 偏左，左旋微調
            command = {"action": "left", "speed": left_pwm, "duration": 180}
        else:
            # 偏右，右旋微調
            command = {"action": "right", "speed": right_pwm, "duration": 180}

        return {
            "target_found": True,
            "detected_class": matched_class,
            "conf": round(highest_conf, 2),
            "all_detected": all_detected,
            "status": "TRACKING_APPROACH",
            "distance_mm": round(estimated_dist_mm, 1),
            "error_u": round(error_u, 1),
            "bbox": [int(x1), int(y1), int(x2), int(y2)],
            "center": (round(u, 1), round(v, 1)),
            "command": command,
            "left_pwm": left_pwm,
            "right_pwm": right_pwm,
            "info": f"🚀 閉環推進中: 估計距離 {round(estimated_dist_mm / 10, 1)}cm, 偏航誤差 {round(error_u, 1)}px"
        }
