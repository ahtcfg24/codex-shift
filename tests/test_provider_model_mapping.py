"""Provider 内部模型映射测试。"""

import textwrap

import httpx
from fastapi.testclient import TestClient

from codex_shift import server, upstream
from codex_shift.config import load_config


def _cfg(tmp_path, text: str):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return load_config(str(p))


def test_chat_provider_maps_inbound_model_to_provider_outbound_model(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, """
        providers:
          - name: deepseek
            outbound: chat_completions
            base_url: https://api.deepseek.com
            api_key: sk-deepseek
            models:
              - name: gpt-5.5
                mapped_model: deepseek-v4-pro
                context_window: 1000000
    """)
    client = TestClient(server.create_app(cfg))
    captured: dict[str, str] = {}

    async def fake_forward(provider, payload):
        captured["provider"] = provider.name
        captured["url"] = upstream.upstream_url(provider)
        captured["model"] = payload.get("model")
        req = httpx.Request("POST", captured["url"])
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_1",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
            request=req,
        )

    monkeypatch.setattr(server.upstream, "forward_json", fake_forward)
    resp = client.post("/v1/responses", json={"model": "gpt-5.5", "input": "hi"})

    assert resp.status_code == 200
    assert captured["provider"] == "deepseek"
    assert captured["model"] == "deepseek-v4-pro"


def test_models_catalog_exposes_inbound_model_not_provider_outbound_model(tmp_path):
    cfg = _cfg(tmp_path, """
        providers:
          - name: deepseek
            outbound: chat_completions
            base_url: https://api.deepseek.com
            models:
              - name: gpt-5.5
                mapped_model: deepseek-v4-pro
                context_window: 1000000
    """)
    client = TestClient(server.create_app(cfg))

    slugs = {m["slug"] for m in client.get("/models").json()["models"]}
    assert slugs == {"gpt-5.5"}
