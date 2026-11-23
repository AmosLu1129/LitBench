# SPDX-License-Identifier: AGPL-3.0-or-later
"""设置 API（M2b）：用户在界面上配置自己的模型 API（BYO key）。

* ``GET    /api/settings/llm``      当前生效配置（key 只回掩码）
* ``PUT    /api/settings/llm``      保存配置（api_key 留空 = 保留已存的 key）
* ``DELETE /api/settings/llm``      清除配置，回落到环境变量
* ``POST   /api/settings/llm/test`` 测试连接（可先用表单值测，不落库）
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from ..auth import admin_when_enabled
from ..llm import effective_config, test_connection
from ..llm_config import (
    PROTOCOLS,
    LLMConfig,
    LLMConfigError,
    delete_config,
    endpoint_url,
    load_config,
    mask_api_key,
    save_config,
    supported_protocols,
    validate_config,
)
from ..models import AppUser
from ..schemas import LlmProtocolOut, LlmSettingsIn, LlmSettingsOut, LlmTestOut

router = APIRouter(prefix="/settings", tags=["settings"])


def _protocols_out() -> list[LlmProtocolOut]:
    return [LlmProtocolOut(**item) for item in supported_protocols()]


def _state() -> LlmSettingsOut:
    source, cfg = effective_config()
    if cfg is None:
        return LlmSettingsOut(
            configured=False,
            source="none",
            supported_protocols=_protocols_out(),
            note="尚未配置模型 API：配置后章节摘要等 AI 功能才可用。",
        )
    info = PROTOCOLS[cfg.protocol]
    return LlmSettingsOut(
        configured=True,
        source=source,
        protocol=cfg.protocol,
        protocol_label=info["label"],
        protocol_path=info["path"],
        base_url=cfg.base_url,
        model=cfg.model,
        has_api_key=bool(cfg.api_key),
        api_key_masked=mask_api_key(cfg.api_key),
        endpoint=endpoint_url(cfg.base_url, cfg.protocol),
        timeout_s=cfg.timeout_s,
        updated_at=cfg.updated_at,
        last_test_ok=cfg.last_test_ok,
        last_tested_at=cfg.last_tested_at,
        supported_protocols=_protocols_out(),
        note=(
            "当前使用本页保存的 API。"
            if source == "ui"
            else "当前使用服务端环境变量里的 API。保存本页配置会覆盖它。"
        ),
    )


def _same_target(a: LLMConfig, b: LLMConfig) -> bool:
    """协议/地址/模型/key 全一致才算「同一个配置」，只有这时测试结果才写回已存配置。"""
    return (
        a.protocol == b.protocol
        and a.base_url == b.base_url
        and a.model == b.model
        and a.api_key == b.api_key
    )


def _build_config(body: LlmSettingsIn, existing: LLMConfig | None) -> LLMConfig:
    """把入参 + 已存 key 合成完整配置；供保存和「测试连接」共用。"""
    api_key = (body.api_key or "").strip()
    if not api_key and existing is not None:
        api_key = existing.api_key
    try:
        protocol, base_url, model, timeout_s = validate_config(
            protocol=body.protocol,
            base_url=body.base_url,
            model=body.model,
            api_key=api_key or None,
            have_existing_key=bool(existing and existing.api_key),
            timeout_s=body.timeout_s,
        )
    except LLMConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    cfg = LLMConfig(
        protocol=protocol,
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_s=timeout_s,
        created_at=existing.created_at if existing else None,
        session_id=existing.session_id if existing else "",
        extra=existing.extra if existing else {},
    )
    # 配置没变就保留上次测试结果；改了目标就标成「未测试」
    if existing is not None and _same_target(existing, cfg):
        cfg.last_test_ok = existing.last_test_ok
        cfg.last_tested_at = existing.last_tested_at
    return cfg


@router.get("/llm", response_model=LlmSettingsOut)
async def get_llm_settings() -> LlmSettingsOut:
    return _state()


@router.put("/llm", response_model=LlmSettingsOut)
async def save_llm_settings(
    body: LlmSettingsIn,
    _admin: AppUser | None = Depends(admin_when_enabled),
) -> LlmSettingsOut:
    """保存服务端全局模型配置：认证打开后仅管理员可改（避免成员改写后
    把其他人的论文内容发到自己的 endpoint）。"""
    cfg = _build_config(body, load_config())
    save_config(cfg)
    return _state()


@router.delete("/llm", response_model=LlmSettingsOut)
async def clear_llm_settings(
    _admin: AppUser | None = Depends(admin_when_enabled),
) -> LlmSettingsOut:
    delete_config()
    return _state()


@router.post("/llm/test", response_model=LlmTestOut)
async def test_llm_settings(
    body: LlmSettingsIn | None = None,
    _admin: AppUser | None = Depends(admin_when_enabled),
) -> LlmTestOut:
    """测试连接。优先用表单当前值（未保存也能先测）；不传 body 则测已保存/环境变量配置。

    如果测的正是已保存的那份配置，把结果写回配置（顶栏状态灯用）；改了一半的未保存配置不写。
    """
    saved = load_config()
    if body is not None:
        cfg = _build_config(body, saved)
    else:
        source, cfg = effective_config()
        if cfg is None:
            raise HTTPException(
                status_code=400,
                detail="还没有可测试的配置：先填 base_url / model / API key，或保存后再测",
            )
    result = await test_connection(cfg)
    if saved is not None and _same_target(saved, cfg):
        saved.last_test_ok = bool(result.get("ok"))
        saved.last_tested_at = datetime.now(timezone.utc).isoformat()
        save_config(saved, touch_updated=False)
    return LlmTestOut(**result)
