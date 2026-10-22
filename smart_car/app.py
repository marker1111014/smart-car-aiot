# app.py
# AIoT 智能小車中控中心：ESP32 上傳照片 ➔ Gemini 決策 ➔ 直接由 HTTP 回應回傳 JSON
import os
import sys
import io
import re
import json
import time
import socket
import asyncio
import traceback
import httpx
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image, ImageFile, ImageDraw

from schema import RobotResponse, ESP32Command
from gemini_brain import GeminiSemanticBrain
from vision_tracker import VisualServoTracker, DEVICE
from arm_kinematics import ArmKinematics

# 容忍相機傳輸時輕微截斷之 JPEG 影像
ImageFile.LOAD_TRUNCATED_IMAGES = True

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

app = FastAPI(title="AIoT Robot Control Hub")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

latest_image_bytes: Optional[bytes] = None
latest_img_path = os.path.join(STATIC_DIR, "latest.jpg")
if not os.path.exists(latest_img_path):
    default_photo = os.path.join(BASE_DIR, "photo.jpg")
    if os.path.exists(default_photo):
        import shutil
        shutil.copy(default_photo, latest_img_path)

if os.path.exists(latest_img_path):
    try:
        with open(latest_img_path, "rb") as f:
            latest_image_bytes = f.read()
    except Exception:
        pass

# ======================================================
# 1. 核心 AI 模組初始化 (語意大腦 + YOLO 視覺追蹤 + IK 求解)
# ======================================================
brain = GeminiSemanticBrain()
tracker = VisualServoTracker()
arm_solver = ArmKinematics()

current_target_class = "cup"
current_target_candidates = ["cup", "bottle", "bowl"]
current_target_height = 110.0
current_target_zh = "杯子"
target_item_prompt = "杯子 (cup)"
latest_all_detected: list = []
latest_detected_summary: str = "尚無"

search_step = 0
SEARCH_ROTATION_STEPS = 8  # 8 次原地左轉 (每次 35°，覆蓋 ~280°~360°)
SEARCH_FORWARD_STEPS = 1   # 環景掃描後前進換位探索

stream_enabled = True
gemini_connected = True
is_analyzing = False
last_ai_latency = 0.0
last_ai_request_time = 0.0

# ======================================================
# 動態請求間隔 (Dynamic Throttling) 設定
# ======================================================
DEFAULT_AI_INTERVAL = 2.5
MAX_AI_INTERVAL = 8.0
current_ai_interval = DEFAULT_AI_INTERVAL
consecutive_api_successes = 0

last_frame_time = 0.0
frame_count = 0

# ======================================================
# 2. ESP32-S3 底層規範指令與 JSON 文字檔持久化管理
# ======================================================
COMMAND_JSON_PATH = os.path.join(BASE_DIR, "command.json")
ROOT_COMMAND_JSON_PATH = os.path.join(os.path.dirname(BASE_DIR), "command.json")
ESP32_COMMAND_URL = os.environ.get("ESP32_COMMAND_URL", os.environ.get("ROBOT_DEVICE_URL", "http://192.168.4.1/api/command"))
ENABLE_DIRECT_FORWARD = os.environ.get("ENABLE_DIRECT_FORWARD", "false").lower() == "true"

feedbacks: List[Dict[str, str]] = [
    {"time": time.strftime("%H:%M:%S"), "text": "🚀 系統啟動：AI 上位機遵循 ESP32-S3 HTTP JSON API 規範 (模式 A ~ E)"}
]

def add_feedback(msg: str):
    global feedbacks
    feedbacks.append({"time": time.strftime("%H:%M:%S"), "text": msg})
    if len(feedbacks) > 60:
        feedbacks = feedbacks[-60:]

