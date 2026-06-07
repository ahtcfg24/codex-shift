"""上游请求转发与 SSE 解析。

依据 provider 的出站协议构造请求头,负责非流式与流式两种转发,并提供
将上游 SSE 字节流解析为 (event, data) 的异步生成器。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import ProviderConfig

logger = logging.getLogger("codex_shift.upstream")


def build_headers(provider: ProviderConfig) -> dict[str, str]:
    """根据 provider 的出站协议构造上游请求头(含鉴权)。"""
    headers = {"content-type": "application/json"}
    if provider.api_key:
        headers["authorization"] = f"Bearer {provider.api_key}"
    return headers


def upstream_url(provider: ProviderConfig) -> str:
    """provider 的完整上游 URL。"""
    return f"{provider.base_url}{provider.path}"


async def forward_json(provider: ProviderConfig, payload: dict[str, Any]) -> httpx.Response:
    """非流式转发: 发送 JSON 并返回完整响应对象。"""
    headers = build_headers(provider)
    async with httpx.AsyncClient(timeout=provider.timeout) as client:
        resp = await client.post(upstream_url(provider), json=payload, headers=headers)
    return resp


async def stream_raw(provider: ProviderConfig, payload: dict[str, Any]) -> AsyncIterator[tuple[str, Any]]:
    """流式原样透传: 不解析 SSE,逐块产出上游响应原文(用于 responses 全透传)。

    产出 (kind, data):
    - ("__error__", {status_code, body}): 上游返回错误状态码;
    - ("__chunk__", text): 上游响应体的一个原始文本块(保持 [DONE]、事件分帧等不变)。
    """
    headers = build_headers(provider)
    async with httpx.AsyncClient(timeout=provider.timeout) as client:
        async with client.stream("POST", upstream_url(provider), json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                logger.warning("上游流式请求(透传)返回错误状态 %s", resp.status_code)
                yield "__error__", {
                    "status_code": resp.status_code,
                    "body": body.decode("utf-8", errors="replace"),
                }
                return
            # 字节级透传: SSE 客户端可容忍任意分块边界
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield "__chunk__", chunk.decode("utf-8", errors="replace")


async def stream_sse(provider: ProviderConfig, payload: dict[str, Any]) -> AsyncIterator[tuple[str | None, dict[str, Any]]]:
    """流式转发: 逐条产出上游 SSE 事件 (event_name, data_dict)。

    遇到上游错误状态码时,产出一个 ("__error__", {...}) 供调用方处理。
    """
    headers = build_headers(provider)
    async with httpx.AsyncClient(timeout=provider.timeout) as client:
        async with client.stream("POST", upstream_url(provider), json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                logger.warning("上游流式请求返回错误状态 %s", resp.status_code)
                yield "__error__", {
                    "status_code": resp.status_code,
                    "body": body.decode("utf-8", errors="replace"),
                }
                return
            # 逐行解析 SSE: 累积 event 与 data 直到空行
            event_name: str | None = None
            data_lines: list[str] = []
            async for raw_line in resp.aiter_lines():
                line = raw_line.rstrip("\r")
                if line == "":
                    # 事件边界
                    if data_lines:
                        data_str = "\n".join(data_lines)
                        if data_str != "[DONE]":
                            try:
                                yield event_name, json.loads(data_str)
                            except json.JSONDecodeError:
                                logger.debug("无法解析的 SSE data: %r", data_str)
                    event_name = None
                    data_lines = []
                    continue
                if line.startswith(":"):
                    # 注释行(心跳),忽略
                    continue
                if line.startswith("event:"):
                    event_name = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].lstrip(" "))
            # 流末若残留未以空行结束的事件,补充处理
            if data_lines:
                data_str = "\n".join(data_lines)
                if data_str != "[DONE]":
                    try:
                        yield event_name, json.loads(data_str)
                    except json.JSONDecodeError:
                        pass
