# schema.py
# 嚴格對接 ESP32-S3 底層控制端 - AI 上位機 HTTP JSON API 對接規範
from typing import Optional, Literal
from pydantic import BaseModel, Field


class ESP32Command(BaseModel):
    """
    符合 ESP32-S3 HTTP RESTful JSON 規範之標準指令封包模型 (模式 A ~ E)
    """
    # 模式 A & E: 底盤運動控制
    action: Optional[Literal[
        "forward", "backward", "left", "right",
        "spin_left", "spin_right", "stop", "brake"
    ]] = Field(
        default=None,
        description="底盤動作: forward(前進), backward(後退), left(左轉), right(右轉), spin_left(左旋轉), spin_right(右旋轉), stop(停止), brake(急煞)"
    )
    speed: Optional[int] = Field(
        default=None,
        ge=0,
        le=255,
        description="PWM 速度: 80 ~ 255 (預設 200)"
    )
    duration: Optional[int] = Field(
        default=None,
        ge=0,
        description="執行毫秒數 (例如 1000 代表執行 1 秒後自動安全停車；不填則持續移動)"
    )

    # 模式 B: 機械手臂 3D/2D 空間座標 (IK 逆運動學)
    x: Optional[float] = Field(
        default=None,
        description="車前水平伸展距離 (單位: 毫米 mm)"
    )
    z: Optional[float] = Field(
        default=None,
        description="離地垂直高度 (單位: 毫米 mm)"
    )

    # 模式 C & E: 機械手臂關節角度
    shoulder: Optional[int] = Field(
        default=None,
        ge=15,
        le=165,
        description="大臂仰角: 15 ~ 165 度"
    )
    elbow: Optional[int] = Field(
        default=None,
        ge=15,
        le=165,
        description="小臂俯仰: 15 ~ 165 度"
    )
    wrist: Optional[int] = Field(
        default=None,
        ge=0,
        le=180,
        description="手腕滾轉: 0 ~ 180 度 (90度為水平夾，0度為垂直夾)"
    )
    gripper: Optional[int] = Field(
        default=None,
        ge=15,
        le=95,
        description="夾爪開合: 15 (緊閉) ~ 95 (全開) 度 (20度=夾緊, 90度=完全張開)"
    )

    # 模式 D: 機械手臂一鍵預設姿態
    preset: Optional[Literal["home", "ground", "lift", "open", "close"]] = Field(
        default=None,
        description="預設姿態: home(復位待命), ground(地面低位抓取), lift(抓起抬高收納), open(張開爪子), close(夾緊爪子)"
    )

    def to_payload(self) -> dict:
        """過濾所有 None 鍵，輸出符合規範的純淨 JSON dict"""
        return {k: v for k, v in self.model_dump().items() if v is not None}


class RobotResponse(BaseModel):
    """
    上位機 Gemini 多模態 AI 視覺決策模型
    """
    target_found: bool = Field(description="畫面上是否識別到目標物 (true/false)")
    target_description: str = Field(description="簡短描述目標物狀態或位置 (20字以內)")
    command: ESP32Command = Field(description="符合 ESP32 規範的具體執行指令 (模式 A~E)")

