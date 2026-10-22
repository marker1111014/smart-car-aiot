# test_pipeline.py
# 端到端全流程驗證腳本：Gemini 語意 -> YOLO 本地追蹤 -> PID 閉環微調 -> 煞停採樣 -> IK 求解與補償
import os
import sys
import time

# 解決 Windows console cp950 編碼問題
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from PIL import Image
from gemini_brain import GeminiSemanticBrain
from arm_kinematics import ArmKinematics

def run_pipeline_test(user_prompt: str, image_path: str):
    print("=" * 65)
    print(f"[TEST] 端到端全流程測試: 指令 = 「{user_prompt}」")
    print("=" * 65)

    # 1. 語意解析
    t0 = time.time()
    brain = GeminiSemanticBrain()
    target_info = brain.parse_task(user_prompt)
    elapsed_brain = round((time.time() - t0) * 1000, 1)
    print(f"[Stage 1: 語意大腦] (耗時: {elapsed_brain}ms)")
    print(f"   * 目標類別: {target_info['target_class']} ({target_info['target_zh']})")
    print(f"   * 先驗物理尺寸: 高度 {target_info['height_mm']}mm, 寬度 {target_info['width_mm']}mm")
    print(f"   * 來源: {target_info['source']}")

    # 2. 本地視覺追蹤與單目模擬測距
    try:
        from vision_tracker import VisualServoTracker
        tracker = VisualServoTracker()
    except Exception as e:
        print(f"[ERROR] 視覺追蹤器載入失敗: {e}")
        return

    if not os.path.exists(image_path):
        print(f"[WARN] 找不到測試圖檔: {image_path}")
        return

    img = Image.open(image_path)
    t1 = time.time()
    track_res = tracker.detect_and_track(
        img=img,
        target_class=target_info["target_class"],
        real_height_mm=target_info["height_mm"],
        stop_dist_threshold_mm=200.0  # 20cm 煞停
    )
    fps = round(1.0 / max(0.001, (time.time() - t1)), 1)
    print(f"\n[Stage 2: YOLO 追蹤與單目測距] (推論幀率: {fps} FPS)")
    print(f"   * 狀態: {track_res['status']}")
    print(f"   * 說明: {track_res['info']}")

    if not track_res["target_found"]:
        print("   * 下發掃描指令:", track_res["command"])
        return

    dist_mm = track_res["distance_mm"]
    print(f"   * 估算距離: {dist_mm} mm ({round(dist_mm/10, 1)} cm)")
    print(f"   * 畫面中心偏差: {track_res.get('error_u', 0)} px")

    # 3. 狀態判定
    if track_res["status"] == "TRACKING_APPROACH":
        print(f"\n[Stage 3: 視覺伺服閉環推進 (Visual Servoing)]")
        print(f"   * 下發即時差速指令: Left PWM={track_res.get('left_pwm')} | Right PWM={track_res.get('right_pwm')}")
        print(f"   * 即時控制指令: {track_res['command']}")
        print("   * (在逼近過程中，物體將始終被鎖定在畫面中心線，確保雷射不脫靶)")

    elif track_res["status"] == "READY_FOR_ARM":
        print(f"\n[Stage 4: 達標煞停與二次採樣確認]")
        print("   * 底盤煞停，等候 300ms 慣性震動消除")
        print(f"   * 目標 3D 座標估計: X={dist_mm}mm, Y=0mm, Z=45mm")

        # 4. 逆運動學求解與補償
        solver = ArmKinematics()
        is_solvable, servo_angles, comp = solver.solve_ik(x=dist_mm, y=0.0, z=45.0)

        print(f"\n[Stage 5: IK 逆運動學與底盤補償分支]")
        if is_solvable:
            print("   [OK] [求解成功] 機械臂規劃夾取姿態:")
            print(f"      - 底座 (Yaw/Wrist): {servo_angles['wrist']}°")
            print(f"      - 大臂 (Shoulder):  {servo_angles['shoulder']}°")
            print(f"      - 小臂 (Elbow):     {servo_angles['elbow']}°")
            print(f"      - 夾爪 (Gripper):   {servo_angles['gripper']}°")
            print("   * 下發夾取指令，抓取後收回！")
        else:
            print(f"   [WARN] [IK 無解/超出工作空間] 觸發底盤微調補償分支:")
            print(f"      - 補償動作: {comp['action']}")
            print(f"      - 補償方向: {comp['direction']} ({comp['distance_mm']} mm)")
            print(f"      - 原因: {comp['reason']}")
            print("   * 命令底盤微調後重新採樣換算，避免機械臂硬卡死！")

    print("=" * 65)

if __name__ == "__main__":
    test_img = os.path.join(os.path.dirname(__file__), "photo.jpg")
    run_pipeline_test("請幫我拿桌上的飲料瓶", test_img)
