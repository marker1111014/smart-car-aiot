# main.py
# 同步 app.py 之最新零延遲管線與主動巡航架構
import app

if __name__ == "__main__":
    import uvicorn
    local_ip = app.get_local_ip()
    print("=" * 60)
    print("🚗 AIoT 尋物小車超低延遲中控中心啟動成功！ (main.py)")
    print(f"👉 本機電腦請開瀏覽器： http://localhost:8001")
    print(f"👉 同區網設備 (手機/ESP32)： http://{local_ip}:8001")
    print(f"📡 ESP32 serverUrl 請設為: http://{local_ip}:8001/api/upload")
    print("=" * 60)
    uvicorn.run(app.app, host="0.0.0.0", port=8001)
