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


def test_admin_rejects_empty_provider_list_without_replacing_config(tmp_path):
    cfg_path = _write(tmp_path, """
        providers:
          - name: a
            outbound: chat_completions
            base_url: https://a.example.com
            models: [m]
    """)
    original = open(cfg_path, encoding="utf-8").read()
    client = TestClient(server.create_app(load_config(cfg_path), config_path=cfg_path))

    resp = client.post("/admin/api/config", json={"providers": []})
    assert resp.status_code == 400
    assert "至少需要配置一个 provider" in resp.json()["error"]["message"]
    assert open(cfg_path, encoding="utf-8").read() == original


def test_admin_surfaces_legacy_global_model_map_and_removes_it_on_full_save(tmp_path):
    cfg_path = _write(tmp_path, """
        model_map:
          gpt-5.5: deepseek-v4-pro
        providers:
          - name: deepseek
            outbound: chat_completions
            base_url: https://api.deepseek.com
            models:
              - name: deepseek-v4-pro
                context_window: 1000000
    """)
    client = TestClient(server.create_app(load_config(cfg_path), config_path=cfg_path))

    loaded = client.get("/admin/api/config").json()["providers"][0]
    alias_item = next(m for m in loaded["model_items"] if m["name"] == "gpt-5.5")
    assert alias_item["mapped_model"] == "deepseek-v4-pro"
    assert alias_item["context_window"] == 1000000

    saved = client.post("/admin/api/config", json={"providers": [
        {
            "name": "deepseek",
            "enabled": True,
            "weight": 1,
            "outbound": "chat_completions",
            "base_url": "https://api.deepseek.com",
            "model_items": [
                {"name": "gpt-5.5", "mapped_model": "deepseek-v4-pro", "context_window": 1000000}
            ],
        }
    ]})
    assert saved.status_code == 200

    text = open(cfg_path, encoding="utf-8").read()
    assert "model_map:" not in text
    assert "name: gpt-5.5" in text
    assert "mapped_model: deepseek-v4-pro" in text
    cfg = load_config(cfg_path)
    provider, mapped = cfg.resolve("gpt-5.5")
    assert provider.name == "deepseek"
    assert mapped == "deepseek-v4-pro"


def test_admin_can_edit_models_and_add_delete_providers(tmp_path):
    cfg_path = _write(tmp_path, """
        providers:
          - name: old
            outbound: chat_completions
            base_url: https://old.example.com
            models:
              - name: old-model
                context_window: 1000
          - name: keep
            outbound: responses
            base_url: https://keep.example.com/v1
            models:
              - name: shared
                mapped_model: upstream-shared
                context_window: 2000
    """)
    client = TestClient(server.create_app(load_config(cfg_path), config_path=cfg_path))

    loaded = client.get("/admin/api/config").json()["providers"]
    keep = next(p for p in loaded if p["name"] == "keep")
    assert keep["model_items"][0]["mapped_model"] == "upstream-shared"
    keep["model_items"] = [
        {"name": "shared", "mapped_model": "upstream-shared-v2", "context_window": 4096},
        {"name": "new-model", "context_window": 8192},
    ]
    added = {
        "name": "added",
        "enabled": True,
        "weight": 2,
        "outbound": "chat_completions",
        "base_url": "https://added.example.com",
        "path": "/v1/chat/completions",
        "api_key_env": "ADDED_API_KEY",
        "model_items": [
            {"name": "added-model", "mapped_model": "provider-added-model", "context_window": 16384}
        ],
    }

    saved = client.post("/admin/api/config", json={"providers": [keep, added]})
    assert saved.status_code == 200
    providers = saved.json()["providers"]
    assert [p["name"] for p in providers] == ["keep", "added"]

    cfg = load_config(cfg_path)
    assert [p.name for p in cfg.providers] == ["keep", "added"]
    assert cfg.providers[0].model_meta["shared"].context_window == 4096
    assert cfg.providers[0].model_meta["new-model"].context_window == 8192
    assert cfg.providers[0].model_map["shared"] == "upstream-shared-v2"
    assert cfg.providers[0].model_map["new-model"] == "new-model"
    assert cfg.providers[1].model_map["added-model"] == "provider-added-model"
    assert "api_key_env: ADDED_API_KEY" in open(cfg_path, encoding="utf-8").read()
    assert "mapped_model: upstream-shared-v2" in open(cfg_path, encoding="utf-8").read()
    assert "mapped_model: provider-added-model" in open(cfg_path, encoding="utf-8").read()

    catalog = client.get("/models").json()
    contexts = {m["slug"]: m["context_window"] for m in catalog["models"]}
    assert contexts == {
        "shared": 4096,
        "new-model": 8192,
        "added-model": 16384,
    }

    health = client.get("/health").json()
    assert [p["name"] for p in health["providers"]] == ["keep", "added"]
