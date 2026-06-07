"""Responses -> Chat Completions 请求转换测试。"""

from codex_shift.convert.to_chat import responses_to_chat


def test_string_input_and_instructions():
    req = {"model": "gpt-4o", "instructions": "Be brief.", "input": "Hello"}
    out = responses_to_chat(req, model="gpt-4o")
    assert out["model"] == "gpt-4o"
    assert out["messages"][0] == {"role": "system", "content": "Be brief."}
    assert out["messages"][1] == {"role": "user", "content": "Hello"}


def test_developer_role_mapped_to_system():
    # DeepSeek 等兼容后端不接受 developer 角色,应降级为 system
    req = {"input": [
        {"type": "message", "role": "developer", "content": "Dev system rule"},
        {"type": "message", "role": "user", "content": "hi"},
    ]}
    out = responses_to_chat(req, model="m")
    assert out["messages"][0] == {"role": "system", "content": "Dev system rule"}
    assert out["messages"][1]["role"] == "user"


def test_max_output_tokens_and_sampling():
    req = {"input": "hi", "max_output_tokens": 100, "temperature": 0.5, "top_p": 0.9}
    out = responses_to_chat(req, model="m")
    assert out["max_completion_tokens"] == 100
    assert out["temperature"] == 0.5
    assert out["top_p"] == 0.9


def test_input_parts_text_and_image():
    req = {"input": [{
        "type": "message", "role": "user",
        "content": [
            {"type": "input_text", "text": "what is this"},
            {"type": "input_image", "image_url": "http://x/y.png", "detail": "high"},
        ],
    }]}
    out = responses_to_chat(req, model="m")
    content = out["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "what is this"}
    assert content[1] == {"type": "image_url", "image_url": {"url": "http://x/y.png", "detail": "high"}}


def test_function_call_and_output():
    req = {"input": [
        {"type": "function_call", "call_id": "c1", "name": "get_weather", "arguments": "{\"city\":\"SF\"}"},
        {"type": "function_call_output", "call_id": "c1", "output": "sunny"},
    ]}
    out = responses_to_chat(req, model="m")
    assistant = out["messages"][0]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["id"] == "c1"
    assert assistant["tool_calls"][0]["function"]["name"] == "get_weather"
    tool_msg = out["messages"][1]
    assert tool_msg == {"role": "tool", "tool_call_id": "c1", "content": "sunny"}


def test_tools_and_tool_choice():
    req = {
        "input": "hi",
        "tools": [{"type": "function", "name": "f", "description": "d", "parameters": {"type": "object"}}],
        "tool_choice": {"type": "function", "name": "f"},
    }
    out = responses_to_chat(req, model="m")
    assert out["tools"][0] == {"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}
    assert out["tool_choice"] == {"type": "function", "function": {"name": "f"}}


def test_text_format_json_schema():
    req = {"input": "hi", "text": {"format": {"type": "json_schema", "name": "S", "schema": {"type": "object"}, "strict": True}}}
    out = responses_to_chat(req, model="m")
    assert out["response_format"]["type"] == "json_schema"
    assert out["response_format"]["json_schema"]["name"] == "S"
    assert out["response_format"]["json_schema"]["strict"] is True


def test_reasoning_effort_mapped():
    # Responses 嵌套 reasoning.effort 应映射为 Chat 顶层 reasoning_effort
    req = {"input": "hi", "reasoning": {"effort": "high"}}
    out = responses_to_chat(req, model="m")
    assert out["reasoning_effort"] == "high"
    # 原始嵌套 reasoning 对象不应透传到出站请求
    assert "reasoning" not in out


def test_reasoning_without_effort_is_dropped():
    # reasoning 存在但无 effort 字段时,不应产生 reasoning_effort
    req = {"input": "hi", "reasoning": {"summary": "auto"}}
    out = responses_to_chat(req, model="m")
    assert "reasoning_effort" not in out
    assert "reasoning" not in out


def test_stream_adds_usage_option():
    out = responses_to_chat({"input": "hi", "stream": True}, model="m")
    assert out["stream"] is True
    assert out["stream_options"] == {"include_usage": True}
