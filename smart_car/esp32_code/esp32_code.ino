#include "esp_camera.h"
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>

// ======================================================
// 1. WiFi 與後端中控伺服器設定
// ======================================================
// ⚠️ 請填入您的 2.4GHz WiFi 帳號與密碼
const char *ssid = "YOUR_WIFI_SSID";
const char *password = "YOUR_WIFI_PASSWORD";

// ⚠️ 請填入電腦在區網中的 IP 與埠號 (8001)，例如：http://192.168.1.100:8001/api/upload
const char *serverUrl = "http://YOUR_SERVER_IP:8001/api/upload";

// 🎛️ 鏡頭拍照與回傳運行狀態旗標 (由網站中控即時遠端切換)
bool isStreamActive = true;

// ======================================================
// 2. ESP32-S3-CAM 通用相機腳位配置 (OV2640)
// ======================================================
#define PWDN_GPIO_NUM -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 15 // 標準 S3-CAM 為 15
#define SIOD_GPIO_NUM 4  // I2C Data
#define SIOC_GPIO_NUM 5  // I2C Clock

#define Y9_GPIO_NUM 16
#define Y8_GPIO_NUM 17
#define Y7_GPIO_NUM 18
#define Y6_GPIO_NUM 12
#define Y5_GPIO_NUM 10
#define Y4_GPIO_NUM 8
#define Y3_GPIO_NUM 9
#define Y2_GPIO_NUM 11
#define VSYNC_GPIO_NUM 6
#define HREF_GPIO_NUM 7
#define PCLK_GPIO_NUM 13

// ======================================================
// 3. 機械臂 / 舵機 PWM 腳位設定 (已完全避開相機引腳)
// ======================================================
#define SERVO_BASE_PIN 1  // 底座旋轉舵機
#define SERVO_ARM1_PIN 2  // 大臂關節舵機
#define SERVO_ARM2_PIN 42 // 小臂關節舵機
#define SERVO_GRIP_PIN 41 // 夾爪開合舵機

#define PWM_FREQ 50       // 舵機標準 50Hz
#define PWM_RESOLUTION 16 // 16 位元解析度

// ======================================================
// 4. 小車底盤馬達驅動腳位 (支援 L298N / TB6612 / MX1508)
// ======================================================
#define MOTOR_IN1_PIN 47 // 左輪正轉
#define MOTOR_IN2_PIN 48 // 左輪反轉
#define MOTOR_IN3_PIN 21 // 右輪正轉
#define MOTOR_IN4_PIN 14 // 右輪反轉

// ======================================================
// 5. 舵機控制函數 (相容 Arduino ESP32 Core 2.x 與 3.x)
// ======================================================
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
void initServoPin(uint8_t pin) { ledcAttach(pin, PWM_FREQ, PWM_RESOLUTION); }
void writeServoAngle(uint8_t pin, int angle) {
  angle = constrain(angle, 0, 180);
  uint32_t duty =
      (uint32_t)(((angle / 180.0) * 2000.0 + 500.0) / 20000.0 * 65535.0);
  ledcWrite(pin, duty);
}
#else
int getServoChannel(uint8_t pin) {
  if (pin == SERVO_BASE_PIN)
    return 2;
  if (pin == SERVO_ARM1_PIN)
    return 3;
  if (pin == SERVO_ARM2_PIN)
    return 4;
  if (pin == SERVO_GRIP_PIN)
    return 5;
  return 2;
}
void initServoPin(uint8_t pin) {
  int ch = getServoChannel(pin);
  ledcSetup(ch, PWM_FREQ, PWM_RESOLUTION);
  ledcAttachPin(pin, ch);
}
void writeServoAngle(uint8_t pin, int angle) {
  angle = constrain(angle, 0, 180);
  uint32_t duty =
      (uint32_t)(((angle / 180.0) * 2000.0 + 500.0) / 20000.0 * 65535.0);
  int ch = getServoChannel(pin);
  ledcWrite(ch, duty);
}
#endif