def save_command_json(payload: dict):
    """
    將最新決策指令以嚴格符合 ESP32-S3 規範的 JSON 文字檔格式寫入本地磁碟 (command.json)
    供外部程式、AI 模組或日誌監聽對接
    """
    targets = [
        COMMAND_JSON_PATH,
        ROOT_COMMAND_JSON_PATH,
        os.path.join(BASE_DIR, "latest_command.json"),
        os.path.join(os.path.dirname(BASE_DIR), "latest_command.json")
    ]
    for target_path in targets:
        try:
            with open(target_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️ 寫入 JSON 文字檔 {target_path} 失敗: {e}")

# 全域狀態與規範指令物件
latest_command: dict = {"action": "brake", "speed": 0}
latest_target_found: bool = False
latest_target_desc: str = "系統待命中"
latest_search_mode: str = "INITIALIZING"

# 為相容舊版介面暫存之綜合字典
latest_response: dict = {
    "stream_enabled": True,
    "target_found": False,
    "target_description": "系統初始化",
    "search_mode": "INITIALIZING",
    "command": latest_command
}

async def forward_to_esp32(payload: dict):
    """
    將符合規範的標準 JSON 指令透過 HTTP POST 發送給 ESP32-S3 (若啟用直接推播模式)
    """
    if not payload or not ENABLE_DIRECT_FORWARD:
        return

    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            await client.post(
                ESP32_COMMAND_URL,
                json=payload,
                headers={"Content-Type": "application/json"}
            )
    except Exception:
        pass

def dispatch_command_async(payload: dict):
    """非同步派發任務 (若啟用主動推播模式)"""
    if not ENABLE_DIRECT_FORWARD:
        return
    try:
        data_copy = dict(payload)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(forward_to_esp32(data_copy))
        except RuntimeError:
            pass
    except Exception:
        pass

def set_active_command(cmd_payload: dict, desc: str = "", target_found: bool = False, mode: str = "SCANNING"):
    """
    統一更新上位機決策指令：
    1. 更新全域變數 latest_command (嚴格符合規範之 JSON)
    2. 自動寫入 command.json 與 latest_command.json 本地文字檔
    3. 非同步推播至 ESP32-S3 控制端
    4. 同步更新 Dashboard 狀態
    """
    global latest_command, latest_target_found, latest_target_desc, latest_search_mode, latest_response
    latest_command = dict(cmd_payload)
    latest_target_found = target_found
    latest_target_desc = desc
    latest_search_mode = mode

    # 寫入規範 JSON 文字檔
    save_command_json(latest_command)

    # 同步相容字典
    latest_response = {
        "stream_enabled": stream_enabled,
        "target_found": target_found,
        "target_description": desc,
        "search_mode": mode,
        "command": latest_command
    }

    # 非同步發送給 ESP32
    dispatch_command_async(latest_command)

def generate_search_command() -> dict:
    """
    自主搜尋巡航狀態機 (符合 ESP32 規範模式 A：底盤車輛運動控制)：
    [原地左轉 35° / 700ms] x 8 次 ➔ [向前微調 15cm / 750ms 換位探索] ➔ 循環繼續
    """
    global search_step, stream_enabled
    cycle_length = SEARCH_ROTATION_STEPS + SEARCH_FORWARD_STEPS
    step_in_cycle = search_step % cycle_length
    search_step += 1
    
    if step_in_cycle < SEARCH_ROTATION_STEPS:
        deg = 35
        step_num = step_in_cycle + 1
        desc = f"🔍 [全景巡航 {step_num}/{SEARCH_ROTATION_STEPS}] 原地左轉 {deg}° 廣角搜尋【{target_item_prompt}】"
        # 模式 A: 底盤運動控制
        cmd_payload = ESP32Command(action="spin_left", speed=200, duration=700).to_payload()
        mode = "SCANNING"
    else:
        dist = 15
        cycle_num = (search_step // cycle_length)
        desc = f"🚗 [換位探索 #{cycle_num}] 環顧未發現，向前微調 {dist}cm 擴大探索範圍"
        # 模式 A: 底盤運動控制
        cmd_payload = ESP32Command(action="forward", speed=200, duration=750).to_payload()
        mode = "EXPLORING"

    set_active_command(cmd_payload, desc=desc, target_found=False, mode=mode)
    return cmd_payload

# 伺服器啟動時即產生首個巡航指令並寫入 JSON 文字檔
latest_command = generate_search_command()

# ======================================================
# 3. Gemini 提示詞與背景多模態分析 (對接標準 API 規範)
# ======================================================
def get_system_instruction(target_item: str):
    return f"""
你是一個 AIoT 尋物小車與機械手臂的即時視覺決策大腦。
目前任務：在相機傳來的即時畫面中尋找並定位目標物【{target_item}】。

請根據畫面視覺辨識結果，嚴格輸出符合《ESP32-S3 底層控制端 HTTP JSON API 規範》的指令：
1. 畫面中是否存在【{target_item}】(target_found: true / false)。
2. 若【發現目標】(target_found = true)：
   - 估計目標在視野中的水平位置 (偏左 / 置中 / 偏右) 與距離 (cm)。
   - 依據下列規則輸出 command (符合模式 A / B / D / E)：
     * 若目標偏左：輸出模式 A 左旋微調對齊：{{"action": "spin_left", "speed": 180, "duration": 300}}
     * 若目標偏右：輸出模式 A 右旋微調對齊：{{"action": "spin_right", "speed": 180, "duration": 300}}
     * 若置中且距離 > 12cm：輸出模式 A 前進接近：{{"action": "forward", "speed": 200, "duration": 600}}
     * 若置中且距離 <= 12cm：輸出模式 D 手臂地面低位抓取：{{"preset": "ground"}} (亦可傳入模式 B 空間座標 x, z)
3. 若【未發現目標】(target_found = false)：
   - target_description 簡短說明看到的視野畫面。
   - command 輸出模式 A 全景旋轉掃描指令：{{"action": "spin_left", "speed": 200, "duration": 700}}。
4. 必須嚴格遵照 Response Schema 輸出。
"""

def analyze_image_with_gemini(image: Image.Image, target_item: str, model_name: str) -> str:
    prompt_text = f"這是 ESP32-CAM 傳來的即時照片，請尋找【{target_item}】並下達符合規範的 JSON 控制指令："
    response = client.models.generate_content(
        model=model_name,
        contents=[prompt_text, image],
        config=types.GenerateContentConfig(
            system_instruction=get_system_instruction(target_item),
            response_mime_type="application/json",
            response_schema=RobotResponse,
            temperature=0.1,
        ),
    )
    return response.text

def is_transient_error(err: Exception) -> bool:
    """判斷是否為 503 (UNAVAILABLE), 429 (Rate Limit), 504 (DEADLINE_EXCEEDED), Timeout 等暫態錯誤"""
    if isinstance(err, (TimeoutError, asyncio.TimeoutError)):
        return True
    msg = str(err).lower().strip()
    if not msg:
        return True
    return any(keyword in msg for keyword in [
        "503", "429", "504", "unavailable", "resource_exhausted",
        "high demand", "too many requests", "rate limit", "deadline exceeded", "overloaded", "timeout"
    ])

async def call_gemini_with_backoff(img: Image.Image, target_item: str, model_name: str) -> tuple[Optional[str], Optional[Exception]]:
    """
    指數退避重試 (Exponential Backoff & Retry)
    """
    global current_ai_interval, consecutive_api_successes
    backoff_delays = [1.0, 2.0, 4.0]
    last_exception = None

    for attempt in range(len(backoff_delays) + 1):
        try:
            response_text = await asyncio.wait_for(
                asyncio.to_thread(analyze_image_with_gemini, img, target_item, model_name),
                timeout=28.0
            )
            return response_text, None
        except Exception as e:
            last_exception = e
            if attempt >= len(backoff_delays) or not is_transient_error(e):
                break

            delay = backoff_delays[attempt]
            consecutive_api_successes = 0
            if current_ai_interval < 5.0:
                current_ai_interval = 5.0
            elif current_ai_interval < 8.0:
                current_ai_interval = 8.0

            add_feedback(
                f"⏳ Gemini 繁忙 ({model_name} 遇暫態錯誤)，啟動第 {attempt + 1}/3 次退避重試 (等候 {delay}s，請求間隔拉長至 {current_ai_interval}s)..."
            )
            await asyncio.sleep(delay)

    return None, last_exception

async def process_frame_in_background(img: Image.Image):
    """
    背景非同步影像分析：整合三項穩健性機制，並直接產出規範 JSON
    """
    global is_analyzing, current_model_idx, gemini_connected
    global search_step, last_ai_latency, stream_enabled, current_ai_interval, consecutive_api_successes
    if is_analyzing or not stream_enabled:
        return
    is_analyzing = True
    start_t = time.time()
    success = False

    try:
        total_models = len(MODELS_TO_TRY)
        for _ in range(total_models):
            m_name = MODELS_TO_TRY[current_model_idx]
            response_text, err = await call_gemini_with_backoff(img, target_item_prompt, m_name)

            if response_text is not None:
                elapsed = time.time() - start_t
                last_ai_latency = round(elapsed, 2)
                
                cleaned_text = response_text.strip()
                if cleaned_text.startswith("```"):
                    cleaned_text = re.sub(r"^```(?:json)?\s*", "", cleaned_text, flags=re.MULTILINE)
                    cleaned_text = re.sub(r"\s*```$", "", cleaned_text, flags=re.MULTILINE)
                    
                parsed = RobotResponse.model_validate_json(cleaned_text)
                
                if not stream_enabled:
                    return

                consecutive_api_successes += 1
                if current_ai_interval > DEFAULT_AI_INTERVAL and consecutive_api_successes >= 2:
                    current_ai_interval = max(DEFAULT_AI_INTERVAL, round(current_ai_interval - 0.8, 1))

                cmd_payload = parsed.command.to_payload()

                if parsed.target_found:
                    search_step = 0
                    set_active_command(cmd_payload, desc=parsed.target_description, target_found=True, mode="LOCKED")
                    add_feedback(f"🎯 [鎖定目標!] ({last_ai_latency}s): {parsed.target_description} | 指令: {cmd_payload}")
                else:
                    # 巡航掃描
                    search_cmd = generate_search_command()
                    add_feedback(f"🔍 [巡航搜尋] ({last_ai_latency}s): {latest_target_desc} | 指令: {search_cmd}")

                gemini_connected = True
                success = True
                break
            else:
                prev_model = m_name
                current_model_idx = (current_model_idx + 1) % total_models
                new_model = MODELS_TO_TRY[current_model_idx]
                err_brief = (str(err).strip() or type(err).__name__) if err else "連線或逾時"
                add_feedback(f"⚠️ {prev_model} 重試均異常，切換備援模型 ➔ {new_model} ({err_brief})")

        # 安全防護 (Safe Fallback)
        if not success and stream_enabled:
            gemini_connected = False
            consecutive_api_successes = 0
            current_ai_interval = MAX_AI_INTERVAL
            safe_payload = ESP32Command(action="brake", speed=0).to_payload()
            set_active_command(safe_payload, desc="⚠️ API 嚴重壅塞，小車暫時安全停車", target_found=False, mode="SAFE_STOP")
            add_feedback("⚠️ API 嚴重壅塞，小車暫時安全停車")

    except Exception as e:
        traceback.print_exc()
        if stream_enabled:
            safe_payload = ESP32Command(action="brake", speed=0).to_payload()
            set_active_command(safe_payload, desc="⚠️ 處理發生異常，小車安全停車", target_found=False, mode="SAFE_STOP")
            add_feedback(f"⚠️ 異常安全停車: {e}")
    finally:
        is_analyzing = False

# ======================================================
# 3. 請求體模型定義
# ======================================================
class TargetRequest(BaseModel):
    target: str

class StreamControlRequest(BaseModel):
    enabled: bool

class LLMConfigRequest(BaseModel):
    api_url: str = "https://apihub.agnes-ai.com/v1/chat/completions"
    api_key: Optional[str] = None
    model: str = "agnes-2.5-flash"
    enabled: bool = True
    provider: str = "agnes"

class LLMTestRequest(BaseModel):
    prompt: Optional[str] = "手把"

# ======================================================
# 4. 現代化超低延遲 Dashboard 網頁介面
# ======================================================
HTML_CONTENT = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AIoT 尋物小車中控中心</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root {
            --bg-color: #0b0f17;
            --card-bg: rgba(18, 24, 38, 0.78);
            --border-color: rgba(255, 255, 255, 0.08);
            --accent-cyan: #38bdf8;
            --accent-green: #10b981;
            --accent-amber: #f59e0b;
            --accent-red: #ef4444;
            --accent-purple: #a855f7;
            --text-main: #f8fafc;
            --text-sub: #94a3b8;
            --json-bg: #070a10;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', system-ui, -apple-system, sans-serif;
            background-color: var(--bg-color);
            background-image: 
                radial-gradient(at 0% 0%, rgba(56, 189, 248, 0.12) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(168, 85, 247, 0.08) 0px, transparent 50%);
            color: var(--text-main);
            min-height: 100vh;
            padding: 24px;
            overflow-x: hidden;
        }

        header {
            max-width: 1200px;
            margin: 0 auto 20px auto;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--border-color);
        }

        .title-group { display: flex; align-items: center; gap: 12px; }
        .title-group i { font-size: 1.8rem; color: var(--accent-cyan); }
        h1 { font-size: 1.45rem; font-weight: 700; letter-spacing: -0.02em; }

        .status-strip {
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }

        .badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--border-color);
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 0.82rem;
            font-weight: 500;
        }

        .pulse-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background-color: var(--accent-green);
            box-shadow: 0 0 8px var(--accent-green);
            animation: pulse 2s infinite;
        }
        .pulse-dot.paused {
            background-color: var(--accent-amber);
            box-shadow: 0 0 8px var(--accent-amber);
        }
        @keyframes pulse {
            0% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.4; transform: scale(1.2); }
            100% { opacity: 1; transform: scale(1); }
        }

        .network-info-bar {
            max-width: 1200px;
            margin: 0 auto 16px auto;
            background: rgba(30, 41, 59, 0.45);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 10px 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
            font-size: 0.85rem;
            color: var(--text-sub);
        }
        .network-info-bar code {
            font-family: 'JetBrains Mono', monospace;
            background: rgba(0, 0, 0, 0.4);
            padding: 3px 8px;
            border-radius: 6px;
            color: var(--accent-cyan);
        }

        .control-master-card {
            max-width: 1200px;
            margin: 0 auto 16px auto;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 16px 22px;
            backdrop-filter: blur(14px);
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.25);
            transition: all 0.3s ease;
        }
        .control-master-card.active-state {
            border-color: rgba(16, 185, 129, 0.3);
            background: linear-gradient(135deg, rgba(16, 185, 129, 0.08) 0%, rgba(18, 24, 38, 0.8) 100%);
        }
        .control-master-card.paused-state {
            border-color: rgba(245, 158, 11, 0.3);
            background: linear-gradient(135deg, rgba(245, 158, 11, 0.08) 0%, rgba(18, 24, 38, 0.8) 100%);
        }

        .master-info { display: flex; align-items: center; gap: 14px; }
        .master-icon-box {
            width: 48px;
            height: 48px;
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.4rem;
        }
        .icon-active {
            background: rgba(16, 185, 129, 0.15);
            color: var(--accent-green);
            border: 1px solid rgba(16, 185, 129, 0.3);
        }
        .icon-paused {
            background: rgba(245, 158, 11, 0.15);
            color: var(--accent-amber);
            border: 1px solid rgba(245, 158, 11, 0.3);
        }

        .master-text h3 {
            font-size: 1.05rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .master-text p {
            font-size: 0.82rem;
            color: var(--text-sub);
            margin-top: 3px;
        }

        .master-btn-group {
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }

        .btn-toggle-main {
            padding: 10px 20px;
            font-size: 0.92rem;
            font-weight: 600;
            border-radius: 10px;
            border: none;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            transition: all 0.2s;
        }
        .btn-pause-mode { background: linear-gradient(135deg, #f59e0b, #d97706); color: #fff; }
        .btn-start-mode { background: linear-gradient(135deg, #10b981, #059669); color: #fff; }
        .btn-quick {
            padding: 10px 16px;
            font-size: 0.85rem;
            font-weight: 500;
            border-radius: 10px;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            color: var(--text-main);
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            transition: all 0.2s;
        }
        .btn-quick:hover { background: rgba(255, 255, 255, 0.1); }
        .btn-quick.danger:hover {
            background: rgba(239, 68, 68, 0.2);
            border-color: var(--accent-red);
            color: #fca5a5;
        }

        .search-target-card {
            max-width: 1200px;
            margin: 0 auto 20px auto;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 16px 22px;
            backdrop-filter: blur(14px);
        }
        .search-box {
            display: flex;
            gap: 10px;
            margin-top: 10px;
        }
        .search-box input {
            flex: 1;
            padding: 10px 16px;
            background: rgba(7, 10, 16, 0.85);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            color: #fff;
            font-size: 0.92rem;
            outline: none;
        }
        .search-box button {
            padding: 10px 20px;
            background: var(--accent-cyan);
            border: none;
            border-radius: 10px;
            color: #070a10;
            font-weight: 600;
            font-size: 0.92rem;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            transition: all 0.2s;
        }
        .search-box button:hover { opacity: 0.9; transform: translateY(-1px); }

        .container {
            max-width: 1200px;
            margin: 0 auto;
            display: grid;
            grid-template-columns: 1.15fr 0.85fr;
            gap: 20px;
        }

        .card {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 20px;
            backdrop-filter: blur(14px);
        }
        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
            padding-bottom: 12px;
            border-bottom: 1px solid var(--border-color);
        }
        .card-title {
            font-size: 1.05rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .card-title i { color: var(--accent-cyan); }

        .img-wrapper {
            position: relative;
            width: 100%;
            aspect-ratio: 4/3;
            background: #000;
            border-radius: 12px;
            overflow: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
            border: 1px solid var(--border-color);
        }
        .img-wrapper img {
            width: 100%;
            height: 100%;
            object-fit: contain;
            transition: opacity 0.3s;
        }
        .img-wrapper img.dimmed { opacity: 0.25; filter: grayscale(80%); }

        .video-paused-overlay {
            position: absolute;
            inset: 0;
            background: rgba(8, 12, 20, 0.85);
            backdrop-filter: blur(4px);
            display: none;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 10px;
            text-align: center;
            padding: 20px;
            z-index: 10;
        }
        .video-paused-overlay.show { display: flex; }

        .mode-banner {
            margin-top: 14px;
            padding: 12px 16px;
            border-radius: 10px;
            font-size: 0.88rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .mode-scanning { background: rgba(245, 158, 11, 0.12); color: var(--accent-amber); border: 1px solid rgba(245, 158, 11, 0.3); }
        .mode-locked { background: rgba(16, 185, 129, 0.12); color: var(--accent-green); border: 1px solid rgba(16, 185, 129, 0.3); }
        .mode-exploring { background: rgba(168, 85, 247, 0.12); color: var(--accent-purple); border: 1px solid rgba(168, 85, 247, 0.3); }
        .mode-paused { background: rgba(239, 68, 68, 0.12); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }
        .mode-safestop { background: rgba(239, 68, 68, 0.18); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.5); }

        .json-box {
            background: var(--json-bg);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 14px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.82rem;
            color: #cbd5e1;
            line-height: 1.45;
            overflow-x: auto;
            max-height: 240px;
            margin-top: 14px;
            white-space: pre-wrap;
            word-break: break-all;
        }

        .feedback-box {
            height: 480px;
            overflow-y: auto;
            background: var(--json-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 14px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .feedback-item {
            background: rgba(30, 41, 59, 0.6);
            border-left: 3px solid var(--accent-cyan);
            border-radius: 8px;
            padding: 10px 14px;
            font-size: 0.84rem;
            line-height: 1.4;
            word-break: break-word;
        }
        .feedback-time {
            font-size: 0.72rem;
            color: var(--text-sub);
            margin-bottom: 4px;
        }

        @media (max-width: 860px) {
            .container { grid-template-columns: 1fr; }
            .control-master-card { flex-direction: column; align-items: flex-start; }
            .master-btn-group { width: 100%; justify-content: flex-start; }
        }

        /* 自訂 LLM 設定 Modal */
        .modal-backdrop {
            display: none;
            position: fixed;
            top: 0; left: 0; width: 100vw; height: 100vh;
            background: rgba(0, 0, 0, 0.75);
            backdrop-filter: blur(8px);
            z-index: 9999;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .modal-backdrop.open {
            display: flex;
            animation: fadeIn 0.2s ease-out;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: scale(0.96); }
            to { opacity: 1; transform: scale(1); }
        }
        .modal-card {
            background: #0f1523;
            border: 1px solid rgba(168, 85, 247, 0.35);
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6), 0 0 30px rgba(168, 85, 247, 0.15);
            border-radius: 20px;
            width: 100%;
            max-width: 580px;
            max-height: 90vh;
            overflow-y: auto;
            padding: 24px 28px;
            color: #f8fafc;
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 14px;
            border-bottom: 1px solid var(--border-color);
            margin-bottom: 16px;
        }
        .modal-header h3 {
            font-size: 1.15rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 10px;
            color: #f8fafc;
        }
        .modal-close-btn {
            background: transparent;
            border: none;
            color: var(--text-sub);
            font-size: 1.4rem;
            cursor: pointer;
            padding: 4px 8px;
            border-radius: 8px;
            transition: all 0.2s;
        }
        .modal-close-btn:hover {
            color: #fff;
            background: rgba(255, 255, 255, 0.08);
        }
        .preset-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
            margin-bottom: 16px;
        }
        .preset-btn {
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 10px 14px;
            cursor: pointer;
            text-align: left;
            transition: all 0.2s;
            display: flex;
            flex-direction: column;
            gap: 4px;
        }
        .preset-btn:hover {
            background: rgba(168, 85, 247, 0.12);
            border-color: rgba(168, 85, 247, 0.4);
            transform: translateY(-1px);
        }
        .preset-title {
            font-size: 0.88rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 6px;
            color: #fff;
        }
        .preset-desc {
            font-size: 0.74rem;
            color: var(--text-sub);
        }
        .form-group {
            margin-bottom: 14px;
        }
        .form-label {
            display: block;
            font-size: 0.82rem;
            font-weight: 600;
            margin-bottom: 6px;
            color: var(--text-sub);
        }
        .form-input {
            width: 100%;
            padding: 10px 14px;
            background: rgba(7, 10, 16, 0.9);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            color: #fff;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.85rem;
            outline: none;
            transition: border-color 0.2s;
        }
        .form-input:focus {
            border-color: var(--accent-purple);
            box-shadow: 0 0 0 2px rgba(168, 85, 247, 0.2);
        }
        .modal-tip {
            font-size: 0.78rem;
            color: #94a3b8;
            background: rgba(168, 85, 247, 0.08);
            border: 1px solid rgba(168, 85, 247, 0.2);
            border-radius: 10px;
            padding: 10px 14px;
            margin-bottom: 16px;
            line-height: 1.5;
        }
        .modal-tip strong { color: #d8b4fe; }
        .modal-actions {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
            margin-top: 18px;
            padding-top: 14px;
            border-top: 1px solid var(--border-color);
        }
        .test-res-box {
            display: none;
            margin-top: 12px;
            padding: 10px 14px;
            border-radius: 10px;
            font-size: 0.82rem;
            line-height: 1.4;
            font-family: 'JetBrains Mono', monospace;
        }
        .test-res-box.success {
            display: block;
            background: rgba(16, 185, 129, 0.12);
            border: 1px solid rgba(16, 185, 129, 0.4);
            color: #6ee7b7;
        }
        .test-res-box.error {
            display: block;
            background: rgba(239, 68, 68, 0.12);
            border: 1px solid rgba(239, 68, 68, 0.4);
            color: #fca5a5;
        }
    </style>
</head>
<body>

    <header>
        <div class="title-group">
            <i class="fa-solid fa-robot"></i>
            <h1>AIoT 尋物小車超低延遲控制中心</h1>
        </div>
        <div class="status-strip">
            <div class="badge">
                <span id="header-pulse" class="pulse-dot"></span>
                <span id="server-status">伺服器：在線</span>
            </div>
            <div class="badge">
                <i class="fa-solid fa-bolt" style="color: var(--accent-amber);"></i>
                <span>回傳延遲: <strong id="latency-val" style="color: var(--accent-green);">&lt; 10ms</strong></span>
            </div>
            <div class="badge" onclick="openLlmModal()" style="cursor: pointer; border-color: rgba(168, 85, 247, 0.4); background: rgba(168, 85, 247, 0.1);" title="點擊設定自訂 LLM 大腦 (Agnes 2.5 Flash / DeepSeek / OpenAI / Ollama)">
                <i class="fa-solid fa-brain" style="color: var(--accent-purple);"></i>
                <span id="gemini-status">大腦：載入中...</span>
                <i class="fa-solid fa-gear" style="font-size: 0.72rem; color: var(--accent-purple); margin-left: 2px;"></i>
            </div>
        </div>
    </header>

    <div class="network-info-bar">
        <div>🌐 本機伺服器：<code id="lan-url">載入中...</code></div>
        <div>📡 ESP32 鏡頭：<code id="upload-url">載入中...</code></div>
        <div>🤖 控制設備：<code>http://172.20.10.4/api/command</code> (記憶體非同步自動推播)</div>
        <div>⚡ 狀態：<strong id="stream-subtext" style="color: var(--accent-green);">鏡頭拍照與自動回傳機制運作中</strong></div>
    </div>

    <!-- 鏡頭開關與小車運行中控總開關面板 -->
    <div id="master-panel" class="control-master-card active-state">
        <div class="master-info">
            <div id="master-icon" class="master-icon-box icon-active">
                <i id="master-fa" class="fa-solid fa-video"></i>
            </div>
            <div class="master-text">
                <h3 id="master-title">
                    <span>ESP32 鏡頭與回傳機制</span>
                    <span id="master-pill" style="font-size: 0.75rem; padding: 2px 8px; border-radius: 12px; background: rgba(16,185,129,0.2); color: var(--accent-green);">傳輸中</span>
                </h3>
                <p id="master-desc">正在即時接收 ESP32 鏡頭畫面並於 HTTP 回應直接回饋 Gemini 決策 JSON</p>
            </div>
        </div>
        <div class="master-btn-group">
            <button id="toggle-btn" class="btn-toggle-main btn-pause-mode" onclick="toggleStream()">
                <i class="fa-solid fa-pause"></i> <span>暫停相機與回傳</span>
            </button>
            <button class="btn-quick danger" onclick="emergencyStop()" title="立即下達剎車指令並關閉回傳">
                <i class="fa-solid fa-hand"></i> <span>緊急煞車</span>
            </button>
            <button class="btn-quick" onclick="resetArm()" title="手臂舵機回到90度待機狀態">
                <i class="fa-solid fa-rotate-left"></i> <span>手臂復位</span>
            </button>
        </div>
    </div>

    <!-- 尋找目標設定 -->
    <div class="search-target-card">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
            <div style="font-weight: 600; font-size: 0.95rem;">🎯 尋找目標設定</div>
            <div style="display: flex; align-items: center; gap: 12px;">
                <div style="font-size: 0.85rem; color: var(--text-sub);">目前追蹤：<strong id="target-label" style="color: var(--accent-cyan);">杯子 (cup)</strong></div>
                <button onclick="openLlmModal()" class="btn-quick" style="padding: 4px 10px; font-size: 0.78rem; border-color: rgba(168, 85, 247, 0.4); color: #d8b4fe;" title="自訂 OpenAI 相容 API (Agnes 2.5 Flash / DeepSeek / 本機模型)">
                    <i class="fa-solid fa-sliders"></i> LLM 大腦設定
                </button>
            </div>
        </div>
        <div class="search-box">
            <input type="text" id="target-input" placeholder="輸入目標物件名稱，例如：杯子、手把、手機、剪刀、電腦鍵盤..." onkeypress="if(event.key==='Enter') setTarget()">
            <button onclick="setTarget()"><i class="fa-solid fa-crosshairs"></i> 更新目標</button>
        </div>
        <div style="margin-top: 8px; font-size: 0.82rem; color: var(--text-sub); display: flex; align-items: center; gap: 8px;">
            <span style="color: var(--accent-cyan);"><i class="fa-solid fa-eye"></i> 鏡頭視野辨識到：</span>
            <span id="detected-objects-text" style="color: #38bdf8; font-weight: 500;">尚無檢測資料</span>
        </div>
    </div>

    <div class="container">
        <!-- 影像與即時動作狀態 -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">
                    <i class="fa-solid fa-video"></i>
                    <span>ESP32-CAM 即時影像</span>
                </div>
                <div style="display: flex; gap: 10px; align-items: center;">
                    <button onclick="reloadImageNow()" class="btn-quick" style="padding: 4px 10px; font-size: 0.75rem;"><i class="fa-solid fa-rotate-right"></i> 刷新畫面</button>
                    <span id="frame-stat" style="font-size: 0.78rem; color: var(--accent-amber);">尚未收到 ESP32 傳輸</span>
                    <div id="live-indicator" style="font-size: 0.8rem; color: var(--accent-green);">
                        <i class="fa-solid fa-circle-dot"></i> 即時連線
                    </div>
                </div>
            </div>
            
            <div class="img-wrapper">
                <img id="live-img" src="/api/latest_image" alt="ESP32 即時影像">
                <div id="video-overlay" class="video-paused-overlay">
                    <i class="fa-solid fa-circle-pause" style="font-size: 2.2rem; color: var(--accent-amber);"></i>
                    <h3 style="font-size: 1.15rem; font-weight: 600;">鏡頭拍攝與回傳已手動暫停</h3>
                    <p style="font-size: 0.82rem; color: var(--text-sub);">ESP32 處於省電休眠待命模式，小車已安全剎車。</p>
                    <button onclick="toggleStream()" class="btn-toggle-main btn-start-mode" style="margin-top: 6px;">
                        <i class="fa-solid fa-play"></i> 立即啟動傳輸
                    </button>
                </div>
            </div>

            <div id="mode-badge" class="mode-banner mode-scanning">
                <span id="mode-text"><i class="fa-solid fa-compass"></i> 全景旋轉掃描中...</span>
                <span id="ai-time-text" style="font-size: 0.8rem; font-weight: normal;">AI 耗時: 1.2s</span>
            </div>

            <div style="margin-top: 14px; font-weight: 600; font-size: 0.85rem; color: var(--text-sub);">
                符合 ESP32-S3 HTTP JSON API 規範之即時控制 JSON (模式 A ~ E)：
            </div>
            <pre id="json-output" class="json-box">{}</pre>
        </div>

        <!-- 系統決策日誌 -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">
                    <i class="fa-solid fa-terminal"></i>
                    <span>即時決策與巡航日誌</span>
                </div>
                <div style="font-size: 0.8rem; color: var(--text-sub);">自動捲動</div>
            </div>
            <div id="feedback-box" class="feedback-box"></div>
        </div>
    </div>

    <script>
        let currentStreamEnabled = true;
        let lastImageTimestamp = 0;
        function reloadImageNow() {
            const img = document.getElementById('live-img');
            if (img) {
                img.src = '/api/latest_image?t=' + Date.now();
            }
        }

        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                if (!res.ok) return;
                const d = await res.json();

                if (d.server_url) {
                    document.getElementById('lan-url').innerText = d.server_url;
                    document.getElementById('upload-url').innerText = d.server_url + '/api/upload';
                }

                if (d.target_item) {
                    document.getElementById('target-label').innerText = d.target_item;
                }

                if (d.detected_summary !== undefined) {
                    const el = document.getElementById('detected-objects-text');
                    if (el) el.innerText = d.detected_summary || "（視野內未檢出常見物品）";
                }

                if (d.stream_enabled !== undefined) {
                    updateStreamUI(d.stream_enabled);
                }

                const frameStat = document.getElementById('frame-stat');
                if (frameStat) {
                    if (d.frame_count > 0) {
                        frameStat.innerText = `⚡ 已接收: ${d.frame_count} 幀`;
                        frameStat.style.color = "var(--accent-green)";
                    } else {
                        frameStat.innerText = `⏳ 尚未收到 ESP32 傳輸`;
                        frameStat.style.color = "var(--accent-amber)";
                    }
                }

                if (d.data && d.data.server_latency_ms !== undefined) {
                    document.getElementById('latency-val').innerText = d.data.server_latency_ms + ' ms';
                }
                const aiSec = d.ai_latency_s ? d.ai_latency_s + 's' : '計算中';
                const aiInterval = d.ai_interval_s ? ' (動態間隔: ' + d.ai_interval_s + 's)' : '';
                document.getElementById('gemini-status').innerText = (d.active_model || '大腦就緒');
                document.getElementById('ai-time-text').innerText = 'AI 推理: ' + aiSec + aiInterval;

                const modeEl = document.getElementById('mode-badge');
                const modeTxt = document.getElementById('mode-text');
                const sm = d.search_mode || (d.data ? d.data.search_mode : "SCANNING");
                if (!d.stream_enabled || sm === "PAUSED") {
                    modeEl.className = "mode-banner mode-paused";
                    modeTxt.innerHTML = '<i class="fa-solid fa-circle-pause"></i> ⏸️ 系統暫停：停止拍攝與動作';
                } else if (sm === "SAFE_STOP") {
                    modeEl.className = "mode-banner mode-safestop";
                    modeTxt.innerHTML = '<i class="fa-solid fa-shield-halved"></i> ⚠️ API 嚴重壅塞：小車暫時安全停車待命';
                } else if (sm === "ARM_GRAB") {
                    modeEl.className = "mode-banner mode-locked";
                    modeTxt.innerHTML = '<i class="fa-solid fa-robot"></i> 🦾 已抵達抓取距離！手臂執行精準夾取！';
                } else if (sm === "COMPENSATING") {
                    modeEl.className = "mode-banner mode-exploring";
                    modeTxt.innerHTML = '<i class="fa-solid fa-arrows-up-down"></i> ⚠️ 觸發底盤位置補償調整';
                } else if (sm === "LOCKED" || sm === "TRACKING" || d.target_found) {
                    modeEl.className = "mode-banner mode-locked";
                    modeTxt.innerHTML = '<i class="fa-solid fa-bullseye"></i> 🎯 鎖定目標！' + (d.target_description || '視覺伺服閉環推進中');
                } else if (sm === "EXPLORING") {
                    modeEl.className = "mode-banner mode-exploring";
                    modeTxt.innerHTML = '<i class="fa-solid fa-car"></i> 🚗 360°未發現，向前微調換位探索';
                } else {
                    modeEl.className = "mode-banner mode-scanning";
                    modeTxt.innerHTML = '<i class="fa-solid fa-compass"></i> 🔍 未發現目標，全景旋轉掃描中';
                }

                if (d.data) {
                    document.getElementById('json-output').innerText = JSON.stringify(d.data, null, 2);
                }

                if (d.stream_enabled) {
                    const img = document.getElementById('live-img');
                    if (img && d.last_frame_time && d.last_frame_time !== lastImageTimestamp) {
                        lastImageTimestamp = d.last_frame_time;
                        img.src = '/api/latest_image?t=' + d.last_frame_time;
                    }
                }

                if (d.feedbacks) {
                    renderFeedbacks(d.feedbacks);
                }
            } catch (err) {
                console.error("fetchStatus error", err);
            }
        }

        function updateStreamUI(enabled) {
            currentStreamEnabled = enabled;
            const panel = document.getElementById('master-panel');
            const icon = document.getElementById('master-icon');
            const fa = document.getElementById('master-fa');
            const pill = document.getElementById('master-pill');
            const desc = document.getElementById('master-desc');
            const btn = document.getElementById('toggle-btn');
            const overlay = document.getElementById('video-overlay');
            const img = document.getElementById('live-img');
            const pulse = document.getElementById('header-pulse');
            const liveInd = document.getElementById('live-indicator');
            const subtext = document.getElementById('stream-subtext');

            if (enabled) {
                panel.className = "control-master-card active-state";
                icon.className = "master-icon-box icon-active";
                fa.className = "fa-solid fa-video";
                pill.style.background = "rgba(16,185,129,0.2)";
                pill.style.color = "var(--accent-green)";
                pill.innerText = "傳輸中";
                desc.innerText = "正在即時接收 ESP32 鏡頭畫面並於 HTTP 回應直接回饋 Gemini 決策 JSON";

                btn.className = "btn-toggle-main btn-pause-mode";
                btn.innerHTML = '<i class="fa-solid fa-pause"></i> <span>暫停相機與回傳</span>';

                overlay.className = "video-paused-overlay";
                img.className = "";
                pulse.className = "pulse-dot";
                liveInd.style.color = "var(--accent-green)";
                liveInd.innerHTML = '<i class="fa-solid fa-circle-dot"></i> 即時連線';
                subtext.style.color = "var(--accent-green)";
                subtext.innerText = "鏡頭拍照與自動回傳機制運作中";
            } else {
                panel.className = "control-master-card paused-state";
                icon.className = "master-icon-box icon-paused";
                fa.className = "fa-solid fa-video-slash";
                pill.style.background = "rgba(245,158,11,0.2)";
                pill.style.color = "var(--accent-amber)";
                pill.innerText = "已休眠";
                desc.innerText = "已暫停鏡頭拍攝與上傳，ESP32 處於低功耗待命模式，小車已煞車";

                btn.className = "btn-toggle-main btn-start-mode";
                btn.innerHTML = '<i class="fa-solid fa-play"></i> <span>啟動相機與回傳</span>';

                overlay.className = "video-paused-overlay show";
                img.className = "dimmed";
                pulse.className = "pulse-dot paused";
                liveInd.style.color = "var(--accent-amber)";
                liveInd.innerHTML = '<i class="fa-solid fa-circle-pause"></i> 待命休眠中';
                subtext.style.color = "var(--accent-amber)";
                subtext.innerText = "鏡頭傳輸已關閉 (省電待命中)";
            }
        }

        async function toggleStream() {
            try {
                const targetState = !currentStreamEnabled;
                const res = await fetch('/api/set_stream_state', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: targetState })
                });
                if (res.ok) {
                    fetchStatus();
                }
            } catch (err) {
                alert("切換相機狀態失敗: " + err);
            }
        }

        async function emergencyStop() {
            try {
                await fetch('/api/emergency_stop', { method: 'POST' });
                fetchStatus();
            } catch (err) {
                alert("煞車指令發送失敗: " + err);
            }
        }

        async function resetArm() {
            try {
                await fetch('/api/reset_arm', { method: 'POST' });
                fetchStatus();
            } catch (err) {
                alert("手臂復位失敗: " + err);
            }
        }

        function renderFeedbacks(list) {
            const box = document.getElementById('feedback-box');
            box.innerHTML = '';
            list.slice().reverse().forEach(fb => {
                const div = document.createElement('div');
                div.className = 'feedback-item';
                div.innerHTML = `<div class="feedback-time">${fb.time}</div><div>${fb.text}</div>`;
                box.appendChild(div);
            });
        }

        async function setTarget() {
            const input = document.getElementById('target-input');
            const target = input.value.trim();
            if (!target) return alert("請輸入目標！");
            try {
                const res = await fetch('/api/set_target', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ target: target })
                });
                if (res.ok) {
                    input.value = "";
                    fetchStatus();
                }
            } catch (e) {
                alert("設定失敗");
            }
        }

        // ==========================================
        // 自訂 LLM 大腦 (Agnes 2.5 Flash / OpenAI) 前端邏輯
        // ==========================================
        let currentLlmProvider = "agnes";

        const LLM_PRESETS = {
            agnes: {
                provider: "agnes",
                url: "https://apihub.agnes-ai.com/v1/chat/completions",
                model: "agnes-2.5-flash"
            },
            deepseek: {
                provider: "deepseek",
                url: "https://api.deepseek.com/v1/chat/completions",
                model: "deepseek-chat"
            },
            openai: {
                provider: "openai",
                url: "https://api.openai.com/v1/chat/completions",
                model: "gpt-4o-mini"
            },
            ollama: {
                provider: "ollama",
                url: "http://localhost:11434/v1/chat/completions",
                model: "llama3.2"
            }
        };

        function openLlmModal() {
            document.getElementById('llm-modal').classList.add('open');
            loadLlmConfig();
        }

        function closeLlmModal() {
            document.getElementById('llm-modal').classList.remove('open');
            document.getElementById('llm-test-result').className = "test-res-box";
        }

        function applyLlmPreset(name) {
            currentLlmProvider = name;
            const p = LLM_PRESETS[name];
            if (p) {
                document.getElementById('llm-api-url').value = p.url;
                document.getElementById('llm-model-name').value = p.model;
                if (name === "ollama") {
                    document.getElementById('llm-api-key').placeholder = "本機 Ollama 免金鑰 (可留空)";
                } else {
                    document.getElementById('llm-api-key').placeholder = "請輸入 " + p.model + " 的 API Key";
                }
            }
        }

        async function loadLlmConfig() {
            try {
                const res = await fetch('/api/llm_config');
                if (res.ok) {
                    const data = await res.json();
                    document.getElementById('llm-api-url').value = data.api_url || "";
                    document.getElementById('llm-model-name').value = data.model || "";
                    document.getElementById('llm-enabled-check').checked = !!data.enabled;
                    currentLlmProvider = data.provider || "agnes";

                    const keyStatus = document.getElementById('llm-key-status');
                    if (data.has_key) {
                        keyStatus.innerText = "已設定 (" + data.api_key_masked + ")";
                        keyStatus.style.color = "var(--accent-green)";
                        document.getElementById('llm-api-key').placeholder = "已保存金鑰 (若不修改請留空)";
                    } else {
                        keyStatus.innerText = "未設定 (請填入金鑰)";
                        keyStatus.style.color = "var(--accent-amber)";
                        document.getElementById('llm-api-key').placeholder = "sk-xxxxxxxxxxxxxxxx";
                    }
                }
            } catch (err) {
                console.error("載入 LLM 設定失敗", err);
            }
        }

        async function saveLlmConfig() {
            const url = document.getElementById('llm-api-url').value.trim();
            const key = document.getElementById('llm-api-key').value.trim();
            const model = document.getElementById('llm-model-name').value.trim();
            const enabled = document.getElementById('llm-enabled-check').checked;

            if (!url) return alert("請填入 API 端點 URL！");
            if (!model) return alert("請填入 Model 名稱！");

            try {
                const payload = {
                    provider: currentLlmProvider,
                    api_url: url,
                    model: model,
                    enabled: enabled
                };
                if (key) {
                    payload.api_key = key;
                }
                const res = await fetch('/api/llm_config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                if (res.ok) {
                    alert("✅ LLM 設定已成功儲存並生效！");
                    closeLlmModal();
                    fetchStatus();
                } else {
                    const err = await res.json();
                    alert("儲存失敗: " + (err.detail || JSON.stringify(err)));
                }
            } catch (err) {
                alert("儲存網路例外: " + err);
            }
        }

        async function testLlmConnection() {
            const btn = document.getElementById('btn-test-llm');
            const resBox = document.getElementById('llm-test-result');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 測試中...';
            resBox.className = "test-res-box";

            const key = document.getElementById('llm-api-key').value.trim();
            const url = document.getElementById('llm-api-url').value.trim();
            const model = document.getElementById('llm-model-name').value.trim();
            if (key || url || model) {
                const payload = {
                    provider: currentLlmProvider,
                    api_url: url,
                    model: model,
                    enabled: true
                };
                if (key) payload.api_key = key;
                await fetch('/api/llm_config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
            }

            try {
                const res = await fetch('/api/test_llm', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ prompt: "手把" })
                });
                const data = await res.json();
                if (data.success) {
                    resBox.className = "test-res-box success";
                    resBox.innerHTML = `<strong>✅ 連線成功！</strong> (耗時 ${data.latency_s}s)<br>模型: ${data.model}<br>回覆: ${data.response_preview || 'OK'}`;
                } else {
                    resBox.className = "test-res-box error";
                    resBox.innerHTML = `<strong>❌ 連線測試失敗</strong> (耗時 ${data.latency_s || 0}s)<br>錯誤: ${data.error || '連線逾時'}`;
                }
            } catch (err) {
                resBox.className = "test-res-box error";
                resBox.innerHTML = `<strong>❌ 測試例外:</strong> ${err}`;
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-flask"></i> 測試連線';
            }
        }

        setInterval(fetchStatus, 800);
        fetchStatus();
    </script>

    <!-- 自訂 LLM 大腦設定 Modal -->
    <div id="llm-modal" class="modal-backdrop">
        <div class="modal-card">
            <div class="modal-header">
                <h3><i class="fa-solid fa-brain" style="color: var(--accent-purple);"></i> 自訂 LLM 語意大腦設定</h3>
                <button class="modal-close-btn" onclick="closeLlmModal()">&times;</button>
            </div>

            <div class="modal-tip">
                💡 <strong>提示：</strong>當使用者輸入未在本地字典中的物品（如「手把」、「剪刀」、「遙控器」）時，系統將即時調用您設定的 <strong>OpenAI 相容模型</strong>（例如 Agnes 2.5 Flash），自動轉換為 YOLO 標準標籤與幾何先驗。常見物品仍會走 0.1ms 極速本機規則，絕不浪費 Token！
            </div>

            <div style="font-size: 0.82rem; font-weight: 600; color: var(--text-sub); margin-bottom: 8px;">
                ⚡ 快速載入常用服務預設：
            </div>
            <div class="preset-grid">
                <div class="preset-btn" onclick="applyLlmPreset('agnes')">
                    <span class="preset-title"><i class="fa-solid fa-bolt" style="color: #a855f7;"></i> Agnes 2.5 Flash</span>
                    <span class="preset-desc">apihub.agnes-ai.com (推薦)</span>
                </div>
                <div class="preset-btn" onclick="applyLlmPreset('deepseek')">
                    <span class="preset-title"><i class="fa-solid fa-microchip" style="color: #38bdf8;"></i> DeepSeek Chat</span>
                    <span class="preset-desc">api.deepseek.com</span>
                </div>
                <div class="preset-btn" onclick="applyLlmPreset('openai')">
                    <span class="preset-title"><i class="fa-solid fa-robot" style="color: #10b981;"></i> OpenAI GPT-4o-mini</span>
                    <span class="preset-desc">api.openai.com</span>
                </div>
                <div class="preset-btn" onclick="applyLlmPreset('ollama')">
                    <span class="preset-title"><i class="fa-solid fa-server" style="color: #f59e0b;"></i> 本機 Ollama</span>
                    <span class="preset-desc">localhost:11434 (完全離線)</span>
                </div>
            </div>

            <div class="form-group">
                <label class="form-label">API 服務端點 URL (相容 OpenAI chat/completions)</label>
                <input type="text" id="llm-api-url" class="form-input" placeholder="https://apihub.agnes-ai.com/v1/chat/completions">
            </div>

            <div class="form-group">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                    <label class="form-label" style="margin-bottom:0;">API Key (安全存於本機 llm_config.json)</label>
                    <span id="llm-key-status" style="font-size: 0.75rem; color: var(--accent-amber);">未設定</span>
                </div>
                <input type="password" id="llm-api-key" class="form-input" placeholder="sk-xxxxxxxxxxxxxxxx" autocomplete="off">
            </div>

            <div class="form-group">
                <label class="form-label">模型識別碼 (Model ID)</label>
                <input type="text" id="llm-model-name" class="form-input" placeholder="agnes-2.5-flash">
            </div>

            <div style="display: flex; align-items: center; gap: 8px; margin: 12px 0;">
                <input type="checkbox" id="llm-enabled-check" checked style="accent-color: var(--accent-purple); width: 16px; height: 16px; cursor: pointer;">
                <label for="llm-enabled-check" style="font-size: 0.85rem; cursor: pointer;">啟用雲端 / 自訂 LLM 語意大腦</label>
            </div>

            <div id="llm-test-result" class="test-res-box"></div>

            <div class="modal-actions">
                <button type="button" class="btn-quick" id="btn-test-llm" onclick="testLlmConnection()">
                    <i class="fa-solid fa-flask"></i> 測試連線
                </button>
                <div style="display: flex; gap: 8px;">
                    <button type="button" class="btn-quick" onclick="closeLlmModal()">關閉</button>
                    <button type="button" class="btn-quick" style="background: linear-gradient(135deg, #a855f7, #7c3aed); border: none; font-weight: 600;" onclick="saveLlmConfig()">
                        <i class="fa-solid fa-check"></i> 儲存並啟用
                    </button>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""

