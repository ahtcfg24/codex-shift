"""OpenAI Chat Completions 响应 -> Responses 响应(非流式与流式)。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from . import common


def _usage_to_responses(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """Chat usage -> Responses usage。"""
    if not usage:
        return None
    out: dict[str, Any] = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }
    # 透传细分用量(推理 token、缓存 token 等)
    if usage.get("completion_tokens_details"):
        out["output_tokens_details"] = usage["completion_tokens_details"]
    if usage.get("prompt_tokens_details"):
        out["input_tokens_details"] = usage["prompt_tokens_details"]
    return out


def _finish_reason_to_status(finish_reason: str | None) -> tuple[str, dict[str, Any] | None]:
    """Chat finish_reason -> (Responses status, incomplete_details)。"""
    if finish_reason == "length":
        return "incomplete", {"reason": "max_output_tokens"}
    if finish_reason == "content_filter":
        return "incomplete", {"reason": "content_filter"}
    # stop / tool_calls / function_call / None 均视为正常完成
    return "completed", None


def _message_to_output(message: dict[str, Any],
                       name_map: dict[str, dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """Chat 单条 assistant message -> Responses output 项列表。"""
    output: list[dict[str, Any]] = []

    # 文本/拒绝内容 -> message 项
    content = message.get("content")
    refusal = message.get("refusal")
    content_parts: list[dict[str, Any]] = []
    if isinstance(content, str) and content:
        content_parts.append({"type": "output_text", "text": content, "annotations": []})
    elif isinstance(content, list):
        # 某些实现返回结构化 content
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("text", "output_text"):
                content_parts.append({
                    "type": "output_text",
                    "text": part.get("text", ""),
                    "annotations": [],
                })
    if refusal:
        content_parts.append({"type": "refusal", "refusal": refusal})

    if content_parts:
        output.append({
            "id": common.new_id("msg"),
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": content_parts,
        })

    # 工具调用 -> function_call 项
    for tc in message.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function", {})
        # 回程拆名: 还原命名空间(独立字段)与裸名,供客户端正确路由
        bare, ns = common.split_call_name(fn.get("name", ""), name_map)
        item: dict[str, Any] = {
            "id": common.new_id("fc"),
            "type": "function_call",
            "call_id": tc.get("id", ""),
            "name": bare,
            "arguments": fn.get("arguments", "") or "",
            "status": "completed",
        }
        if ns:
            item["namespace"] = ns
        output.append(item)

    return output


def chat_to_responses(chat: dict[str, Any], *, request_model: str | None = None,
                      name_map: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    """将 Chat Completions 响应体转换为 Responses 响应体。"""
    choices = chat.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}

    output = _message_to_output(message, name_map)
    status, incomplete = _finish_reason_to_status(choice.get("finish_reason"))

    resp: dict[str, Any] = {
        "id": common.new_id("resp"),
        "object": "response",
        "created_at": chat.get("created", common.now_ts()),
        "status": status,
        "model": chat.get("model") or request_model,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }
    if incomplete:
        resp["incomplete_details"] = incomplete

    usage = _usage_to_responses(chat.get("usage"))
    if usage:
        resp["usage"] = usage

    # 便捷字段: 聚合纯文本输出
    resp["output_text"] = _aggregate_output_text(output)
    return resp


def _aggregate_output_text(output: list[dict[str, Any]]) -> str:
    """聚合所有 message 项中的 output_text 文本。"""
    texts: list[str] = []
    for item in output:
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    texts.append(part.get("text", ""))
    return "".join(texts)


# --------------------------------------------------------------------------
# 流式转换: Chat SSE chunk 流 -> Responses SSE 事件流
# --------------------------------------------------------------------------

def _sse(event: str, data: dict[str, Any]) -> str:
    """构造一条 Responses 风格的 SSE 文本(带 event 行)。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class ChatStreamConverter:
    """逐块消费 Chat 流式 chunk,产出 Responses 流式事件。

    维护输出项索引与状态,生成符合 Responses 语义的事件序列:
    response.created -> output_item.added -> content_part.added ->
    output_text.delta* -> output_text.done -> content_part.done ->
    output_item.done -> ... -> response.completed
    """

    def __init__(self, *, request_model: str | None = None,
                 name_map: dict[str, dict[str, str]] | None = None) -> None:
        self.request_model = request_model
        self.name_map = name_map
        self.response_id = common.new_id("resp")
        self.created_at = common.now_ts()
        self.model = request_model
        self.seq = 0  # 事件序号
        self.output_index = 0  # 当前输出项索引
        # 文本消息项状态
        self._text_open = False
        self._text_item_id: str | None = None
        self._text_buf: list[str] = []
        # 工具调用状态: index -> {item_id, call_id, name, args[]}
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._tool_order: list[int] = []
        self._finish_reason: str | None = None
        self._usage: dict[str, Any] | None = None
        self._started = False
        # 已收尾的输出项(用于 completed 事件重建完整 output)
        self._final_items: list[dict[str, Any]] = []

    def _next_seq(self) -> int:
        n = self.seq
        self.seq += 1
        return n

    def _response_skeleton(self, status: str) -> dict[str, Any]:
        """构造 response 对象骨架(用于 created/in_progress/completed 事件)。"""
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.model,
            "output": [],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
        }

    def start(self) -> Iterator[str]:
        """产出起始事件(response.created / in_progress)。"""
        if self._started:
            return
        self._started = True
        yield _sse("response.created", {
            "type": "response.created",
            "sequence_number": self._next_seq(),
            "response": self._response_skeleton("in_progress"),
        })
        yield _sse("response.in_progress", {
            "type": "response.in_progress",
            "sequence_number": self._next_seq(),
            "response": self._response_skeleton("in_progress"),
        })

    def _open_text_item(self) -> Iterator[str]:
        """打开文本消息项,产出 output_item.added 与 content_part.added。"""
        self._text_item_id = common.new_id("msg")
        self._text_open = True
        item = {
            "id": self._text_item_id,
            "type": "message",
            "role": "assistant",
            "status": "in_progress",
            "content": [],
        }
        yield _sse("response.output_item.added", {
            "type": "response.output_item.added",
            "sequence_number": self._next_seq(),
            "output_index": self.output_index,
            "item": item,
        })
        yield _sse("response.content_part.added", {
            "type": "response.content_part.added",
            "sequence_number": self._next_seq(),
            "item_id": self._text_item_id,
            "output_index": self.output_index,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        })

    def _close_text_item(self) -> Iterator[str]:
        """关闭文本消息项,产出 output_text.done / content_part.done / output_item.done。"""
        if not self._text_open:
            return
        text = "".join(self._text_buf)
        yield _sse("response.output_text.done", {
            "type": "response.output_text.done",
            "sequence_number": self._next_seq(),
            "item_id": self._text_item_id,
            "output_index": self.output_index,
            "content_index": 0,
            "text": text,
        })
        yield _sse("response.content_part.done", {
            "type": "response.content_part.done",
            "sequence_number": self._next_seq(),
            "item_id": self._text_item_id,
            "output_index": self.output_index,
            "content_index": 0,
            "part": {"type": "output_text", "text": text, "annotations": []},
        })
        text_item = {
            "id": self._text_item_id,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
        yield _sse("response.output_item.done", {
            "type": "response.output_item.done",
            "sequence_number": self._next_seq(),
            "output_index": self.output_index,
            "item": text_item,
        })
        self._final_items.append(text_item)
        self._text_open = False
        self._text_buf = []
        self.output_index += 1

    def feed(self, chunk: dict[str, Any]) -> Iterator[str]:
        """消费一个 Chat 流式 chunk,产出零或多条 Responses 事件。"""
        if not self._started:
            yield from self.start()

        if chunk.get("model"):
            self.model = chunk["model"]
        if chunk.get("usage"):
            self._usage = chunk["usage"]

        choices = chunk.get("choices") or []
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta") or {}

        # 文本增量
        content = delta.get("content")
        if content:
            # 出现文本前若已开启工具项,先不处理(Chat 通常文本在前)
            if not self._text_open:
                yield from self._open_text_item()
            self._text_buf.append(content)
            yield _sse("response.output_text.delta", {
                "type": "response.output_text.delta",
                "sequence_number": self._next_seq(),
                "item_id": self._text_item_id,
                "output_index": self.output_index,
                "content_index": 0,
                "delta": content,
            })

        # 工具调用增量
        for tc in delta.get("tool_calls") or []:
            yield from self._feed_tool_call(tc)

        if choice.get("finish_reason"):
            self._finish_reason = choice["finish_reason"]

    def _feed_tool_call(self, tc: dict[str, Any]) -> Iterator[str]:
        """处理工具调用增量(可能跨多个 chunk 累积 arguments)。"""
        idx = tc.get("index", 0)
        if idx not in self._tool_calls:
            # 新工具调用前,先收尾文本项(保证项顺序)
            if self._text_open:
                yield from self._close_text_item()
            item_id = common.new_id("fc")
            raw_name = (tc.get("function") or {}).get("name", "")
            bare, ns = common.split_call_name(raw_name, self.name_map)
            state = {
                "item_id": item_id,
                "call_id": tc.get("id", "") or common.new_id("call"),
                "raw": raw_name,   # 上游返回的组合名(用于判断是否已收到 name)
                "name": bare,       # 拆出的裸名
                "namespace": ns,    # 拆出的命名空间(可能为 None)
                "args": [],
                "output_index": self.output_index,
            }
            self._tool_calls[idx] = state
            self._tool_order.append(idx)
            self.output_index += 1
            added_item: dict[str, Any] = {
                "id": item_id,
                "type": "function_call",
                "call_id": state["call_id"],
                "name": state["name"],
                "arguments": "",
                "status": "in_progress",
            }
            if ns:
                added_item["namespace"] = ns
            yield _sse("response.output_item.added", {
                "type": "response.output_item.added",
                "sequence_number": self._next_seq(),
                "output_index": state["output_index"],
                "item": added_item,
            })
        state = self._tool_calls[idx]
        fn = tc.get("function") or {}
        if fn.get("name") and not state["raw"]:
            # name 跨 chunk 延迟到达: 补充拆名
            state["raw"] = fn["name"]
            state["name"], state["namespace"] = common.split_call_name(fn["name"], self.name_map)
        if tc.get("id") and not state["call_id"]:
            state["call_id"] = tc["id"]
        args_delta = fn.get("arguments")
        if args_delta:
            state["args"].append(args_delta)
            yield _sse("response.function_call_arguments.delta", {
                "type": "response.function_call_arguments.delta",
                "sequence_number": self._next_seq(),
                "item_id": state["item_id"],
                "output_index": state["output_index"],
                "delta": args_delta,
            })

    def _close_tool_calls(self) -> Iterator[str]:
        """收尾所有工具调用项。"""
        for idx in self._tool_order:
            state = self._tool_calls[idx]
            args = "".join(state["args"])
            yield _sse("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "sequence_number": self._next_seq(),
                "item_id": state["item_id"],
                "output_index": state["output_index"],
                "arguments": args,
            })
            done_item = {
                "id": state["item_id"],
                "type": "function_call",
                "call_id": state["call_id"],
                "name": state["name"],
                "arguments": args,
                "status": "completed",
            }
            if state.get("namespace"):
                done_item["namespace"] = state["namespace"]
            yield _sse("response.output_item.done", {
                "type": "response.output_item.done",
                "sequence_number": self._next_seq(),
                "output_index": state["output_index"],
                "item": done_item,
            })
            self._final_items.append(done_item)

    def finish(self) -> Iterator[str]:
        """流结束时产出收尾事件与 response.completed。"""
        yield from self._close_text_item()
        yield from self._close_tool_calls()

        status, incomplete = _finish_reason_to_status(self._finish_reason)
        response = self._response_skeleton(status)
        # 输出项已按收尾顺序入栈(文本先于工具调用)
        response["output"] = list(self._final_items)
        if incomplete:
            response["incomplete_details"] = incomplete
        usage = _usage_to_responses(self._usage)
        if usage:
            response["usage"] = usage

        event_name = "response.completed" if status == "completed" else "response.incomplete"
        yield _sse(event_name, {
            "type": event_name,
            "sequence_number": self._next_seq(),
            "response": response,
        })