void armReadySequence() {
  writeServoAngle(SERVO_BASE_PIN, 90);
  writeServoAngle(SERVO_ARM1_PIN, 90);
  writeServoAngle(SERVO_ARM2_PIN, 90);
  writeServoAngle(SERVO_GRIP_PIN, 90);
}

void armGrabSequence() {
  Serial.println("🦾 [手臂] 開始執行夾取目標物序列...");
  writeServoAngle(SERVO_GRIP_PIN, 45);
  delay(350);
  writeServoAngle(SERVO_ARM1_PIN, 50);
  writeServoAngle(SERVO_ARM2_PIN, 120);
  delay(450);
  writeServoAngle(SERVO_GRIP_PIN, 140);
  delay(400);
  writeServoAngle(SERVO_ARM1_PIN, 90);
  writeServoAngle(SERVO_ARM2_PIN, 80);
  delay(400);
  Serial.println("✅ [手臂] 夾取完成！");
}

void armReleaseSequence() {
  Serial.println("🦾 [手臂] 放開物品...");
  writeServoAngle(SERVO_GRIP_PIN, 45);
  delay(350);
  writeServoAngle(SERVO_GRIP_PIN, 90);
}

// ======================================================
// 6. 小車底盤控制函數
// ======================================================
void initMotors() {
  pinMode(MOTOR_IN1_PIN, OUTPUT);
  pinMode(MOTOR_IN2_PIN, OUTPUT);
  pinMode(MOTOR_IN3_PIN, OUTPUT);
  pinMode(MOTOR_IN4_PIN, OUTPUT);
  carStop();
}

void carStop() {
  digitalWrite(MOTOR_IN1_PIN, LOW);
  digitalWrite(MOTOR_IN2_PIN, LOW);
  digitalWrite(MOTOR_IN3_PIN, LOW);
  digitalWrite(MOTOR_IN4_PIN, LOW);
}

void carMove(const String &dir, int value) {
  int durationMs = 250;
  if (dir == "forward") {
    durationMs = (value > 0) ? value * 25 : 300;
    digitalWrite(MOTOR_IN1_PIN, HIGH);
    digitalWrite(MOTOR_IN2_PIN, LOW);
    digitalWrite(MOTOR_IN3_PIN, HIGH);
    digitalWrite(MOTOR_IN4_PIN, LOW);
  } else if (dir == "backward") {
    durationMs = (value > 0) ? value * 25 : 300;
    digitalWrite(MOTOR_IN1_PIN, LOW);
    digitalWrite(MOTOR_IN2_PIN, HIGH);
    digitalWrite(MOTOR_IN3_PIN, LOW);
    digitalWrite(MOTOR_IN4_PIN, HIGH);
  } else if (dir == "left") {
    durationMs = (value > 0) ? value * 8 : 280;
    digitalWrite(MOTOR_IN1_PIN, LOW);
    digitalWrite(MOTOR_IN2_PIN, HIGH);
    digitalWrite(MOTOR_IN3_PIN, HIGH);
    digitalWrite(MOTOR_IN4_PIN, LOW);
  } else if (dir == "right") {
    durationMs = (value > 0) ? value * 8 : 280;
    digitalWrite(MOTOR_IN1_PIN, HIGH);
    digitalWrite(MOTOR_IN2_PIN, LOW);
    digitalWrite(MOTOR_IN3_PIN, LOW);
    digitalWrite(MOTOR_IN4_PIN, HIGH);
  } else {
    carStop();
    return;
  }
  delay(durationMs);
  carStop();
}