# ======================================================
# 5. API 路由定義
# ======================================================
@app.get("/", response_class=HTMLResponse)
@app.head("/", response_class=HTMLResponse)
async def serve_dashboard():
    return HTMLResponse(content=HTML_CONTENT)

@app.get("/api/latest_image")
@app.head("/api/latest_image")
async def get_latest_image():
    global latest_image_bytes
    if latest_image_bytes:
        return Response(
            content=latest_image_bytes,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
                "Access-Control-Allow-Origin": "*"
            }
        )
    if os.path.exists(latest_img_path):
        return FileResponse(
            latest_img_path,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
                "Access-Control-Allow-Origin": "*"
            }
        )
    return Response(status_code=404)

@app.get("/api/stream_state")
async def get_stream_state():
    return {"stream_enabled": stream_enabled}

@app.post("/api/set_stream_state")
async def set_stream_state(req: StreamControlRequest):
    global stream_enabled, search_step
    stream_enabled = req.enabled
    status_text = "🟢 啟動 (全速運作)" if stream_enabled else "⏸️ 暫停 (休眠待命)"
    add_feedback(f"🎛️ 網站中控開關：相機與回傳機制切換為【{status_text}】")
    if not stream_enabled:
        stop_cmd = ESP32Command(action="brake", speed=0).to_payload()
        set_active_command(stop_cmd, desc="⏸️ 鏡頭與回傳機制已暫停 (小車待命停俥)", target_found=False, mode="PAUSED")
    else:
        search_step = 0
        generate_search_command()
    return {"status": "ok", "stream_enabled": stream_enabled}

