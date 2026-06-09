"""FastAPI 服务: 暴露 /v1/responses,完成入站 Responses -> 出站协议的转换与转发。"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any, AsyncGenerator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import admin
from .config import OUTBOUND_RESPONSES, Config, ProviderConfig, load_config
from .convert import common, from_chat, to_chat
from .models_catalog import build_models_catalog, catalog_etag
from . import upstream

logger = logging.getLogger("codex_shift.server")

# 默认配置文件路径,可由环境变量覆盖
DEFAULT_CONFIG_PATH = os.environ.get("CODEX_SHIFT_CONFIG", "config.yaml")


def _passthrough_payload(body: dict[str, Any], model: str | None) -> dict[str, Any]:
    """responses 全透传: 请求体原样透传,仅按 provider 内部映射改写 model 字段。

    入站与上游同为 Responses 协议,无需任何结构转换;唯一改写是把 model 换为
    选中 provider 的出站模型名,其余字段(input/tools/reasoning/stream 等)保持不变。
    """
    payload = dict(body)
    if model is not None:
        payload["model"] = model
    return payload


def _build_request_payload(provider: ProviderConfig, body: dict[str, Any],
                           model: str | None) -> dict[str, Any]:
    """根据 provider 出站协议将 Responses 请求体转换为上游请求体。

    model 为经 provider 内部映射后的出站(实际)model 名。
    """
    if provider.outbound == OUTBOUND_RESPONSES:
        return _passthrough_payload(body, model)
    return to_chat.responses_to_chat(
        body, model=model, passthrough_unknown=provider.passthrough_unknown,
        supports_web_search=provider.supports_web_search,
        web_search_enabled=provider.web_search_enabled,
    )


def _convert_response(provider: ProviderConfig, upstream_json: dict[str, Any],
                      request_model: str | None,
                      name_map: dict[str, dict[str, str]] | None) -> dict[str, Any]:
    """将上游非流式响应体转换为 Responses 响应体。"""
    if provider.outbound == OUTBOUND_RESPONSES:
        # responses 全透传: 上游响应已是 Responses 协议,原样回送
        return upstream_json
    return from_chat.chat_to_responses(
        upstream_json, request_model=request_model, name_map=name_map)


def _dump(obj: Any, limit: int = 8000) -> str:
    """将对象序列化为日志友好的 JSON 字符串,超长则截断。"""
    try:
        text = json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(obj)
    if len(text) > limit:
        return text[:limit] + f"...(截断, 共 {len(text)} 字符)"
    return text


def _error_response(status_code: int, message: str, *, etype: str = "upstream_error",
                    raw: str | None = None) -> JSONResponse:
    """构造 Responses 风格的错误响应。"""
    err: dict[str, Any] = {"message": message, "type": etype}
    if raw is not None:
        err["raw"] = raw
    return JSONResponse(status_code=status_code, content={"error": err})


def _format_upstream_exception(exc: BaseException, provider: ProviderConfig) -> str:
    """格式化上游异常,确保 httpx 空消息异常也能透给客户端可读原因。"""
    parts = [type(exc).__name__]
    text = str(exc).strip()
    if text:
        parts.append(text)
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        cause_text = str(cause).strip()
        cause_desc = type(cause).__name__
        if cause_text:
            cause_desc = f"{cause_desc}: {cause_text}"
        if cause_desc not in parts:
            parts.append(cause_desc)
    return f"上游请求失败({upstream.upstream_url(provider)}): {'; '.join(parts)}"


async def _stream_generator(provider: ProviderConfig, payload: dict[str, Any],
                            request_model: str | None, rid: str = "-",
                            name_map: dict[str, dict[str, str]] | None = None) -> AsyncIterator[str]:
    """驱动流式转换,产出 Responses SSE 文本。"""
    converter = from_chat.ChatStreamConverter(request_model=request_model, name_map=name_map)

    upstream_events = 0
    try:
        async for event_name, data in upstream.stream_sse(provider, payload):
            if event_name == "__error__":
                # 上游错误: 以 error 事件告知客户端并结束
                logger.warning("[%s] 流式上游错误 status=%s: %s",
                               rid, data.get("status_code"), str(data.get("body"))[:1000])
                err_event = {
                    "type": "error",
                    "code": data.get("status_code"),
                    "message": data.get("body", "upstream error"),
                }
                yield f"event: error\ndata: {json.dumps(err_event, ensure_ascii=False)}\n\n"
                return
            upstream_events += 1
            for out in converter.feed(data):
                yield out
        # 正常结束: 产出收尾事件
        for out in converter.finish():
            yield out
        logger.info("[%s] 流式完成, 消费上游事件 %d 条", rid, upstream_events)
    except Exception as exc:  # noqa: BLE001 - 兜底,避免连接悬挂
        logger.exception("[%s] 流式转换过程中发生异常", rid)
        err_event = {
            "type": "error",
            "code": 502,
            "message": _format_upstream_exception(exc, provider),
        }
        yield f"event: error\ndata: {json.dumps(err_event, ensure_ascii=False)}\n\n"


async def _passthrough_stream_generator(provider: ProviderConfig, payload: dict[str, Any],
                                        rid: str = "-") -> AsyncIterator[str]:
    """responses 全透传的流式生成器: 原样转发上游 SSE 文本块。"""
    chunks = 0
    try:
        async for kind, data in upstream.stream_raw(provider, payload):
            if kind == "__error__":
                logger.warning("[%s] 流式上游错误(透传) status=%s: %s",
                               rid, data.get("status_code"), str(data.get("body"))[:1000])
                err_event = {
                    "type": "error",
                    "code": data.get("status_code"),
                    "message": data.get("body", "upstream error"),
                }
                yield f"event: error\ndata: {json.dumps(err_event, ensure_ascii=False)}\n\n"
                return
            chunks += 1
            yield data
        logger.info("[%s] 流式透传完成, 转发上游块 %d 个", rid, chunks)
    except Exception as exc:  # noqa: BLE001 - 兜底,避免连接悬挂
        logger.exception("[%s] 流式透传过程中发生异常", rid)
        err_event = {
            "type": "error",
            "code": 502,
            "message": _format_upstream_exception(exc, provider),
        }
        yield f"event: error\ndata: {json.dumps(err_event, ensure_ascii=False)}\n\n"


def create_app(cfg: Config, *, config_path: str | None = None) -> FastAPI:
    """创建并配置 FastAPI 应用。"""
    app = FastAPI(title="codex-shift", version="0.1.0")
    runtime = admin.RuntimeConfig.create(config_path or DEFAULT_CONFIG_PATH, cfg)
    app.state.runtime_config = runtime

    def current_config() -> Config:
        return runtime.get()

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        """统一记录每个入站请求的完整 path(含 query)、来源与响应状态。

        为每个请求分配短 ID 并存入 request.state.rid,供各端点复用以串联同一请求的日志。
        覆盖所有端点(/health、/models、/v1/responses 等),确保任何入站请求都能看到完整 path。
        """
        rid = uuid.uuid4().hex[:8]
        request.state.rid = rid
        client = request.client.host if request.client else "?"
        # 完整 path = 路径 + 原始 query string(如 codex 的 /v1/models?client_version=...)
        full_path = request.url.path
        if request.url.query:
            full_path = f"{full_path}?{request.url.query}"
        logger.info("[%s] 入站 %s %s (来自 %s)", rid, request.method, full_path, client)
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("[%s] 请求处理发生未捕获异常: %s %s", rid, request.method, full_path)
            raise
        logger.info("[%s] 完成 %s %s -> %s", rid, request.method, full_path, response.status_code)
        return response

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """健康检查,返回已配置的各 provider 概览。"""
        cur = current_config()
        return {
            "status": "ok",
            "providers": [
                {
                    "name": p.name,
                    "enabled": p.enabled,
                    "weight": p.weight,
                    "outbound": p.outbound,
                    "upstream": upstream.upstream_url(p),
                    "models": p.models,
                    "catch_all": p.catch_all,
                }
                for p in cur.active_providers
            ],
        }

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page() -> str:
        """本地 provider 开关与权重控制台。"""
        return admin.ADMIN_HTML

    @app.get("/admin/api/config")
    async def admin_config() -> dict[str, Any]:
        """返回当前 provider 控制项。"""
        cur = current_config()
        return {"providers": admin.provider_summary(cur, runtime.path)}

    @app.post("/admin/api/config")
    async def save_admin_config(request: Request) -> JSONResponse:
        """保存 provider enabled/weight 并热加载进程内配置。"""
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return _error_response(400, "请求体不是合法的 JSON", etype="invalid_request_error")
        if not isinstance(body, dict) or not isinstance(body.get("providers"), list):
            return _error_response(400, "请求体必须包含 providers 数组", etype="invalid_request_error")
        try:
            admin.update_provider_controls(runtime.path, body["providers"])
            cur = runtime.reload()
        except Exception as exc:  # noqa: BLE001 - 配置错误要返回给控制台
            logger.warning("控制台保存配置失败: %s", exc)
            return _error_response(400, f"保存配置失败: {exc}", etype="invalid_request_error")
        return JSONResponse(content={"providers": admin.provider_summary(cur, runtime.path)})

    @app.get("/v1/models")
    @app.get("/models")
    async def list_models(request: Request) -> JSONResponse:
        """codex 兼容的模型目录端点(挂载于 /models 与 /v1/models)。

        codex 在满足触发条件时请求 ``GET {base_url}/models?client_version=...``,
        据此读取各模型的 context_window 等元数据。仅暴露已配置上下文窗口的模型。
        """
        rid = getattr(request.state, "rid", None) or "-"
        catalog = build_models_catalog(current_config())
        etag = catalog_etag(catalog)
        # 支持条件请求: ETag 命中时返回 304,避免重复传输
        if request.headers.get("if-none-match") == etag:
            logger.info("[%s] /models 命中 ETag, 返回 304", rid)
            return JSONResponse(status_code=304, content=None, headers={"etag": etag})
        logger.info("[%s] /models 返回 %d 个模型, etag=%s", rid, len(catalog["models"]), etag)
        return JSONResponse(content=catalog, headers={"etag": etag})

    async def create_response(request: Request):
        """入站 Responses 协议端点的处理逻辑(挂载于 /v1/responses 与 /responses)。"""
        # 复用中间件分配的请求 ID(已记录完整 path),用于串联同一请求的多条日志
        rid = getattr(request.state, "rid", None) or uuid.uuid4().hex[:8]
        logger.debug("[%s] 请求头: %s", rid, dict(request.headers))

        # 解析请求体
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("[%s] 请求体不是合法 JSON: %s", rid, exc)
            return _error_response(400, "请求体不是合法的 JSON", etype="invalid_request_error")
        if not isinstance(body, dict):
            logger.warning("[%s] 请求体不是 JSON 对象, 实际类型=%s", rid, type(body).__name__)
            return _error_response(400, "请求体必须是 JSON 对象", etype="invalid_request_error")

        request_model = body.get("model")
        is_stream = bool(body.get("stream"))
        logger.info("[%s] 入站请求 model=%s", rid, request_model)

        cur = current_config()
        # 路由: 按入站 model 定位 provider;重复模型按权重选择,再取 provider 内部出站映射。
        provider, mapped_model = cur.resolve(request_model)
        if provider is None:
            logger.warning("[%s] 无法为 model=%s(出站候选=%s)路由到任何 provider",
                           rid, request_model, mapped_model)
            return _error_response(
                404,
                f"无法为入站 model {request_model!r} 找到匹配的 provider;"
                f"请在某个 provider 的 models[].name 中声明该 model 名",
                etype="model_not_found",
            )

        # responses 全透传无需工具协议转换,不做展平/回程拆名
        is_passthrough = provider.outbound == OUTBOUND_RESPONSES
        if is_passthrough:
            name_map = None
            logger.info(
                "[%s] model=%s -> %s, provider=%s, stream=%s, 模式=responses全透传",
                rid, request_model, mapped_model, provider.name, is_stream,
            )
        else:
            # 提取工具命名空间映射(用于回程拆名)与被丢弃的工具类型
            _flat, name_map, dropped, web_search_tools = common.flatten_function_tools(body.get("tools"))
            logger.info(
                "[%s] model=%s -> %s, provider=%s, stream=%s, tools=%d(展平后 function=%d, web_search=%d)",
                rid, request_model, mapped_model, provider.name, is_stream,
                len(body.get("tools") or []), len(_flat), len(web_search_tools),
            )
            if dropped:
                logger.warning("[%s] 丢弃无法转换为 function 的工具类型: %s", rid, dropped)
            if web_search_tools and not provider.supports_web_search:
                logger.warning("[%s] provider %s 不支持 web_search,已丢弃", rid, provider.name)
        logger.debug("[%s] 入站请求体: %s", rid, _dump(body))

        # 转换请求
        try:
            payload = _build_request_payload(provider, body, mapped_model)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] 请求转换失败", rid)
            return _error_response(400, f"请求转换失败: {exc}", etype="conversion_error")
        logger.info("[%s] 出站请求 -> provider=%s, 协议=%s, url=%s, model=%s",
                    rid, provider.name, provider.outbound, upstream.upstream_url(provider), mapped_model)
        logger.debug("[%s] 出站请求体: %s", rid, _dump(payload))

        # 流式: 返回 SSE。responses 全透传走原样转发,其余走协议转换
        if is_stream:
            logger.info("[%s] 进入流式转发", rid)
            stream_gen = (
                _passthrough_stream_generator(provider, payload, rid)
                if is_passthrough
                else _stream_generator(provider, payload, request_model, rid, name_map)
            )
            # 预取首个 SSE 事件,若上游直接返回错误则回退为 JSON 错误响应
            try:
                first_chunk = await stream_gen.__anext__()
            except StopAsyncIteration:
                first_chunk = ""

            if first_chunk.startswith("event: error"):
                # 解析 SSE error 事件中的 data JSON 抽取 code / message
                err_code = 502
                err_message = "上游返回错误"
                err_raw = first_chunk
                for line in first_chunk.split("\n"):
                    if line.startswith("data:"):
                        try:
                            err_data = json.loads(line[len("data:"):].strip())
                        except json.JSONDecodeError:
                            break
                        if isinstance(err_data, dict):
                            err_code = err_data.get("code", err_code)
                            err_message = err_data.get("message", err_message)
                        break
                logger.warning("[%s] 流式上游直接返回错误, 回退为 JSON 错误响应 code=%s",
                               rid, err_code)
                return _error_response(
                    err_code, err_message, raw=err_raw,
                )

            # 首块正常: 拼接回传,维持 StreamingResponse
            async def _prepended_gen(
                first: str, rest: AsyncGenerator[str, None],
            ) -> AsyncIterator[str]:
                yield first
                async for chunk in rest:
                    yield chunk
            stream_gen = _prepended_gen(first_chunk, stream_gen)
            return StreamingResponse(
                stream_gen,
                media_type="text/event-stream",
                headers={"cache-control": "no-cache", "connection": "keep-alive"},
            )

        # 非流式: 转发并转换
        try:
            resp = await upstream.forward_json(provider, payload)
        except httpx.TimeoutException as exc:
            logger.warning("[%s] 上游请求超时: %s", rid, exc)
            return _error_response(504, "上游请求超时", etype="timeout_error")
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] 上游请求失败", rid)
            return _error_response(502, _format_upstream_exception(exc, provider), etype="upstream_error")

        logger.info("[%s] 上游响应 status=%s", rid, resp.status_code)
        if resp.status_code >= 400:
            # 透传上游错误信息
            raw = resp.text
            logger.warning("[%s] 上游返回错误状态 %s: %s", rid, resp.status_code, raw[:1000])
            return _error_response(resp.status_code, "上游返回错误", raw=raw)

        try:
            upstream_json = resp.json()
        except json.JSONDecodeError:
            logger.warning("[%s] 上游响应不是合法 JSON: %s", rid, resp.text[:1000])
            return _error_response(502, "上游响应不是合法 JSON", raw=resp.text[:1000])
        logger.debug("[%s] 上游响应体: %s", rid, _dump(upstream_json))

        responses_body = _convert_response(provider, upstream_json, request_model, name_map)
        logger.debug("[%s] 回送 Responses 响应: %s", rid, _dump(responses_body))
        return JSONResponse(content=responses_body)

    # 同时挂载到 /v1/responses 与 /responses,兼容不同客户端的 base_url 拼接方式
    app.add_api_route("/v1/responses", create_response, methods=["POST"])
    app.add_api_route("/responses", create_response, methods=["POST"])

    return app


def build_default_app() -> FastAPI:
    """从默认配置路径构造应用,供 `uvicorn --factory` 使用。"""
    return create_app(load_config(DEFAULT_CONFIG_PATH), config_path=DEFAULT_CONFIG_PATH)