// ======================================================
// 7. 指令解析與執行函數 (符合 ESP32-S3 規範模式 A~E)
// ======================================================
void executeCommands(const String &jsonStr) {
#if ARDUINOJSON_VERSION_MAJOR >= 7
  JsonDocument doc;
#else
  DynamicJsonDocument doc(2048);
#endif

  DeserializationError err = deserializeJson(doc, jsonStr);
  if (err) {
    Serial.printf("❌ JSON 解析失敗: %s\n", err.c_str());
    return;
  }

  // 1. 檢查規範模式 A / E 底盤動作 ("action")
  if (doc.containsKey("action")) {
    const char *action = doc["action"] | "stop";
    int speed = doc["speed"] | 200;
    int duration = doc["duration"] | 0;

    Serial.printf("🚗 [規範模式 A/E] 底盤指令: action=%s, speed=%d, duration=%d ms\n", action, speed, duration);
    if (strcmp(action, "forward") == 0) {
      carMove("forward", duration > 0 ? duration / 25 : 15);
    } else if (strcmp(action, "backward") == 0) {
      carMove("backward", duration > 0 ? duration / 25 : 15);
    } else if (strcmp(action, "left") == 0 || strcmp(action, "spin_left") == 0) {
      carMove("left", duration > 0 ? duration / 8 : 35);
    } else if (strcmp(action, "right") == 0 || strcmp(action, "spin_right") == 0) {
      carMove("right", duration > 0 ? duration / 8 : 35);
    } else if (strcmp(action, "stop") == 0 || strcmp(action, "brake") == 0) {
      carStop();
    }
  }

  // 2. 檢查規範模式 D 機械手臂預設姿態 ("preset")
  if (doc.containsKey("preset")) {
    const char *preset = doc["preset"] | "";
    Serial.printf("🦾 [規範模式 D] 手臂預設姿態: %s\n", preset);
    if (strcmp(preset, "home") == 0) {
      armReadySequence();
    } else if (strcmp(preset, "ground") == 0) {
      armGrabSequence();
    } else if (strcmp(preset, "lift") == 0) {
      writeServoAngle(SERVO_ARM1_PIN, 90);
      writeServoAngle(SERVO_ARM2_PIN, 80);
    } else if (strcmp(preset, "open") == 0) {
      armReleaseSequence();
    } else if (strcmp(preset, "close") == 0) {
      writeServoAngle(SERVO_GRIP_PIN, 140);
    }
  }

  // 3. 檢查規範模式 C / E 機械手臂關節角度直接設定
  if (doc.containsKey("shoulder") || doc.containsKey("elbow") || doc.containsKey("gripper")) {
    if (doc.containsKey("shoulder")) writeServoAngle(SERVO_ARM1_PIN, doc["shoulder"]);
    if (doc.containsKey("elbow")) writeServoAngle(SERVO_ARM2_PIN, doc["elbow"]);
    if (doc.containsKey("wrist")) writeServoAngle(SERVO_BASE_PIN, doc["wrist"]);
    if (doc.containsKey("gripper")) writeServoAngle(SERVO_GRIP_PIN, doc["gripper"]);
    Serial.println("🦾 [規範模式 C/E] 手臂舵機角度已設定");
  }

  // 4. 向下相容舊版 sequence 序列 (若有提供)
  if (doc.containsKey("sequence")) {
    JsonArray seq = doc["sequence"];
    for (JsonObject cmd : seq) {
      const char *action = cmd["action"] | "";
      const char *direction = cmd["direction"] | "";
      int value = cmd["value"] | 0;
      const char *armPos = cmd["arm_position"] | "";

      if (strcmp(action, "move") == 0) {
        if (strcmp(direction, "stop") == 0) {
          carStop();
        } else {
          carMove(String(direction), value);
        }
      } else if (strcmp(action, "arm") == 0) {
        if (strcmp(armPos, "grab") == 0) {
          armGrabSequence();
        } else if (strcmp(armPos, "release") == 0) {
          armReleaseSequence();
        } else if (strcmp(armPos, "ready") == 0) {
          armReadySequence();
        }
      }
    }
  }
}

