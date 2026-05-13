from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    role: str = "user"


class UserLogin(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: str
    username: str
    role: str
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class DatasetOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    created_at: datetime


class ImportOut(BaseModel):
    task_id: str
    file_id: str
    status: str
    message: str


class ImportTaskOut(BaseModel):
    id: str
    file_id: str
    dataset_id: str
    status: str
    data_type: str
    gis_category: str | None = None
    error_rows: list[dict[str, Any]] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class GraphBuildRequest(BaseModel):
    dataset_id: str


class GraphBuildOut(BaseModel):
    task_id: str
    status: str
    message: str


class GraphTaskOut(BaseModel):
    id: str
    dataset_id: str
    status: str
    progress: int = 0
    logs: list[str] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class GraphNode(BaseModel):
    id: str
    label: str
    type: str
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    label: str
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphOut(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class QuestionIn(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    dataset_id: str | None = None


class AnswerOut(BaseModel):
    answer: str
    route: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
