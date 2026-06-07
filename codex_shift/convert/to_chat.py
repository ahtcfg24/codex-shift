"""Responses 请求 -> OpenAI Chat Completions 请求。

尽量全面地映射字段;无法对应的语义优先保留为等价表达,其余忽略。
"""

from __future__ import annotations

import json
from typing import Any

from . import common

# Responses 顶层字段 -> Chat 顶层字段 的直通映射(同名或简单改名)
# 这些字段语义一致,可直接拷贝。
_PASSTHROUGH = {
    "top_p": "top_p",
    "temperature": "temperature",
    "stream": "stream",
    "parallel_tool_calls": "parallel_tool_calls",
    "metadata": "metadata",
    "user": "user",
    "seed": "seed",
    "frequency_penalty": "frequency_penalty",
    "presence_penalty": "presence_penalty",
    "logit_bias": "logit_bias",
    "n": "n",
}


def _content_to_chat(content: Any, role: str) -> Any:
    """将 Responses 消息 content 转换为 Chat 消息 content。

    - 字符串原样返回。
    - 数组逐部分转换: input_text/output_text/text -> text; input_image -> image_url。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "text"):
            parts.append({"type": "text", "text": part.get("text", "")})
        elif ptype == "input_image":
            # Responses 用 image_url 或 file_id 引用图片;Chat 用 image_url 对象
            image_url = part.get("image_url")
            if isinstance(image_url, str):
                url = image_url
            elif isinstance(image_url, dict):
                url = image_url.get("url", "")
            else:
                url = ""
            if not url and part.get("file_id"):
                # 无法直接转换 file_id,跳过(避免发送无效引用)
                continue
            img: dict[str, Any] = {"url": url}
            if part.get("detail"):
                img["detail"] = part["detail"]
            parts.append({"type": "image_url", "image_url": img})
        elif ptype == "input_file":
            # Chat 通过 file 类型部分支持文件输入
            file_obj: dict[str, Any] = {}
            if part.get("file_id"):
                file_obj["file_id"] = part["file_id"]
            if part.get("filename"):
                file_obj["filename"] = part["filename"]
            if part.get("file_data"):
                file_obj["file_data"] = part["file_data"]
            if file_obj:
                parts.append({"type": "file", "file": file_obj})
        elif ptype == "refusal":
            # 助手拒绝内容在 Chat 中通过 refusal 字段表达,这里降级为文本
            parts.append({"type": "text", "text": part.get("refusal", "")})

    # 纯文本场景退化为字符串,更贴近常见 Chat 用法
    if all(p.get("type") == "text" for p in parts):
        return "".join(p["text"] for p in parts)
    return parts


def _convert_messages(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将 Responses 输入项列表转换为 Chat messages 列表。"""
    messages: list[dict[str, Any]] = []

    for item in items:
        itype = common.item_type(item)

        if itype == "message":
            role = item.get("role", "user")
            # developer 角色统一降级为 system: OpenAI 官方接受 developer,
            # 但多数 OpenAI 兼容后端(如 DeepSeek)只认 system,二者语义等价。
            if role == "developer":
                role = "system"
            chat_msg: dict[str, Any] = {
                "role": role,
                "content": _content_to_chat(item.get("content"), role),
            }
            if item.get("name"):
                chat_msg["name"] = item["name"]
            messages.append(chat_msg)

        elif itype == "function_call":
            # 助手发起的函数调用 -> assistant 消息的 tool_calls
            # 历史调用项的 namespace 为独立字段,需组合回扁平名以匹配工具定义
            call_name = common.combine_call_name(item.get("name", ""), item.get("namespace"))
            tool_call = {
                "id": item.get("call_id") or item.get("id") or common.new_id("call"),
                "type": "function",
                "function": {
                    "name": call_name,
                    "arguments": item.get("arguments", "") or "",
                },
            }
            # 合并到上一条 assistant 消息,否则新建
            if messages and messages[-1].get("role") == "assistant" and "tool_calls" in messages[-1]:
                messages[-1]["tool_calls"].append(tool_call)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [tool_call]})

        elif itype == "function_call_output":
            # 工具执行结果 -> tool 消息
            output = item.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id", ""),
                "content": output,
            })

        elif itype == "reasoning":
            # Chat Completions 不接受推理项作为输入,忽略以避免上游报错
            continue
        # 其余未知类型忽略

    return messages