// ======================================================
// 8. OV2640 相機初始化
// ======================================================
bool initCamera() {
  camera_config_t config;
  memset(&config, 0, sizeof(config));

  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;

  config.xclk_freq_hz = 20000000; // S3 官方標準時脈 20MHz
  config.pixel_format = PIXFORMAT_JPEG;

  if (psramFound()) {
    Serial.printf("✅ 成功啟用 PSRAM！總容量: %d KB (可用: %d KB)\n", 
                  ESP.getPsramSize() / 1024, ESP.getFreePsram() / 1024);
    config.frame_size = FRAMESIZE_VGA; // 640x480
    config.jpeg_quality = 14;          // 兼顧畫質與緩衝穩定
    config.fb_count = 2;               // 雙緩衝
    config.fb_location = CAMERA_FB_IN_PSRAM;
    config.grab_mode = CAMERA_GRAB_WHEN_EMPTY; // 最穩定模式，杜絕 DMA 超時
  } else {
    Serial.println("⚠️【嚴重警示】未檢測到 PSRAM！");
    Serial.println("👉 請至 Arduino IDE 工具列 ->「PSRAM」改選為「OPI PSRAM」！");
    Serial.println("👉 暫時降級為低記憶體模式運行 (避免拍照記憶體溢位)...");
    config.frame_size = FRAMESIZE_QVGA; // 320x240
    config.jpeg_quality = 22;          // 縮小體積
    config.fb_count = 1;
    config.fb_location = CAMERA_FB_IN_DRAM;
    config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("❌ 相機初始化失敗，代碼: 0x%x\n", err);
    return false;
  }

  sensor_t *s = esp_camera_sensor_get();
  if (s != NULL) {
    Serial.println("📷 成功初始化相機感光元件！");
    s->set_vflip(s, 1);
    s->set_hmirror(s, 1);
  }

  // 預熱相機並清空初期不穩定之 DMA 緩衝幀
  for (int i = 0; i < 2; i++) {
    camera_fb_t *dummy_fb = esp_camera_fb_get();
    if (dummy_fb) {
      esp_camera_fb_return(dummy_fb);
      delay(50);
    }
  }

  return true;
}

void printWifiStatus(wl_status_t status) {
  switch (status) {
  case WL_NO_SHIELD:
    Serial.println("❌ 找不到 WiFi 模組");
    break;
  case WL_IDLE_STATUS:
    Serial.println("⏳ WiFi 閒置中...");
    break;
  case WL_NO_SSID_AVAIL:
    Serial.println("❌ 找不到此 WiFi 名稱！(請確認為 2.4GHz)");
    break;
  case WL_CONNECTED:
    Serial.println("✅ WiFi 已成功連線！");
    break;
  case WL_CONNECT_FAILED:
    Serial.println("❌ WiFi 連線失敗！(密碼可能輸入錯誤)");
    break;
  default:
    Serial.printf("⚠️ WiFi 狀態碼: %d\n", status);
    break;
  }
}

