# ESP32-S3 底層控制端 - AI 上位機 HTTP JSON API 對接規範

本文件提供給負責 **AI 決策 / 視覺辨識 (Python / OpenCV / YOLO / ROS / Web)** 的團隊成員。  
ESP32-S3 作為**底層受控端 (Receiver Server)**，提供以下 HTTP RESTful JSON 接收端點。

---

## 1. 連線資訊 (Connection)

- **預設 Wi-Fi AP 熱點**：`ESP32-S3-CAR-ARM` (密碼: `12345678`)
- **預設 IP 位址**：`192.168.4.1`
- **服務端口 (Port)**：`80`
- **mDNS 網域名稱**：`http://esp32car.local`
- **跨域支援 (CORS)**：已全面啟用 (`Access-Control-Allow-Origin: *`)

---

## 2. 核心指令接收端點 (Command Receiver Endpoint)

- **URL**：`http://192.168.4.1/api/command`  *(亦相容 `/api/json`)*
- **HTTP Method**：`POST`
- **Headers**：`Content-Type: application/json`

---

## 3. AI 指令 JSON 格式規範與範例

上位機 AI 可依據需求發送以下幾種不同類型的 JSON 封包：

### 模式 A：底盤車輛運動控制
```json
{
  "action": "forward",   // 可選: "forward"(前進), "backward"(後退), "left"(左轉), "right"(右轉), "spin_left"(左旋轉), "spin_right"(右旋轉), "stop"(停止), "brake"(急煞)
  "speed": 220,          // PWM 速度: 80 ~ 255 (預設 200)
  "duration": 1000       // 可選毫秒數 (例如 1000 代表執行 1 秒後自動安全停車；不填則持續移動)
}
```

### 模式 B：機械手臂 3D/2D 空間座標直接導引 (IK 逆運動學)
當 AI 視覺辨識到目標物品在車前的位置 $(X, Z)$ 時，直接傳入毫米座標，ESP32-S3 自動反推大臂與小臂角度：
```json
{
  "x": 150.0,            // 車前水平伸展距離 (單位: 毫米 mm)
  "z": 45.0,             // 離地垂直高度 (單位: 毫米 mm)
  "wrist": 90,           // 可選: 手腕滾轉 (0~180度，90度為水平夾，0度為垂直夾)
  "gripper": 25          // 可選: 夾爪開合 (20度=夾緊, 90度=完全張開)
}
```

### 模式 C：機械手臂關節角度直接設定
```json
{
  "shoulder": 85,        // 大臂仰角: 15 ~ 165 度
  "elbow": 110,          // 小臂俯仰: 15 ~ 165 度
  "wrist": 90,           // 手腕滾轉: 0 ~ 180 度 (90度為水平)
  "gripper": 30          // 夾爪開合: 15 (緊閉) ~ 95 (全開) 度
}
```

### 模式 D：機械手臂一鍵預設姿態
```json
{
  "preset": "home"       // 可選: "home"(復位待命), "ground"(地面低位抓取), "lift"(抓起抬高收納), "open"(張開爪子), "close"(夾緊爪子)
}
```

### 模式 E：車輛 + 機械手臂同時復合控制
```json
{
  "action": "forward",
  "speed": 180,
  "shoulder": 70,
  "elbow": 100,
  "gripper": 90
}
```

---

## 4. ESP32 回傳的狀態回應 (JSON Response)

每當收到 POST 指令並執行後，ESP32-S3 會立即回傳 HTTP 200 及目前全車的最新物理狀態：

```json
{
  "status": "success",
  "code": 200,
  "car": {
    "action": "forward",
    "speed": 200,
    "moving": true
  },
  "arm": {
    "shoulder": 85,
    "elbow": 110,
    "wrist": 90,
    "gripper": 30,
    "x": 151.3,
    "z": 45.0,
    "ikSuccess": true
  },
  "timestamp": 128452
}
```

---

## 5. Python 上位機 AI 調用範例程式碼

AI 團隊成員可以直接在 Python 專案中貼上這段代碼進行測試：

```python
import requests
import json
import time

ESP32_URL = "http://192.168.4.1/api/command"

def send_command(payload):
    headers = {"Content-Type": "application/json"}
    try:
        response = requests.post(ESP32_URL, json=payload, timeout=2.0)
        if response.status_code == 200:
            print("✅ 執行成功:", response.json())
        else:
            print("❌ 執行失敗:", response.text)
    except Exception as e:
        print("⚠️ 無法連線至小車:", e)

# 範例 1: 小車向前行駛 1 秒後自動停下
send_command({
    "action": "forward",
    "speed": 200,
    "duration": 1000
})

time.sleep(1.5)

# 範例 2: 手臂張開爪子，並伸到 X=140mm, Z=50mm 處夾取物品
send_command({
    "x": 140.0,
    "z": 50.0,
    "wrist": 90,
    "gripper": 90  # 張開爪子
})

time.sleep(1.0)

# 範例 3: 爪子夾緊，抬高收納
send_command({
    "preset": "close"
})
time.sleep(0.5)
send_command({
    "preset": "lift"
})
```
