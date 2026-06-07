"""/models 与 /v1/models HTTP 端点测试。"""

import textwrap

import pytest
from fastapi.testclient import TestClient

from codex_shift.config import load_config
from codex_shift.server import create_app

_CONFIG = """
    model_map:
      gpt-5.5: deepseek-v4-pro
    providers:
      - name: deepseek
        outbound: chat_completions
        base_url: https://api.deepseek.com
        models:
          - name: deepseek-v4-pro
            context_window: 131072
          - plain-no-context
"""


@pytest.fixture()
def client(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(_CONFIG), encoding="utf-8")
    return TestClient(create_app(load_config(str(p))))


@pytest.mark.parametrize("path", ["/models", "/v1/models"])
def test_models_endpoint(client, path):
    """两个挂载路径均返回 codex 目录结构,带 ETag。"""
    resp = client.get(path)
    assert resp.status_code == 200
    assert "etag" in resp.headers
    body = resp.json()
    slugs = {m["slug"] for m in body["models"]}
    # 已配置上下文的模型与别名在目录中;无上下文的模型被跳过
    assert slugs == {"deepseek-v4-pro", "gpt-5.5"}
    assert "plain-no-context" not in slugs


def test_models_etag_304(client):
    """携带匹配的 If-None-Match 时返回 304。"""
    first = client.get("/models")
    etag = first.headers["etag"]
    again = client.get("/models", headers={"If-None-Match": etag})
    assert again.status_code == 304


def test_middleware_logs_full_path(client, caplog):
    """中间件为每个入站请求记录含 query 的完整 path 与响应状态。"""
    import logging

    with caplog.at_level(logging.INFO, logger="codex_shift.server"):
        client.get("/v1/models?client_version=9.9.9")
    msgs = [r.getMessage() for r in caplog.records]
    assert any("入站 GET /v1/models?client_version=9.9.9" in m for m in msgs)
    assert any("完成 GET /v1/models?client_version=9.9.9 -> 200" in m for m in msgs)
