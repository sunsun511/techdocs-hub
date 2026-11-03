from typing import Literal
from pydantic import BaseModel, Field, field_validator, ConfigDict


class ImportDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    title: str = Field(min_length=1, max_length=240)
    version_label: str = Field(min_length=1, max_length=80)
    source_url: str = Field(default="", max_length=2048)
    format: Literal["text", "markdown", "html"] = "markdown"
    content: str = Field(min_length=1, max_length=1_000_000)

    @field_validator("title", "version_label", "content")
    @classmethod
    def nonblank(cls, value):
        value = value.replace("\r\n", "\n").strip()
        if not value:
            raise ValueError("内容不能为空")
        return value

    @field_validator("source_url")
    @classmethod
    def safe_url(cls, value):
        from urllib.parse import urlparse
        if value and (urlparse(value).scheme not in ["http", "https"] or not urlparse(value).netloc):
            raise ValueError("来源须为完整 http/https 链接；留空则只保存本地文档")
        return value


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    language: Literal["auto", "en", "zh"] = "auto"
    method: Literal["auto", "bm25", "dense", "hybrid"] = "auto"
    top_k: int = Field(default=5, ge=1, le=20)
    document_id: str | None = None
    version_id: str | None = None
    # User-corrected English wording for a Chinese query; replaces only the machine translation step.
    translation: str | None = Field(default=None, max_length=500)

    @field_validator("query")
    @classmethod
    def query_nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("请输入搜索内容")
        return value

    @field_validator("translation")
    @classmethod
    def translation_blank_is_none(cls, value):
        if value is None:
            return None
        return value.strip() or None


class SwitchVersion(BaseModel):
    version_id: str
