"""本地配置控制台与热加载测试。"""

import textwrap

import httpx
from fastapi.testclient import TestClient

from codex_shift import server, upstream
from codex_shift.config import load_config


def _write(tmp_path, text: str) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(p)


def test_admin_lists_all_providers_and_html(tmp_path):
    cfg_path = _write(tmp_path, """
        providers:
          - name: a
            outbound: chat_completions
            base_url: https://a.example.com
            weight: 2
            models: [m]
          - name: b
            enabled: false
            outbound: responses
            base_url: https://b.example.com/v1
            models: [m]
    """)
    client = TestClient(server.create_app(load_config(cfg_path), config_path=cfg_path))

    page = client.get("/admin")
    assert page.status_code == 200
    assert "codex-shift 控制台" in page.text
    assert "模型路由表" in page.text
    assert "启用 Provider" in page.text
    assert "renderRoutes" in page.text

    resp = client.get("/admin/api/config")
    assert resp.status_code == 200
    providers = resp.json()["providers"]
    assert [(p["name"], p["enabled"], p["weight"]) for p in providers] == [
        ("a", True, 2.0),
        ("b", False, 1.0),
    ]


def test_admin_save_hot_reloads_routing_without_restart(tmp_path, monkeypatch):
    cfg_path = _write(tmp_path, """
        providers:
          - name: a
            outbound: responses
            base_url: https://a.example.com/v1
            api_key: sk-a
            models: [m]
          - name: b
            enabled: false
            outbound: responses
            base_url: https://b.example.com/v1
            api_key: sk-b
            models: [m]
    """)
    client = TestClient(server.create_app(load_config(cfg_path), config_path=cfg_path))
    seen: list[str] = []

    async def fake_forward(provider, payload):
        seen.append(provider.name)
        req = httpx.Request("POST", upstream.upstream_url(provider))
        return httpx.Response(
            200,
            json={"id": provider.name, "object": "response", "output": []},
            request=req,
        )

    monkeypatch.setattr(server.upstream, "forward_json", fake_forward)

    first = client.post("/v1/responses", json={"model": "m", "input": "hi"})
    assert first.status_code == 200
    assert first.json()["id"] == "a"

    saved = client.post("/admin/api/config", json={
        "providers": [
            {"name": "a", "enabled": False, "weight": 1},
            {"name": "b", "enabled": True, "weight": 1},
        ]
    })
    assert saved.status_code == 200

    second = client.post("/v1/responses", json={"model": "m", "input": "hi"})
    assert second.status_code == 200
    assert second.json()["id"] == "b"
    assert seen == ["a", "b"]

    health = client.get("/health").json()
    assert [p["name"] for p in health["providers"]] == ["b"]


def test_admin_rejects_invalid_weight_without_replacing_config(tmp_path):
    cfg_path = _write(tmp_path, """
        providers:
          - name: a
            outbound: chat_completions
            base_url: https://a.example.com
            models: [m]
    """)
    original = open(cfg_path, encoding="utf-8").read()
    client = TestClient(server.create_app(load_config(cfg_path), config_path=cfg_path))

    resp = client.post("/admin/api/config", json={
        "providers": [{"name": "a", "enabled": True, "weight": -1}]
    })
    assert resp.status_code == 400
    assert open(cfg_path, encoding="utf-8").read() == original
