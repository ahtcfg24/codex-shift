"""namespace 工具展平与回程拆名的端到端测试。

模拟 Codex 发送的 namespace 工具,验证:
1. 出站请求把命名空间展平为组合名的 function 工具;
2. 上游返回的 tool_call 组合名能拆回 name(裸) + namespace(独立字段)。
"""

import json

from codex_shift.convert import common
from codex_shift.convert.from_chat import ChatStreamConverter, chat_to_responses
from codex_shift.convert.to_chat import responses_to_chat

# Codex 风格的 namespace 工具
NS_TOOLS = [
    {"type": "function", "name": "shell", "description": "run", "parameters": {"type": "object"}},
    {"type": "namespace", "name": "mcp__codex_apps__calendar", "description": "Plan events",
     "tools": [
         {"type": "function", "name": "create_event", "description": "Create.", "parameters": {"type": "object"}},
     ]},
]


def test_flatten_function_tools():
    funcs, name_map, dropped, web_search = common.flatten_function_tools(NS_TOOLS)
    names = [f["name"] for f in funcs]
    assert "shell" in names
    assert "mcp__codex_apps__calendar__create_event" in names
    assert name_map["mcp__codex_apps__calendar__create_event"] == {
        "namespace": "mcp__codex_apps__calendar", "name": "create_event"}
    assert dropped == []


def test_flatten_drops_unknown_types():
    funcs, _, dropped, web_search = common.flatten_function_tools([{"type": "web_search"}])
    assert funcs == []
    assert dropped == []
    assert len(web_search) == 1

    # file_search 仍然被丢弃
    funcs2, _, dropped2, web_search2 = common.flatten_function_tools([{"type": "file_search"}])
    assert funcs2 == []
    assert dropped2 == ["file_search"]
    assert web_search2 == []


def test_namespaced_child_name_special_cases():
    assert common.namespaced_child_name("ns", "tool") == "ns__tool"
    # 命名空间以 _ 结尾时不再加分隔符
    assert common.namespaced_child_name("ns_", "tool") == "ns_tool"
    # 子名以 _ 开头时不再加分隔符
    assert common.namespaced_child_name("ns", "_tool") == "ns_tool"


def test_to_chat_flattens_namespace():
    out = responses_to_chat({"input": "hi", "tools": NS_TOOLS}, model="m")
    tool_names = [t["function"]["name"] for t in out["tools"]]
    assert tool_names == ["shell", "mcp__codex_apps__calendar__create_event"]


def test_to_chat_combines_history_function_call_namespace():
    # 历史 function_call 项带独立 namespace 字段,应组合回扁平名
    req = {"input": [
        {"type": "function_call", "call_id": "c1", "name": "create_event",
         "namespace": "mcp__codex_apps__calendar", "arguments": "{}"},
    ]}
    out = responses_to_chat(req, model="m")
    assert out["messages"][0]["tool_calls"][0]["function"]["name"] == "mcp__codex_apps__calendar__create_event"


