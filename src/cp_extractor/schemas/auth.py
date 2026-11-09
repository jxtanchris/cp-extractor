"""
用户认证与授权数据契约模型
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class UserBase(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, description="用户名 (3-50字符)")
    display_name: Optional[str] = Field(default="", max_length=50, description="工程师姓名/昵称")


class UserRegisterRequest(UserBase):
    password: str = Field(..., min_length=6, max_length=128, description="登录密码 (至少6位)")


class UserLoginRequest(BaseModel):
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


class UserResponse(BaseModel):
    id: int = Field(..., description="用户唯一ID")
    username: str = Field(..., description="用户名")
    display_name: str = Field(default="", description="工程师姓名/昵称")
    created_at: str = Field(..., description="账号注册时间")
    last_login_at: Optional[str] = Field(default=None, description="最后登录时间")
    is_active: bool = Field(default=True, description="账号是否启用")


class TokenResponse(BaseModel):
    access_token: str = Field(..., description="认证凭证 Bearer Token")
    token_type: str = Field(default="bearer", description="Token 类型")
    user: UserResponse = Field(..., description="当前用户信息")
