from typing import Any

from pydantic import BaseModel, Field


class LoginForm(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class CaseForm(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any]


class ReviewForm(BaseModel):
    note: str = Field(min_length=1, max_length=500)


class CastingEventForm(BaseModel):
    op: str = Field(min_length=1, max_length=40)
    # 领域事件字段差异较大，统一以透传对象承载，由领域服务负责校验。
    data: dict[str, Any] = Field(default_factory=dict)


class ErasureExecuteForm(BaseModel):
    job_id: str = Field(min_length=1, max_length=64)
