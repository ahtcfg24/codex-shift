"""Chat Completions -> Responses 响应转换测试(非流式与流式)。"""

import json

from codex_shift.convert.from_chat import ChatStreamConverter, chat_to_responses


def test_basic_message():
    chat = {
        "model": "gpt-4o", "created": 100,
        "choices": [{"message": {"role": "assistant", "content": "Hi there"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    out = chat_to_responses(chat)
    assert out["status"] == "completed"
    assert out["model"] == "gpt-4o"
    msg = out["output"][0]
    assert msg["type"] == "message" and msg["role"] == "assistant"
    assert msg["content"][0] == {"type": "output_text", "text": "Hi there", "annotations": []}
    assert out["usage"] == {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}
    assert out["output_text"] == "Hi there"


def test_tool_call_output():
    chat = {"choices": [{
        "message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "f", "arguments": "{\"x\":1}"}}]},
        "finish_reason": "tool_calls",
    }]}
    out = chat_to_responses(chat)
    fc = out["output"][0]
    assert fc["type"] == "function_call"
    assert fc["call_id"] == "c1" and fc["name"] == "f" and fc["arguments"] == "{\"x\":1}"


def test_length_finish_is_incomplete():
    chat = {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]}
    out = chat_to_responses(chat)
    assert out["status"] == "incomplete"
    assert out["incomplete_details"] == {"reason": "max_output_tokens"}


def _drive(chunks):
    """驱动流式转换器,返回解析后的 (event_name, data) 列表。"""
    conv = ChatStreamConverter(request_model="m")
    raw = []
    for ch in chunks:
        raw.extend(conv.feed(ch))
    raw.extend(conv.finish())
    events = []
    for block in raw:
        lines = block.strip().split("\n")
        ev = lines[0][len("event: "):]
        data = json.loads(lines[1][len("data: "):])
        events.append((ev, data))
    return events


def test_stream_text():
    chunks = [
        {"model": "m", "choices": [{"delta": {"role": "assistant"}}]},
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}, "choices": []},
    ]
    events = _drive(chunks)
    names = [e[0] for e in events]
    assert names[0] == "response.created"
    assert "response.output_item.added" in names
    assert "response.output_text.delta" in names
    deltas = [d["delta"] for n, d in events if n == "response.output_text.delta"]
    assert "".join(deltas) == "Hello"
    last = events[-1]
    assert last[0] == "response.completed"
    assert last[1]["response"]["output"][0]["content"][0]["text"] == "Hello"
    assert last[1]["response"]["usage"]["output_tokens"] == 2


def test_stream_tool_call():
    chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "f", "arguments": "{\"a\""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ":1}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events = _drive(chunks)
    names = [e[0] for e in events]
    assert "response.function_call_arguments.delta" in names
    arg_done = [d for n, d in events if n == "response.function_call_arguments.done"][0]
    assert arg_done["arguments"] == "{\"a\":1}"
    final = events[-1][1]["response"]["output"][0]
    assert final["type"] == "function_call" and final["call_id"] == "c1"
