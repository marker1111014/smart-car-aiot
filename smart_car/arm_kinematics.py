# arm_kinematics.py
# 機械手臂逆運動學 (IK) 與工作空間邊界檢查及底盤微調補償
import math
from typing import Optional, Dict, Any, Tuple

class ArmKinematics:
    """
    4 自由度機械臂逆運動學解算器 (底座 Yaw + 大臂 Shoulder + 小臂 Elbow + 夾爪 Gripper)
    支援工作空間超限與奇異點檢查，並提供底盤前後微調補償建議。
    """
    def __init__(
        self,
        l1: float = 105.0,  # 大臂長度 (mm)
        l2: float = 100.0,  # 小臂長度 (mm)
        base_h: float = 55.0  # 底座高度 (mm)
    ):
        self.l1 = l1
        self.l2 = l2
        self.base_h = base_h
        self.max_reach = (l1 + l2) * 0.95  # 避免奇異點之最大工作半徑
        self.min_reach = abs(l1 - l2) + 20.0  # 最小工作半徑

    def solve_ik(self, x: float, y: float, z: float) -> Tuple[bool, Optional[Dict[str, int]], Optional[Dict[str, Any]]]:
        """
        解算 (x, y, z) 毫米空間座標：
        :param x: 車身水平前方距離 (mm)
        :param y: 車身水平左右偏差 (mm, 左正右負)
        :param z: 離地垂直高度 (mm)
        :return: (is_solvable, servo_angles, compensation)
        """
        # 1. 解算底座旋轉角 (Yaw)
        yaw_rad = math.atan2(y, x) if x != 0 else 0.0
        yaw_deg = int(round(90 + math.degrees(yaw_rad)))
        yaw_deg = max(0, min(180, yaw_deg))

        # 2. 轉換為大臂平面二維座標
        r = math.sqrt(x**2 + y**2)
        z_rel = z - self.base_h
        dist_sq = r**2 + z_rel**2
        dist = math.sqrt(dist_sq)

        # 3. 邊界與奇異點檢查 (工作空間檢驗)
        if dist > self.max_reach:
            # 目標過遠，無解 -> 計算底盤前進補償
            need_forward_mm = dist - (self.max_reach * 0.8)
            compensation = {
                "action": "chassis_compensation",
                "direction": "forward",
                "distance_mm": round(need_forward_mm, 1),
                "reason": f"超出工作半徑 (目標距離 {round(dist, 1)}mm > 最大 {round(self.max_reach, 1)}mm)"
            }
            return False, None, compensation

        if dist < self.min_reach:
            # 目標過近，無法折疊夾取 -> 計算底盤後退補償
            need_backward_mm = self.min_reach + 30.0 - dist
            compensation = {
                "action": "chassis_compensation",
                "direction": "backward",
                "distance_mm": round(need_backward_mm, 1),
                "reason": f"小於最小工作距離 (目標距離 {round(dist, 1)}mm < 最小 {round(self.min_reach, 1)}mm)"
            }
            return False, None, compensation

        # 4. 餘弦定理求肩關節與肘關節角度
        try:
            cos_elbow = (self.l1**2 + self.l2**2 - dist_sq) / (2 * self.l1 * self.l2)
            cos_elbow = max(-1.0, min(1.0, cos_elbow))
            beta = math.acos(cos_elbow)  # 肘關節內角

            cos_shoulder_offset = (self.l1**2 + dist_sq - self.l2**2) / (2 * self.l1 * dist)
            cos_shoulder_offset = max(-1.0, min(1.0, cos_shoulder_offset))
            alpha = math.acos(cos_shoulder_offset)

            phi = math.atan2(z_rel, r)
            shoulder_rad = phi + alpha
            elbow_rad = math.pi - beta

            shoulder_deg = int(round(math.degrees(shoulder_rad)))
            elbow_deg = int(round(math.degrees(elbow_rad)))

            # 映射至實體舵機機械極限 (通常 15~165度)
            shoulder_deg = max(15, min(165, shoulder_deg))
            elbow_deg = max(15, min(165, elbow_deg))

            angles = {
                "wrist": yaw_deg,        # 底座旋轉
                "shoulder": shoulder_deg, # 大臂仰角
                "elbow": elbow_deg,       # 小臂俯仰
                "gripper": 25             # 預設夾爪夾緊度
            }
            return True, angles, None

        except Exception as e:
            compensation = {
                "action": "chassis_compensation",
                "direction": "forward",
                "distance_mm": 30.0,
                "reason": f"數學逆解發散異常: {e}"
            }
            return False, None, compensation

if __name__ == "__main__":
    solver = ArmKinematics()
    print("--- 測試正常可夾取點 (x=140, y=0, z=45) ---")
    ok, angles, comp = solver.solve_ik(140, 0, 45)
    print("解算成功:", ok, "舵機角度:", angles)

    print("\n--- 測試超出範圍點 (x=240, y=0, z=45) ---")
    ok, angles, comp = solver.solve_ik(240, 0, 45)
    print("解算成功:", ok, "底盤補償建議:", comp)