def _convert_tools(tools: Any, *, supports_web_search: bool = False) -> tuple[list[dict[str, Any]] | None, bool]:
    """Responses tools -> Chat 嵌套 function tools。

    展平 namespace 工具为各 function 子工具;无法表达为 function 的类型被丢弃
    (Chat 协议只接受 type=function,透传其它类型会被上游拒绝)。
    web_search 工具在 provider 支持时透传(保留原始参数如 max_keyword/force_search)。

    返回 (tools_list, has_web_search):
    - tools_list: 转换后的工具列表,无工具时为 None
    - has_web_search: 是否包含 web_search 工具(供调用方设置 webSearchEnabled)
    """
    func_descs, _name_map, _dropped, web_search_tools = common.flatten_function_tools(tools)
    converted: list[dict[str, Any]] = []
    has_web_search = False
    for desc in func_descs:
        # Responses 扁平 function -> Chat 嵌套 {type, function: {...}}
        fn: dict[str, Any] = {
            "name": desc.get("name", ""),
            "description": desc.get("description", ""),
            "parameters": desc.get("parameters", {}),
        }
        if "strict" in desc:
            fn["strict"] = desc["strict"]
        converted.append({"type": "function", "function": fn})
    # web_search 工具: provider 支持时透传,否则丢弃
    if supports_web_search and web_search_tools:
        for ws in web_search_tools:
            converted.append(ws)
        has_web_search = True
    return converted or None, has_web_search


def _convert_tool_choice(tc: Any) -> Any:
    """Responses tool_choice -> Chat tool_choice。"""
    if isinstance(tc, str):
        # auto / none / required 在两协议一致
        return tc
    if isinstance(tc, dict):
        if tc.get("type") == "function":
            name = tc.get("name") or tc.get("function", {}).get("name", "")
            return {"type": "function", "function": {"name": name}}
        return tc
    return None


def _convert_text_format(text: Any) -> dict[str, Any] | None:
    """Responses text.format -> Chat response_format。"""
    if not isinstance(text, dict):
        return None
    fmt = text.get("format")
    if not isinstance(fmt, dict):
        return None
    ftype = fmt.get("type")
    if ftype == "json_schema":
        # Responses: {type, name, schema, strict}
        # Chat:      {type, json_schema: {name, schema, strict}}
        js = {"name": fmt.get("name", "response"), "schema": fmt.get("schema", {})}
        if "strict" in fmt:
            js["strict"] = fmt["strict"]
        if "description" in fmt:
            js["description"] = fmt["description"]
        return {"type": "json_schema", "json_schema": js}
    if ftype == "json_object":
        return {"type": "json_object"}
    if ftype == "text":
        return {"type": "text"}
    return None


def responses_to_chat(req: dict[str, Any], *, model: str | None,
                       passthrough_unknown: bool = True,
                       supports_web_search: bool = False,
                       web_search_enabled: bool = False) -> dict[str, Any]:
    """将 Responses 请求体转换为 Chat Completions 请求体。

    model 为映射后的模型名(由调用方传入)。
    supports_web_search: provider 是否支持 web_search 内置工具。
    web_search_enabled: 当请求携带 web_search 工具时,是否在顶层附加 webSearchEnabled=true
                        (部分 mimo 端点如 token-plan 强制要求此开关,否则上游返回 400)。
    """
    out: dict[str, Any] = {}
    if model is not None:
        out["model"] = model

    # instructions -> 置于消息列表最前的 system 消息
    messages = _convert_messages(common.normalize_input(req.get("input")))
    instructions = req.get("instructions")
    if instructions:
        messages.insert(0, {"role": "system", "content": instructions})
    out["messages"] = messages

    # 直通字段
    for src, dst in _PASSTHROUGH.items():
        if src in req and req[src] is not None:
            out[dst] = req[src]

    # max_output_tokens -> max_completion_tokens(Chat 新字段,兼容性更好)
    if req.get("max_output_tokens") is not None:
        out["max_completion_tokens"] = req["max_output_tokens"]

    # reasoning.effort -> reasoning_effort
    # Responses 用嵌套对象 {"reasoning": {"effort": "..."}},
    # Chat Completions 用顶层字段 reasoning_effort(取值 minimal/low/medium/high)。
    reasoning = req.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if effort is not None:
            out["reasoning_effort"] = effort

    # 工具与工具选择
    tools, has_web_search = _convert_tools(req.get("tools"), supports_web_search=supports_web_search)
    if tools:
        out["tools"] = tools
    # 部分 mimo 端点(如 token-plan)要求顶层显式开启 webSearchEnabled,
    # 否则即使 tools 中带 web_search 也会返回 400;由 provider 配置决定是否附加。
    if has_web_search and web_search_enabled:
        out["webSearchEnabled"] = True
    if req.get("tool_choice") is not None:
        tc = _convert_tool_choice(req["tool_choice"])
        if tc is not None:
            out["tool_choice"] = tc

    # 结构化输出格式
    rf = _convert_text_format(req.get("text"))
    if rf:
        out["response_format"] = rf

    # 流式场景请求用量统计
    if req.get("stream"):
        out["stream_options"] = {"include_usage": True}

    # 透传未识别字段(避免丢失上游可能支持的能力)
    if passthrough_unknown:
        known = set(_PASSTHROUGH) | {
            "model", "input", "instructions", "max_output_tokens",
            "tools", "tool_choice", "text", "stream", "reasoning",
            "previous_response_id", "store", "include", "background",
            "conversation", "context_management", "prompt",
        }
        for k, v in req.items():
            if k not in known and k not in out:
                out[k] = v

    return out
