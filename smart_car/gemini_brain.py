# gemini_brain.py
# 雲端 Gemini 自然語言語意大腦：將口語化任務轉化為標準 COCO 目標類別與幾何先驗資訊
import os
import json
import re
from typing import Dict, Any, Optional

# 常用目標物之物理尺寸先驗資料庫 (含豐富中文關鍵字同義詞匹配與 COCO 候選類別)
OBJECT_PRIORS = {
    "remote": {
        "keywords": ["手把", "遊戲手把", "手柄", "遙控器", "遙控", "手掣", "搖桿", "遙控手柄", "控制器", "remote", "gamepad", "controller", "joystick"],
        "candidates": ["remote", "cell phone", "mouse"],
        "name_zh": "遊戲手把/遙控器",
        "height_mm": 135.0,
        "width_mm": 95.0
    },
    "keyboard": {
        "keywords": ["鍵盤", "電腦鍵盤", "機械鍵盤", "keyboard"],
        "candidates": ["keyboard", "laptop"],
        "name_zh": "鍵盤",
        "height_mm": 140.0,
        "width_mm": 440.0
    },
    "laptop": {
        "keywords": ["筆電", "筆記型電腦", "手提電腦", "筆記本電腦", "電腦", "主機", "laptop", "notebook"],
        "candidates": ["laptop", "tv", "keyboard"],
        "name_zh": "筆記型電腦",
        "height_mm": 220.0,
        "width_mm": 320.0
    },
    "mouse": {
        "keywords": ["滑鼠", "電腦滑鼠", "mouse"],
        "candidates": ["mouse", "remote"],
        "name_zh": "滑鼠",
        "height_mm": 40.0,
        "width_mm": 65.0
    },
    "cup": {
        "keywords": ["杯", "杯子", "水杯", "馬克杯", "茶杯", "紙杯", "咖啡杯", "隨行杯", "cup", "mug"],
        "candidates": ["cup", "bottle", "bowl"],
        "name_zh": "杯子/水杯",
        "height_mm": 110.0,
        "width_mm": 80.0
    },
    "bottle": {
        "keywords": ["寶特瓶", "飲料", "水壺", "保溫瓶", "可樂", "雪碧", "瓶", "瓶子", "罐", "罐子", "酒瓶", "礦泉水", "bottle"],
        "candidates": ["bottle", "cup"],
        "name_zh": "寶特瓶/飲料罐",
        "height_mm": 210.0,
        "width_mm": 65.0
    },
    "cell phone": {
        "keywords": ["手機", "行動電話", "智慧型手機", "智慧手機", "智能手機", "電話", "phone", "iphone", "cell phone"],
        "candidates": ["cell phone", "remote"],
        "name_zh": "手機",
        "height_mm": 150.0,
        "width_mm": 75.0
    },
    "tv": {
        "keywords": ["電視", "螢幕", "顯示器", "液晶螢幕", "液晶電視", "tv", "monitor"],
        "candidates": ["tv", "laptop"],
        "name_zh": "電視/螢幕",
        "height_mm": 350.0,
        "width_mm": 550.0
    },
    "sports ball": {
        "keywords": ["球", "網球", "棒球", "籃球", "足球", "桌球", "排球", "ball", "sports ball"],
        "candidates": ["sports ball", "apple", "orange"],
        "name_zh": "球類/網球",
        "height_mm": 68.0,
        "width_mm": 68.0
    },
    "scissors": {
        "keywords": ["剪刀", "剪子", "scissors"],
        "candidates": ["scissors"],
        "name_zh": "剪刀",
        "height_mm": 160.0,
        "width_mm": 70.0
    },
    "clock": {
        "keywords": ["時鐘", "鬧鐘", "手錶", "鐘", "clock", "watch"],
        "candidates": ["clock"],
        "name_zh": "時鐘/鬧鐘",
        "height_mm": 120.0,
        "width_mm": 120.0
    },
    "book": {
        "keywords": ["書", "書籍", "筆記本", "書本", "課本", "小說", "雜誌", "漫畫", "本子", "book"],
        "candidates": ["book"],
        "name_zh": "書籍",
        "height_mm": 210.0,
        "width_mm": 148.0
    },
    "box": {
        "keywords": ["箱", "紙箱", "盒子", "方盒", "箱子", "行李箱", "旅行箱", "box", "suitcase", "package"],
        "candidates": ["suitcase"],
        "name_zh": "紙箱/方盒",
        "height_mm": 180.0,
        "width_mm": 180.0
    },
    "backpack": {
        "keywords": ["背包", "後背包", "書包", "包包", "雙肩包", "backpack", "bag"],
        "candidates": ["backpack", "handbag", "suitcase"],
        "name_zh": "背包/書包",
        "height_mm": 400.0,
        "width_mm": 300.0
    },
    "handbag": {
        "keywords": ["手提包", "提包", "皮包", "女包", "公事包", "handbag", "purse"],
        "candidates": ["handbag", "backpack"],
        "name_zh": "手提包/皮包",
        "height_mm": 250.0,
        "width_mm": 300.0
    },
    "umbrella": {
        "keywords": ["雨傘", "傘", "陽傘", "umbrella"],
        "candidates": ["umbrella"],
        "name_zh": "雨傘",
        "height_mm": 500.0,
        "width_mm": 80.0
    },
    "toothbrush": {
        "keywords": ["牙刷", "電動牙刷", "toothbrush"],
        "candidates": ["toothbrush"],
        "name_zh": "牙刷",
        "height_mm": 180.0,
        "width_mm": 15.0
    },
    "teddy bear": {
        "keywords": ["玩偶", "娃娃", "布偶", "泰迪熊", "熊娃娃", "公仔", "teddy bear", "doll"],
        "candidates": ["teddy bear"],
        "name_zh": "玩偶/娃娃",
        "height_mm": 200.0,
        "width_mm": 150.0
    },
    "chair": {
        "keywords": ["椅子", "板凳", "辦公椅", "座椅", "沙發", "chair", "couch", "sofa"],
        "candidates": ["chair", "couch"],
        "name_zh": "椅子/沙發",
        "height_mm": 600.0,
        "width_mm": 450.0
    },
    "banana": {
        "keywords": ["香蕉", "banana"],
        "candidates": ["banana"],
        "name_zh": "香蕉",
        "height_mm": 180.0,
        "width_mm": 40.0
    },
    "apple": {
        "keywords": ["蘋果", "apple"],
        "candidates": ["apple", "orange"],
        "name_zh": "蘋果",
        "height_mm": 80.0,
        "width_mm": 80.0
    },
    "orange": {
        "keywords": ["橘子", "柳丁", "柳橙", "柑橘", "orange"],
        "candidates": ["orange", "apple"],
        "name_zh": "橘子",
        "height_mm": 75.0,
        "width_mm": 75.0
    },
    "bowl": {
        "keywords": ["碗", "湯碗", "飯碗", "碗公", "bowl"],
        "candidates": ["bowl", "cup"],
        "name_zh": "碗",
        "height_mm": 70.0,
        "width_mm": 120.0
    },
    "fork": {
        "keywords": ["叉子", "餐叉", "fork"],
        "candidates": ["fork", "knife", "spoon"],
        "name_zh": "叉子",
        "height_mm": 180.0,
        "width_mm": 25.0
    },
    "knife": {
        "keywords": ["刀子", "餐刀", "刀", "水果刀", "knife"],
        "candidates": ["knife", "fork"],
        "name_zh": "刀子",
        "height_mm": 200.0,
        "width_mm": 25.0
    },
    "spoon": {
        "keywords": ["湯匙", "勺子", "匙", "spoon"],
        "candidates": ["spoon", "fork"],
        "name_zh": "湯匙",
        "height_mm": 160.0,
        "width_mm": 35.0
    },
    "cat": {
        "keywords": ["貓", "小貓", "貓咪", "喵", "cat", "kitten"],
        "candidates": ["cat", "dog"],
        "name_zh": "貓咪",
        "height_mm": 250.0,
        "width_mm": 150.0
    },
    "dog": {
        "keywords": ["狗", "小狗", "狗狗", "汪", "dog", "puppy"],
        "candidates": ["dog", "cat"],
        "name_zh": "小狗",
        "height_mm": 350.0,
        "width_mm": 200.0
    },
    "car": {
        "keywords": ["汽車", "轎車", "小車", "車子", "車輛", "車模", "模型車", "car"],
        "candidates": ["car", "truck"],
        "name_zh": "汽車",
        "height_mm": 150.0,
        "width_mm": 300.0
    },
    "bicycle": {
        "keywords": ["腳踏車", "自行車", "單車", "bicycle", "bike"],
        "candidates": ["bicycle", "motorcycle"],
        "name_zh": "自行車",
        "height_mm": 300.0,
        "width_mm": 400.0
    }
}

COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush"
]

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_config.json")

class GeminiSemanticBrain:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.mode = None
        self.client = None
        self.config: Dict[str, Any] = {}
        self.load_config()

        # 嘗試新版 google.genai
        try:
            from google import genai
            self.client = genai.Client(api_key=self.api_key)
            self.mode = "genai"
        except Exception:
            # 降級嘗試標準 google.generativeai
            try:
                import google.generativeai as gai
                gai.configure(api_key=self.api_key)
                self.client = gai.GenerativeModel("models/gemini-2.5-flash")
                self.mode = "generativeai"
            except Exception as e:
                print(f"[WARN] Gemini SDK failed to load: {e}")

    def load_config(self) -> Dict[str, Any]:
        default_cfg = {
            "provider": "openai_compatible",
            "api_url": "https://apihub.agnes-ai.com/v1/chat/completions",
            "api_key": os.environ.get("AGNES_API_KEY", os.environ.get("OPENAI_API_KEY", "")),
            "model": "agnes-2.5-flash",
            "enabled": True
        }
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    self.config = json.load(f)
                    # 確保必要欄位存在
                    for k, v in default_cfg.items():
                        if k not in self.config:
                            self.config[k] = v
                    return self.config
            except Exception as e:
                print(f"[WARN] 讀取 llm_config.json 失敗: {e}")
        self.config = default_cfg
        self.save_config()
        return self.config

    def save_config(self) -> bool:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"[WARN] 儲存 llm_config.json 失敗: {e}")
            return False

    def update_config(self, new_cfg: Dict[str, Any]) -> Dict[str, Any]:
        """更新自訂 LLM 設定 (支援 Agnes 2.5 Flash / DeepSeek / OpenAI / 本機 Ollama)"""
        if "api_url" in new_cfg:
            url = str(new_cfg["api_url"]).strip()
            # 若使用者輸入了 Base URL (如 https://apihub.agnes-ai.com/v1)，自動補齊端點
            if url and not url.endswith("/chat/completions"):
                url = url.rstrip("/") + "/chat/completions"
            self.config["api_url"] = url
        if "api_key" in new_cfg:
            # 若為遮蔽字元，保持原 key
            key = str(new_cfg["api_key"]).strip()
            if not ("****" in key):
                self.config["api_key"] = key
        if "model" in new_cfg and new_cfg["model"]:
            self.config["model"] = str(new_cfg["model"]).strip()
        if "enabled" in new_cfg:
            self.config["enabled"] = bool(new_cfg["enabled"])
        if "provider" in new_cfg:
            self.config["provider"] = str(new_cfg["provider"])

        self.save_config()
        return self.config

    def call_openai_compatible(self, user_command: str) -> Optional[Dict[str, Any]]:
        """
        透過標準 OpenAI 相容格式調用第三方 API (例如 Agnes 2.5 Flash, DeepSeek, GPT-4o-mini, Ollama)
        """
        api_url = self.config.get("api_url", "https://apihub.agnes-ai.com/v1/chat/completions")
        api_key = self.config.get("api_key", "").strip()
        model_name = self.config.get("model", "agnes-2.5-flash")

        if not api_url or not api_key:
            return None

        import httpx

        system_instruction = f"""你是一個 AIoT 智能機械臂小車的語意理解與目標匹配大腦。
小車配備 YOLO 視覺偵測模型，支援的 80 種標準 COCO 標籤清單如下：
{COCO_CLASSES}

任務：分析使用者的口語指令，挑選出最相符的一個 COCO 英文標籤與物理尺寸。
若目標在列表中無完全一致單字，請挑選在功能或形態上最接近的替代標籤（例如：手把/搖桿/遊戲控制器 ➔ remote；保溫杯 ➔ bottle 或 cup；筆電 ➔ laptop；玩偶 ➔ teddy bear）。

請嚴格僅輸出標準純 JSON 物件（切勿輸出 Markdown 標籤或任何解釋文字）：
{{
  "target_class": "最相符的COCO英文類別名稱",
  "candidates": ["主標籤", "備用相容標籤1"],
  "target_zh": "繁體中文物品名稱",
  "height_mm": 該物體標準高度毫米(浮點數),
  "width_mm": 該物體標準寬度毫米(浮點數)
}}"""

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": f"使用者任務指令：「{user_command}」"}
            ],
            "temperature": 0.2,
            "max_tokens": 512
        }

        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.post(api_url, json=payload, headers=headers)
                if res.status_code == 200:
                    resp_data = res.json()
                    choices = resp_data.get("choices", [])
                    if choices:
                        raw_text = choices[0].get("message", {}).get("content", "").strip()
                        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.MULTILINE)
                        raw_text = re.sub(r"\s*```$", "", raw_text, flags=re.MULTILINE)
                        parsed = json.loads(raw_text)

                        target_cls = str(parsed.get("target_class", "")).lower().strip()
                        candidates = parsed.get("candidates", [target_cls])
                        if not isinstance(candidates, list):
                            candidates = [target_cls]

                        return {
                            "success": True,
                            "target_class": target_cls,
                            "candidates": candidates,
                            "target_zh": parsed.get("target_zh", user_command),
                            "height_mm": float(parsed.get("height_mm", 150.0)),
                            "width_mm": float(parsed.get("width_mm", 80.0)),
                            "source": f"openai_api ({model_name})"
                        }
                else:
                    print(f"[WARN] OpenAI 相容 API 呼叫異常 ({res.status_code}): {res.text[:200]}")
        except Exception as e:
            print(f"[WARN] OpenAI 相容 API 網路例外: {e}")
        return None

    def test_connection(self, test_prompt: str = "手把") -> Dict[str, Any]:
        """測試當前配置的 OpenAI 相容 API (如 Agnes 2.5 Flash) 連線狀態"""
        import time
        t0 = time.time()
        api_url = self.config.get("api_url", "")
        api_key = self.config.get("api_key", "").strip()
        model_name = self.config.get("model", "agnes-2.5-flash")

        if not api_key:
            return {"success": False, "error": "尚未設定 API Key，請先填入金鑰！", "model": model_name}

        import httpx
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model_name,
            "messages": [
                {"role": "user", "content": f"Ping test. 請以 JSON 回傳 {{\"status\": \"ok\", \"test\": \"{test_prompt}\"}}"}
            ],
            "max_tokens": 64
        }
        try:
            with httpx.Client(timeout=8.0) as client:
                res = client.post(api_url, json=payload, headers=headers)
                latency = round(time.time() - t0, 2)
                if res.status_code == 200:
                    data = res.json()
                    msg = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    return {
                        "success": True,
                        "latency_s": latency,
                        "model": model_name,
                        "response_preview": msg[:120],
                        "api_url": api_url
                    }
                else:
                    return {
                        "success": False,
                        "latency_s": latency,
                        "status_code": res.status_code,
                        "error": f"HTTP {res.status_code}: {res.text[:180]}",
                        "model": model_name
                    }
        except Exception as e:
            latency = round(time.time() - t0, 2)
            return {"success": False, "latency_s": latency, "error": str(e), "model": model_name}

    def parse_task(self, user_command: str) -> Dict[str, Any]:
        """
        將使用者的自然語言任務解析為 YOLO 標籤與物理尺寸資訊 (微秒級離線優先，次選 Agnes/OpenAI 相容 API)
        """
        cmd_lower = user_command.lower().strip()

        # 1. 快速離線同義詞匹配 (0.1ms 秒回，絕不卡頓、不怕斷網或 API 限流)
        for class_name, info in OBJECT_PRIORS.items():
            if any(kw in cmd_lower for kw in info["keywords"]):
                return {
                    "success": True,
                    "target_class": class_name,
                    "candidates": info.get("candidates", [class_name]),
                    "target_zh": info["name_zh"].split("/")[0],
                    "height_mm": info["height_mm"],
                    "width_mm": info["width_mm"],
                    "source": "offline_instant_rule"
                }

        # 2. 直接英文 COCO 類別精準匹配 (例如使用者輸入 keyboard, remote, cup...)
        if cmd_lower in COCO_CLASSES:
            return {
                "success": True,
                "target_class": cmd_lower,
                "candidates": [cmd_lower],
                "target_zh": cmd_lower,
                "height_mm": 150.0,
                "width_mm": 100.0,
                "source": "coco_direct_match"
            }

        # 3. 離線未命中時，優先調用使用者設定之 OpenAI 相容 API (例如 Agnes 2.5 Flash, DeepSeek...)
        if self.config.get("enabled", True) and self.config.get("api_key", "").strip():
            api_res = self.call_openai_compatible(user_command)
            if api_res is not None and api_res.get("success"):
                return api_res

        # 4. 智慧容錯：若雲端未啟用或連線失敗，在 COCO 80 類別中尋找部分匹配，絕不盲目篡改為「寶特瓶」
        matched_coco = None
        for c in COCO_CLASSES:
            if c in cmd_lower or cmd_lower in c:
                matched_coco = c
                break

        assigned_class = matched_coco or cmd_lower
        return {
            "success": True,
            "target_class": assigned_class,
            "candidates": [assigned_class],
            "target_zh": user_command,
            "height_mm": 150.0,
            "width_mm": 80.0,
            "source": "custom_user_target"
        }

if __name__ == "__main__":
    brain = GeminiSemanticBrain()
    print("當前配置:", brain.config)
    for test_kw in ["杯子", "手把", "遊戲手把", "筆電", "鍵盤", "滑鼠", "幫我拿桌上的Switch搖桿"]:
        res = brain.parse_task(test_kw)
        print(f"「{test_kw}」 -> {res['target_class']} ({res['target_zh']}) via {res['source']}")

