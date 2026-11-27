from typing import Optional

from pydantic import BaseModel


class UserInfo(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    must_change_password: bool = False
    department_id: Optional[int] = None   # 所属部门
    permission_codes: list = []

    model_config = {"from_attributes": True}


class LoginRequest(BaseModel):
    username: str
    password: str
    captcha_id: str = ""
    captcha_code: str = ""
    slider_ticket: str = ""   # 滑块验证码一次性票据（与图形验证码二选一）
    otp_code: str = ""   # TOTP 二步（账号启用 OTP 时必填）


class LoginResponse(BaseModel):
    token: str
    user: UserInfo
