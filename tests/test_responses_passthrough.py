"""responses 出站全透传测试: 路由、请求体改写、非流式与流式端到端。"""

import textwrap

import httpx
import pytest
from fastapi.testclient import TestClient

from codex_shift.config import OUTBOUND_RESPONSES, load_config
from codex_shift import server, upstream
from codex_shift.server import _build_request_payload, _convert_response


def _cfg(tmp_path, text: str):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return load_config(str(p))


_CONFIG = """
    model_map:
      gpt-alias: gpt-5.5
    providers:
      - name: inner
        outbound: responses
        base_url: https://gw.example.com/v1
        api_key: sk-inner
        models:
          - name: gpt-5.5
            context_window: 1050000
"""


def test_responses_default_path_and_outbound(tmp_path):
    """responses 出站默认路径为 /responses,拼接 base_url 末尾的 /v1。"""
    cfg = _cfg(tmp_path, _CONFIG)
    p = cfg.providers[0]
    assert p.outbound == OUTBOUND_RESPONSES
    assert p.path == "/responses"
    assert upstream.upstream_url(p) == "https://gw.example.com/v1/responses"


def test_passthrough_payload_rewrites_only_model(tmp_path):
    """请求体原样透传,仅按 model_map 改写 model;其余字段保持不变。"""
    cfg = _cfg(tmp_path, _CONFIG)
    provider, mapped = cfg.resolve("gpt-alias")
    body = {
        "model": "gpt-alias",
        "input": "你好",
        "tools": [{"type": "web_search"}],
        "reasoning": {"effort": "high"},
        "stream": False,
    }
    payload = _build_request_payload(provider, body, mapped)
    # model 被改写为映射目标
    assert payload["model"] == "gpt-5.5"
    # 其余字段一字不改
    assert payload["input"] == "你好"
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["reasoning"] == {"effort": "high"}
    # 不影响原始 body(浅拷贝)
    assert body["model"] == "gpt-alias"


def test_passthrough_response_verbatim(tmp_path):
    """上游响应原样回送,不做任何转换。"""
    cfg = _cfg(tmp_path, _CONFIG)
    provider = cfg.providers[0]
    upstream_json = {"id": "resp_1", "object": "response", "output": [{"type": "message"}]}
    out = _convert_response(provider, upstream_json, "gpt-5.5", None)
    assert out is upstream_json


def test_bearer_auth_header(tmp_path):
    """responses 出站使用 OpenAI 风格 Bearer 鉴权(apikey 代理)。"""
    cfg = _cfg(tmp_path, _CONFIG)
    headers = upstream.build_headers(cfg.providers[0])
    assert headers["authorization"] == "Bearer sk-inner"
    assert "anthropic-version" not in headers


def test_nonstream_end_to_end(tmp_path, monkeypatch):
    """端到端: 路由到 responses provider,转发后原样回送上游响应体。"""
    cfg = _cfg(tmp_path, _CONFIG)
    app = server.create_app(cfg)
    client = TestClient(app)

    captured = {}

    async def fake_forward(provider, payload):
        captured["provider"] = provider.name
        captured["url"] = upstream.upstream_url(provider)
        captured["model"] = payload.get("model")
        req = httpx.Request("POST", captured["url"])
        body = {"id": "resp_x", "object": "response", "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}]}
        return httpx.Response(200, json=body, request=req)

    monkeypatch.setattr(server.upstream, "forward_json", fake_forward)
    r = client.post("/v1/responses", json={"model": "gpt-alias", "input": "hi"})
    assert r.status_code == 200
    assert captured["provider"] == "inner"
    assert captured["url"] == "https://gw.example.com/v1/responses"
    # model_map 改写后发往上游的是 gpt-5.5
    assert captured["model"] == "gpt-5.5"
    # 响应体原样回送
    assert r.json()["id"] == "resp_x"
    assert r.json()["output"][0]["content"][0]["text"] == "hi"


def test_stream_passthrough_end_to_end(tmp_path, monkeypatch):
    """流式端到端: 原样转发上游 SSE 文本块(含 [DONE])。"""
    cfg = _cfg(tmp_path, _CONFIG)
    app = server.create_app(cfg)
    client = TestClient(app)

    async def fake_stream_raw(provider, payload):
        # 模拟上游原始 SSE 分块输出
        yield "__chunk__", "event: response.created\ndata: {\"id\":\"r1\"}\n\n"
        yield "__chunk__", "event: response.completed\ndata: {\"id\":\"r1\"}\n\n"
        yield "__chunk__", "data: [DONE]\n\n"

    monkeypatch.setattr(server.upstream, "stream_raw", fake_stream_raw)
    with client.stream("POST", "/v1/responses",
                       json={"model": "gpt-5.5", "input": "hi", "stream": True}) as r:
        assert r.status_code == 200
        text = "".join(r.iter_text())
    # 原始事件与 [DONE] 均被原样透传
    assert "event: response.created" in text
    assert "event: response.completed" in text
    assert "data: [DONE]" in text


def test_responses_model_exposed_in_catalog(tmp_path):
    """context_window 代理仍生效: responses provider 的模型出现在 /models 目录。"""
    cfg = _cfg(tmp_path, _CONFIG)
    client = TestClient(server.create_app(cfg))
    body = client.get("/models").json()
    slugs = {m["slug"] for m in body["models"]}
    assert "gpt-5.5" in slugs
    # 别名也随之暴露
    assert "gpt-alias" in slugs
