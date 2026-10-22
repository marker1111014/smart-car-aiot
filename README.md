# AIoT 智能尋物小車與機械手臂系統 (AIoT Smart Retrieval Car)

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100%2B-green.svg)](https://fastapi.tiangolo.com/)
[![YOLOv8](https://img.shields.io/badge/YOLOv8-Ultralytics-orange.svg)](https://ultralytics.com/)
[![ESP32-S3](https://img.shields.io/badge/Hardware-ESP32--S3--CAM-red.svg)](https://www.espressif.com/)

融合多模態大型語言模型（LLM）語意理解、邊緣運算視覺伺服（Visual Servoing）與逆運動學（Inverse Kinematics, IK）求解之物聯網自主尋物機械臂小車系統。

---

## 🌟 核心特色

1. **多模態 LLM 語意大腦**：
   - 支援 **Agnes 2.5 Flash**、**DeepSeek**、**OpenAI** 等 OpenAI 相容 API 格式，亦支援本機完全離線的 **Ollama**。
   - 具備**離線規則優先機制**（0.1ms 極速響應，常見物品免呼叫 API、零 Token 消耗）。
   - 支援口語化輸入（如「幫我拿 Switch 手把」、「找剪刀」），自動推導 COCO 80 類別標籤與三維物理幾何先驗。

2. **超低延遲視覺伺服追蹤 (Visual Servoing)**：
   - 整合本地 YOLOv8 目標偵測模型（單幀推論 15~30ms）。
   - 具備動態 PID 航向角修正，引導小車自主對準目標物並閉環推進。

3. **3-DOF 機械手臂逆運動學求解 (IK Solver)**：
   - 即時求解基座旋轉、肩關節、肘關節與爪部舵機角度。
   - 具備到達性邊界檢查與底盤位置自動補償機制。

4. **現代化即時 Web 中控儀表板**：
   - 深色玻璃擬態（Glassmorphism）介面。
   - 即時鏡頭畫面串流、即時姿態與決策日誌、相機遠端休眠省電開關、自訂 LLM 設定彈窗。

---

## 📂 專案目錄結構

```text
project/
├── smart_car/
│   ├── app.py                     # FastAPI 上位機核心伺服器 & Web Dashboard
│   ├── main.py                    # 快速啟動入口腳本
│   ├── gemini_brain.py            # LLM 語意大腦模組 (Agnes / OpenAI 相容)
│   ├── vision_tracker.py          # YOLOv8 視覺伺服閉環追蹤器
│   ├── arm_kinematics.py          # 機械手臂 3-DOF 逆運動學求解器
│   ├── schema.py                  # Pydantic 資料規範定義
│   ├── test_pipeline.py           # 整合測試管線腳本
│   ├── requirements.txt           # Python 依賴套件清單
│   ├── llm_config.example.json    # LLM 大腦設定範本
│   ├── esp32_code/
│   │   └── esp32_code.ino         # ESP32-S3-CAM 韌體原始碼
│   └── templates/
│       └── index.html             # 獨立網頁中控介面
└── .gitignore                     # Git 忽略檔案清單
```

---

## 🚀 快速開始

### 1. Python 上位機安裝與啟動

```bash
cd smart_car

# 安裝所需套件
pip install -r requirements.txt

# 複製設定範本 (或直接在網頁後台填寫)
cp llm_config.example.json llm_config.json

# 啟動上位機中控系統
python main.py
```
啟動後打開瀏覽器訪問： `http://localhost:8001`

### 2. ESP32-S3-CAM 韌體設定與燒錄

1. 使用 **Arduino IDE** 開啟 `smart_car/esp32_code/esp32_code.ino`。
2. 修改檔案開頭的 WiFi 帳號與密碼，以及上位機伺服器 IP：
   ```cpp
   const char *ssid = "您的_WIFI_名稱";
   const char *password = "您的_WIFI_密碼";
   const char *serverUrl = "http://您的電腦IP:8001/api/upload";
   ```
3. 選擇開發板 `ESP32S3 Dev Module`，上傳燒錄。

---

## 🔒 安全注意事項

- 本專案已內建 `.gitignore` 隔離敏感資訊，請勿將包含真實 API Key 的 `llm_config.json` 或含有個人 WiFi 密碼的程式碼提交至公開倉庫。
- 建議使用環境變數 `AGNES_API_KEY` 或 `OPENAI_API_KEY` 進行金鑰管理。