@app.post("/api/emergency_stop")
async def emergency_stop():
    return await set_stream_state(StreamControlRequest(enabled=False))

@app.post("/api/reset_arm")
async def reset_arm():
    add_feedback("🦾 網站手動指令：下發機械手臂復位指令 (preset: home)")
    home_cmd = ESP32Command(preset="home").to_payload()
    set_active_command(home_cmd, desc="🦾 手臂執行歸位動作", target_found=False, mode="MANUAL")
    return {"status": "ok", "command": home_cmd}

@app.post("/api/set_target")
async def set_target(req: TargetRequest):
    global target_item_prompt, current_target_class, current_target_candidates, current_target_height, current_target_zh, search_step
    raw_prompt = req.target.strip()
    if not raw_prompt:
        return {"status": "error", "message": "Target cannot be empty"}
    # 透過背景執行緒非同步解算，絕不阻塞主影像串流與伺服器迴圈
    info = await asyncio.to_thread(brain.parse_task, raw_prompt)
    current_target_class = info["target_class"]
    current_target_candidates = info.get("candidates", [current_target_class])
    current_target_height = info["height_mm"]
    current_target_zh = info["target_zh"]
    target_item_prompt = f"{current_target_zh} ({current_target_class})"
    search_step = 0
    cmd = generate_search_command()
    add_feedback(f"🎯 使用者語意對齊：【{raw_prompt}】➔ 鎖定類別【{current_target_class}】(高 {current_target_height}mm, 候選: {current_target_candidates}) via {info.get('source')}")
    return {"status": "ok", "target": target_item_prompt, "target_class": current_target_class, "command": cmd}