// ======================================================
// 9. 主程式 Setup & Loop
// ======================================================
void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println("\n=========================================");
  Serial.println("🚗 啟動 ESP32-S3-CAM AIoT 尋物小車 (含網站中控開關)...");
  Serial.println("=========================================");

  // 優先初始化相機 (避免舵機/馬達搶電造成相機因電壓驟降而初始化失敗)
  if (!initCamera()) {
    Serial.println("💥 相機啟動失敗，請檢查硬體排線與 PSRAM 設定！");
  } else {
    Serial.println("🎉 相機初始化完成！");
  }

  initMotors();
  initServoPin(SERVO_BASE_PIN);
  initServoPin(SERVO_ARM1_PIN);
  initServoPin(SERVO_ARM2_PIN);
  initServoPin(SERVO_GRIP_PIN);
  armReadySequence();

  Serial.printf("📡 正在連線至 WiFi: [%s] ...\n", ssid);
  WiFi.disconnect(true);
  delay(100);
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  WiFi.setSleep(false);

  int wifiRetries = 0;
  while (WiFi.status() != WL_CONNECTED && wifiRetries < 40) {
    delay(500);
    Serial.print(".");
    wifiRetries++;
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("✅ WiFi 連線成功！");
    Serial.print("🌐 ESP32 本機 IP: ");
    Serial.println(WiFi.localIP());
    Serial.printf("🎯 上傳目標中控網址: %s\n", serverUrl);
  } else {
    Serial.println(
        "❌ WiFi 連線超時！(若使用手機熱點，請務必開啟「最大化相容性」以支援 "
        "2.4GHz)");
    printWifiStatus(WiFi.status());
  }
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("⚠️ WiFi 未連線或斷線，正在重新連線...");
    printWifiStatus(WiFi.status());
    WiFi.disconnect();
    delay(500);
    WiFi.begin(ssid, password);
    delay(3000);
    return;
  }

  // ----------------------------------------------------
  // ⏸️ 待命休眠分支：當網站手動關閉鏡頭與回傳時執行
  // ----------------------------------------------------
  if (!isStreamActive) {
    carStop(); // 確保小車底盤安全煞車停住

    // 衍生狀態輪詢網址 (只傳數十個字節，極低功耗)
    String checkUrl = String(serverUrl);
    checkUrl.replace("/api/upload", "/api/stream_state");

    HTTPClient http;
    http.begin(checkUrl);
    http.setTimeout(3000);
    int code = http.GET();
    if (code == 200) {
      String json = http.getString();
#if ARDUINOJSON_VERSION_MAJOR >= 7
      JsonDocument doc;
#else
      DynamicJsonDocument doc(256);
#endif
      deserializeJson(doc, json);
      bool enabled = doc["stream_enabled"] | false;
      if (enabled) {
        isStreamActive = true;
        Serial.println("🚀 收到網站【啟動】指令！重啟相機拍照與自主尋物回傳！");
      }
    }
    http.end();
    delay(1000); // 待命期間每秒輪詢一次，超省電且響應快速
    return;
  }

  // ----------------------------------------------------
  // 🟢 運作中分支：全速拍照、即時回傳與智慧控制
  // ----------------------------------------------------
  camera_fb_t *fb = NULL;
  for (int retry = 0; retry < 3; retry++) {
    fb = esp_camera_fb_get();
    if (fb) break;
    delay(40);
  }

  if (!fb) {
    Serial.printf("❌ 拍照失敗！(可用 Heap: %d KB, 可用 PSRAM: %d KB)\n", 
                  ESP.getFreeHeap() / 1024, ESP.getFreePsram() / 1024);
    if (!psramFound()) {
      Serial.println("👉【關鍵原因】未啟用 PSRAM！請至 Arduino IDE「工具(Tools)」->「PSRAM」選為「OPI PSRAM」重新燒錄！");
    } else {
      Serial.println("👉 請檢查 OV2640 排線金手指是否插緊，或是否有舵機/馬達共用電源拉低電壓。");
    }
    delay(1000);
    return;
  }

  HTTPClient http;
  http.begin(serverUrl);
  http.setTimeout(5000);
  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("Connection", "close");

  unsigned long tStart = millis();
  int httpResponseCode = http.POST(fb->buf, fb->len);
  unsigned long rttMs = millis() - tStart;

  if (httpResponseCode == 200) {
    String response = http.getString();
    Serial.printf("⚡ [影像成功上傳至網站 %lu ms] HTTP 200\n", rttMs);
    executeCommands(response);
  } else if (httpResponseCode > 0) {
    Serial.printf("⚠️ 伺服器狀態碼: %d\n", httpResponseCode);
  } else {
    Serial.printf(
        "❌ 上傳失敗: %s (代碼: %d，請確認電腦防火牆或伺服器 IP: %s)\n",
        http.errorToString(httpResponseCode).c_str(), httpResponseCode,
        serverUrl);
  }

  http.end();
  esp_camera_fb_return(fb);

  delay(100); // 動作微震緩衝
}