def test_from_chat_splits_namespace_nonstream():
    _, name_map, _, _ = common.flatten_function_tools(NS_TOOLS)
    chat = {"choices": [{"message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "mcp__codex_apps__calendar__create_event",
                                         "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}]}
    out = chat_to_responses(chat, name_map=name_map)
    fc = out["output"][0]
    assert fc["name"] == "create_event"
    assert fc["namespace"] == "mcp__codex_apps__calendar"


def test_from_chat_splits_namespace_stream():
    _, name_map, _, _ = common.flatten_function_tools(NS_TOOLS)
    conv = ChatStreamConverter(request_model="m", name_map=name_map)
    raw = []
    chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1",
            "function": {"name": "mcp__codex_apps__calendar__create_event", "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    for ch in chunks:
        raw.extend(conv.feed(ch))
    raw.extend(conv.finish())
    events = [(b.strip().split("\n")[0][7:], json.loads(b.strip().split("\n")[1][6:])) for b in raw]
    final = events[-1][1]["response"]["output"][0]
    assert final["name"] == "create_event"
    assert final["namespace"] == "mcp__codex_apps__calendar"


def test_to_chat_web_search_supported():
    """web_search 工具在 supports_web_search=True 时透传。"""
    tools = [
        {"type": "function", "name": "shell", "description": "run", "parameters": {"type": "object"}},
        {"type": "web_search", "max_keyword": 3, "force_search": True},
    ]
    out = responses_to_chat({"input": "hi", "tools": tools}, model="m", supports_web_search=True)
    types = [t.get("type") for t in out["tools"]]
    assert "function" in types
    assert "web_search" in types
    # web_search 工具保留原始参数
    ws = [t for t in out["tools"] if t.get("type") == "web_search"][0]
    assert ws["max_keyword"] == 3
    assert ws["force_search"] is True


def test_to_chat_web_search_unsupported():
    """web_search 工具在 supports_web_search=False 时被丢弃。"""
    tools = [
        {"type": "function", "name": "shell", "description": "run", "parameters": {"type": "object"}},
        {"type": "web_search", "max_keyword": 3},
    ]
    out = responses_to_chat({"input": "hi", "tools": tools}, model="m", supports_web_search=False)
    types = [t.get("type") for t in out["tools"]]
    assert "function" in types
    assert "web_search" not in types


def test_to_chat_web_search_no_enabled_flag():
    """web_search 支持但未开启 web_search_enabled 时,透传工具但不附加 webSearchEnabled
    (标准端点 api.xiaomimimo.com 不识别此字段)。"""
    tools = [
        {"type": "function", "name": "shell", "description": "run", "parameters": {"type": "object"}},
        {"type": "web_search", "max_keyword": 3, "force_search": True},
    ]
    out = responses_to_chat({"input": "hi", "tools": tools}, model="m",
                            supports_web_search=True, web_search_enabled=False)
    # 默认不设置 webSearchEnabled
    assert "webSearchEnabled" not in out
    # 应保留 web_search 工具及其原始参数
    types = [t.get("type") for t in out["tools"]]
    assert "web_search" in types
    ws = [t for t in out["tools"] if t.get("type") == "web_search"][0]
    assert ws["max_keyword"] == 3
    assert ws["force_search"] is True


def test_to_chat_web_search_with_enabled_flag():
    """web_search_enabled=True 且请求带 web_search 工具时,顶层附加 webSearchEnabled=true
    (token-plan 等端点强制要求,否则上游返回 400)。"""
    tools = [
        {"type": "function", "name": "shell", "description": "run", "parameters": {"type": "object"}},
        {"type": "web_search", "max_keyword": 3, "force_search": True},
    ]
    out = responses_to_chat({"input": "hi", "tools": tools}, model="m",
                            supports_web_search=True, web_search_enabled=True)
    assert out.get("webSearchEnabled") is True
    types = [t.get("type") for t in out["tools"]]
    assert "web_search" in types


def test_to_chat_enabled_flag_without_web_search_tool():
    """即使 web_search_enabled=True,请求未携带 web_search 工具时也不应附加 webSearchEnabled。"""
    tools = [
        {"type": "function", "name": "shell", "description": "run", "parameters": {"type": "object"}},
    ]
    out = responses_to_chat({"input": "hi", "tools": tools}, model="m",
                            supports_web_search=True, web_search_enabled=True)
    assert "webSearchEnabled" not in out


def test_to_chat_no_web_search_no_webSearchEnabled():
    """无 web_search 工具时不应设置 webSearchEnabled。"""
    tools = [
        {"type": "function", "name": "shell", "description": "run", "parameters": {"type": "object"}},
    ]
    out = responses_to_chat({"input": "hi", "tools": tools}, model="m", supports_web_search=True)
    assert "webSearchEnabled" not in out