@app.get("/api/llm_config")
async def get_llm_config():
    cfg = brain.config.copy()
    raw_key = cfg.get("api_key", "")
    if raw_key and len(raw_key) > 8:
        masked_key = raw_key[:4] + "****" + raw_key[-4:]
    elif raw_key:
        masked_key = "****"
    else:
        masked_key = ""
    return {
        "provider": cfg.get("provider", "agnes"),
        "api_url": cfg.get("api_url", "https://apihub.agnes-ai.com/v1/chat/completions"),
        "model": cfg.get("model", "agnes-2.5-flash"),
        "enabled": cfg.get("enabled", True),
        "api_key_masked": masked_key,
        "has_key": bool(raw_key)
    }

@app.post("/api/llm_config")
async def set_llm_config(req: LLMConfigRequest):
    update_data = {
        "api_url": req.api_url,
        "model": req.model,
        "enabled": req.enabled,
        "provider": req.provider
    }
    if req.api_key is not None:
        update_data["api_key"] = req.api_key
    updated = brain.update_config(update_data)
    add_feedback(f"⚙️ LLM 設定更新：[{updated.get('provider')}] 模型 {updated.get('model')} (啟用: {updated.get('enabled')})")
    return {
        "status": "ok",
        "config": {
            "provider": updated.get("provider"),
            "api_url": updated.get("api_url"),
            "model": updated.get("model"),
            "enabled": updated.get("enabled")
        }
    }

