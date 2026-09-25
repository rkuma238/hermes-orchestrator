"""Pydantic models for the Open Skill Protocol (OSP) manifest.

Mirrors spec/skill-manifest.schema.json — see spec/SPEC.md for the full
protocol description.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

OSP_VERSION = "0.1"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_ENTRYPOINT_RE = re.compile(r"^[A-Za-z0-9_./]+\.py:[A-Za-z_][A-Za-z0-9_]*$")
_CAPABILITY_RE = re.compile(r"^(net|fs|env):.+$|^none$")


class ResourceLimits(BaseModel):
    timeout_seconds: float = Field(default=10, le=300)
    max_memory_mb: int = 256


class PayloadRef(BaseModel):
    url: str
    sha256: str
    signature: str | None = None

    @field_validator("sha256")
    @classmethod
    def _check_sha256(cls, v: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{64}", v):
            raise ValueError("sha256 must be a 64-char lowercase hex digest")
        return v


class Publisher(BaseModel):
    name: str | None = None
    account_id: str | None = None
    public_key: str | None = None


class SkillManifest(BaseModel):
    osp_version: Literal["0.1"] = OSP_VERSION
    id: str
    version: str
    name: str
    description: str
    runtime: Literal["python3.11", "python3.12", "python3.13"]
    entrypoint: str
    input_schema: dict
    output_schema: dict
    capabilities: list[str] = Field(default_factory=list)
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    payload: PayloadRef
    publisher: Publisher | None = None
    visibility: Literal["public", "private"] = "private"
    allowed_accounts: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not _ID_RE.fullmatch(v):
            raise ValueError(f"invalid skill id: {v!r}")
        return v

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if not _VERSION_RE.fullmatch(v):
            raise ValueError(f"invalid semver version: {v!r}")
        return v

    @field_validator("entrypoint")
    @classmethod
    def _check_entrypoint(cls, v: str) -> str:
        if not _ENTRYPOINT_RE.fullmatch(v):
            raise ValueError(f"invalid entrypoint: {v!r}, expected 'file.py:function'")
        return v

    @field_validator("capabilities")
    @classmethod
    def _check_capabilities(cls, v: list[str]) -> list[str]:
        for cap in v:
            if not _CAPABILITY_RE.fullmatch(cap):
                raise ValueError(f"invalid capability: {cap!r}")
        return v

    def entrypoint_parts(self) -> tuple[str, str]:
        path, _, func = self.entrypoint.partition(":")
        return path, func


class SkillSummary(BaseModel):
    """Lightweight projection of a manifest, returned by /discover."""

    id: str
    version: str
    name: str
    description: str
    capabilities: list[str]
    input_schema: dict
    output_schema: dict
    visibility: str = "private"

    @classmethod
    def from_manifest(cls, m: SkillManifest) -> "SkillSummary":
        return cls(
            id=m.id,
            version=m.version,
            name=m.name,
            description=m.description,
            capabilities=m.capabilities,
            input_schema=m.input_schema,
            output_schema=m.output_schema,
            visibility=m.visibility,
        )