@app.post("/api/test_llm")
async def test_llm_connection(req: LLMTestRequest = LLMTestRequest()):
    res = await asyncio.to_thread(brain.test_connection, req.prompt or "手把")
    return res

# 規範核心指令端點 (相容本機讀取與外部上位機呼叫)
@app.get("/api/command")
async def get_current_command():
    return latest_command

@app.post("/api/command")
async def set_custom_command(cmd: ESP32Command):
    cmd_payload = cmd.to_payload()
    set_active_command(cmd_payload, desc="收到自訂規範控制指令", target_found=False, mode="CUSTOM")
    add_feedback(f"📡 外部自訂指令: {cmd_payload}")
    return {"status": "success", "code": 200, "command": cmd_payload}

@app.post("/api/upload")
async def upload_image(request: Request, file: Optional[UploadFile] = File(None)):
    """
    ESP32-CAM 影像上傳端點：
    接收 ESP32 相片 ➔ 極速本地 YOLO 追蹤 (5~15ms) ➔ 即時計算 PID 航向 / 煞停 IK ➔ 立即回傳 JSON 指令
    """
    global stream_enabled, last_frame_time, frame_count, latest_image_bytes, last_ai_latency, latest_all_detected, latest_detected_summary
    try:
        if not stream_enabled:
            brake_cmd = ESP32Command(action="brake", speed=0).to_payload()
            return JSONResponse(content=brake_cmd)

        contents = None
        if file is not None:
            try:
                contents = await file.read()
            except Exception:
                contents = None
        if not contents:
            contents = await request.body()
        if not contents:
            return JSONResponse(content={"error": "檔案內容為空"}, status_code=400)

        latest_image_bytes = contents
        last_frame_time = round(time.time(), 3)
        frame_count += 1

        t_infer_start = time.time()
        try:
            image = Image.open(io.BytesIO(contents))
            image.load()
            if image.mode != "RGB":
                image = image.convert("RGB")

            image.thumbnail((640, 480), Image.Resampling.BILINEAR)

            # 即時本地 YOLO 追蹤與單目測距
            track_res = tracker.detect_and_track(
                img=image,
                target_class=current_target_class,
                target_classes=current_target_candidates,
                real_height_mm=current_target_height,
                stop_dist_threshold_mm=200.0,  # 20cm 煞停
                conf_threshold=0.15,
                imgsz=480
            )

            latest_all_detected = track_res.get("all_detected", [])
            latest_detected_summary = ", ".join([f"{d['class']} ({int(d['conf']*100)}%)" for d in latest_all_detected]) if latest_all_detected else "無"

            # 繪製視覺化圖層 (Dashboard 實時顯示)
            draw = ImageDraw.Draw(image)
            draw.line([(320, 220), (320, 260)], fill=(0, 255, 255), width=2)
            draw.line([(300, 240), (340, 240)], fill=(0, 255, 255), width=2)

            target_bbox = track_res.get("bbox") if track_res.get("target_found") else None

            # 標註視野中其他可見物件 (青天藍色細框)
            for item in latest_all_detected:
                ibbox = item["bbox"]
                if target_bbox and ibbox == target_bbox:
                    continue
                draw.rectangle(ibbox, outline=(56, 189, 248), width=1)
                draw.text((ibbox[0], max(0, ibbox[1] - 12)), f"{item['class']} {int(item['conf']*100)}%", fill=(56, 189, 248))

            if track_res["target_found"]:
                bbox = track_res["bbox"]
                dist_mm = track_res["distance_mm"]
                det_cls = track_res.get("detected_class", current_target_class)
                conf_pct = int(track_res.get("conf", 0.0) * 100)
                draw.rectangle(bbox, outline=(0, 255, 0), width=3)
                draw.text((bbox[0], max(0, bbox[1] - 16)), f"🎯 {det_cls}: {round(dist_mm/10, 1)}cm ({conf_pct}%)", fill=(0, 255, 0))

                if track_res["status"] == "READY_FOR_ARM":
                    # 煞停 + 二次採樣 + IK 求解
                    is_solvable, servo_angles, comp = arm_solver.solve_ik(x=dist_mm, y=0.0, z=45.0)
                    if is_solvable:
                        cmd_payload = ESP32Command(
                            shoulder=servo_angles["shoulder"],
                            elbow=servo_angles["elbow"],
                            wrist=servo_angles["wrist"],
                            gripper=servo_angles["gripper"]
                        ).to_payload()
                        set_active_command(cmd_payload, desc="🎯 已就位！IK 解算完成，手臂執行夾取", target_found=True, mode="ARM_GRAB")
                        add_feedback(f"🦾 [已抵達夾取點] 距離 {round(dist_mm/10, 1)}cm | 執行手臂動作")
                    else:
                        comp_cmd = ESP32Command(action=comp["direction"], speed=180, duration=250).to_payload()
                        set_active_command(comp_cmd, desc=f"⚠️ IK無解：{comp['reason']} -> 觸發底盤補償", target_found=True, mode="COMPENSATING")
                        add_feedback(f"⚠️ IK無解補償: {comp['reason']} ➔ 底盤 {comp['direction']}")
                else:
                    # 閉環前進推進中 (PID 航向保持)
                    set_active_command(track_res["command"], desc=track_res["info"], target_found=True, mode="TRACKING")
                    if not latest_target_found or (frame_count % 10 == 0):
                        add_feedback(f"🎯 [鎖定目標] {det_cls} ({conf_pct}%) 距離 {round(dist_mm/10, 1)}cm | 指令: {track_res['command']}")
            else:
                # 巡航掃描
                set_active_command(track_res["command"], desc=track_res["info"], target_found=False, mode="SEARCHING")

            temp_file = os.path.join(STATIC_DIR, "latest_temp.jpg")
            final_file = os.path.join(STATIC_DIR, "latest.jpg")
            image.save(temp_file, format="JPEG", quality=85)
            os.replace(temp_file, final_file)

            # 將畫有即時辨識框、十字準心與距離的最新畫面更新至記憶體 bytes，供 /api/latest_image 立即回傳顯示
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=85)
            latest_image_bytes = buf.getvalue()

        except Exception as img_err:
            print(f"⚠️ 影像解析異常: {img_err}")

        last_ai_latency = round(time.time() - t_infer_start, 3)
        return JSONResponse(content=latest_command)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.get("/api/status")
async def get_status():
    local_ip = get_local_ip()
    return {
        "status": "ok",
        "stream_enabled": stream_enabled,
        "data": latest_command,
        "command": latest_command,
        "target_item": target_item_prompt,
        "target_description": latest_target_desc,
        "target_found": latest_target_found,
        "search_mode": latest_search_mode,
        "feedbacks": feedbacks,
        "gemini_connected": gemini_connected,
        "is_analyzing": False,
        "ai_latency_s": last_ai_latency,
        "active_model": f"YOLOv8n [{DEVICE.upper()}] + {brain.config.get('model', 'agnes-2.5-flash') if brain.config.get('enabled', True) else '離線規則庫'}",
        "ai_interval_s": 0.05,
        "local_ip": local_ip,
        "server_url": f"http://{local_ip}:8001",
        "last_frame_time": last_frame_time,
        "frame_count": frame_count,
        "all_detected": latest_all_detected,
        "detected_summary": latest_detected_summary
    }

if __name__ == "__main__":
    # pyrefly: ignore[missing-import]
    import uvicorn
    local_ip = get_local_ip()
    print("=" * 60)
    print("🚗 AIoT 尋物小車超低延遲中控中心啟動成功！")
    print(f"👉 本機電腦請開瀏覽器： http://localhost:8001")
    print(f"👉 同區網設備 (手機/ESP32)： http://{local_ip}:8001")
    print(f"📡 ESP32 serverUrl 請設為: http://{local_ip}:8001/api/upload")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=8001)