"""多 provider 配置加载、路由与校验测试。"""

import textwrap
from unittest.mock import patch

import pytest

from codex_shift.config import load_config


def _write(tmp_path, text: str) -> str:
    """写入临时 config.yaml 并返回路径。"""
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(p)


def test_multi_provider_routing(tmp_path):
    """入站 model 经 model_map 映射后路由到对应 provider。"""
    cfg = _write(tmp_path, """
        model_map:
          gpt-5.5: deepseek-v4-pro
          gpt-5.4-mini: deepseek-v4-pro
        providers:
          - name: deepseek
            outbound: chat_completions
            base_url: https://api.deepseek.com
            models: [deepseek-v4-pro, deepseek-v4-flash]
          - name: glm
            outbound: responses
            base_url: https://responses.example.com/v1
            models: [glm-4.6]
    """)
    c = load_config(cfg)
    assert len(c.providers) == 2

    # 两个映射到 deepseek-v4-pro 的入站名都路由到 deepseek
    p1, m1 = c.resolve("gpt-5.5")
    p2, m2 = c.resolve("gpt-5.4-mini")
    assert p1.name == "deepseek" and m1 == "deepseek-v4-pro"
    assert p2.name == "deepseek" and m2 == "deepseek-v4-pro"

    # 未走映射但直接命中 models 的入站名
    p3, m3 = c.resolve("glm-4.6")
    assert p3.name == "glm" and m3 == "glm-4.6"

    # 出站协议为 provider 级
    assert p1.outbound == "chat_completions"
    assert p3.outbound == "responses"


def test_provider_scoped_model_mapping_routes_by_inbound_model(tmp_path):
    """models[].name 是入站路由键,mapped_model 是该 provider 的出站模型名。"""
    cfg = _write(tmp_path, """
        providers:
          - name: deepseek
            outbound: chat_completions
            base_url: https://api.deepseek.com
            models:
              - name: gpt-5.5
                mapped_model: deepseek-v4-pro
                context_window: 1000000
          - name: mimo
            outbound: chat_completions
            base_url: https://api.mimo.example.com
            weight: 0
            models:
              - name: gpt-5.5
                mapped_model: mimo-v2.5-pro
                context_window: 1000000
    """)
    c = load_config(cfg)
    assert c.providers[0].models == ["gpt-5.5"]
    assert c.providers[0].model_map["gpt-5.5"] == "deepseek-v4-pro"
    assert c.providers[1].model_map["gpt-5.5"] == "mimo-v2.5-pro"
    assert [p.name for p in c._model_index["gpt-5.5"]] == ["deepseek", "mimo"]

    provider, mapped = c.resolve("gpt-5.5")
    assert provider.name == "deepseek"
    assert mapped == "deepseek-v4-pro"


def test_unroutable_model_returns_none(tmp_path):
    """无匹配 provider 时返回 (None, 映射后名)。"""
    cfg = _write(tmp_path, """
        providers:
          - name: deepseek
            outbound: chat_completions
            base_url: https://api.deepseek.com
            models: [deepseek-v4-pro]
    """)
    c = load_config(cfg)
    provider, mapped = c.resolve("unknown-model")
    assert provider is None
    assert mapped == "unknown-model"


def test_duplicate_model_across_providers_weighted_routing(tmp_path):
    """同一 model 可由多个 provider 提供,按权重选择候选。"""
    cfg = _write(tmp_path, """
        providers:
          - name: a
            outbound: chat_completions
            base_url: https://a.example.com
            weight: 1
            models: [shared-model]
          - name: b
            outbound: responses
            base_url: https://b.example.com
            weight: 3
            models: [shared-model]
    """)
    c = load_config(cfg)
    assert [p.name for p in c._model_index["shared-model"]] == ["a", "b"]

    with patch("codex_shift.config.random.uniform", return_value=0.5):
        provider, mapped = c.resolve("shared-model")
    assert provider.name == "a"
    assert mapped == "shared-model"

    with patch("codex_shift.config.random.uniform", return_value=2.5):
        provider, mapped = c.resolve("shared-model")
    assert provider.name == "b"
    assert mapped == "shared-model"


def test_zero_weight_provider_is_not_selected_when_positive_exists(tmp_path):
    """存在正权重候选时,weight=0 的 provider 不参与加权抽选。"""
    cfg = _write(tmp_path, """
        providers:
          - name: zero
            outbound: chat_completions
            base_url: https://zero.example.com
            weight: 0
            models: [shared-model]
          - name: positive
            outbound: responses
            base_url: https://positive.example.com
            weight: 1
            models: [shared-model]
    """)
    c = load_config(cfg)
    with patch("codex_shift.config.random.uniform", return_value=0):
        provider, mapped = c.resolve("shared-model")
    assert provider.name == "positive"
    assert mapped == "shared-model"


def test_disabled_provider_ignored_for_routing(tmp_path):
    """enabled=false 的 provider 不参与路由,等价于未配置。"""
    cfg = _write(tmp_path, """
        providers:
          - name: off
            enabled: false
            outbound: chat_completions
            base_url: https://off.example.com
            models: [m]
          - name: enabled-provider
            outbound: responses
            base_url: https://on.example.com
            models: [m]
    """)
    c = load_config(cfg)
    assert len(c.providers) == 2
    assert [p.name for p in c.active_providers] == ["enabled-provider"]
    provider, mapped = c.resolve("m")
    assert provider.name == "enabled-provider"
    assert mapped == "m"


def test_all_matching_providers_disabled_unroutable(tmp_path):
    """目标 model 仅存在于禁用 provider 时不可路由。"""
    cfg = _write(tmp_path, """
        providers:
          - name: off
            enabled: false
            outbound: chat_completions
            base_url: https://off.example.com
            models: [m]
    """)
    c = load_config(cfg)
    provider, mapped = c.resolve("m")
    assert provider is None
    assert mapped == "m"


def test_multi_provider_requires_models(tmp_path):
    """多 provider 时缺少 models 的 provider 应报错。"""
    cfg = _write(tmp_path, """
        providers:
          - name: a
            outbound: chat_completions
            base_url: https://a.example.com
            models: [model-a]
          - name: b
            outbound: responses
            base_url: https://b.example.com
    """)
    with pytest.raises(ValueError, match="必须声明非空 models"):
        load_config(cfg)


def test_disabled_catch_all_allowed_with_other_active_providers(tmp_path):
    """禁用的空 models provider 不触发多 provider 兜底限制。"""
    cfg = _write(tmp_path, """
        providers:
          - name: active
            outbound: chat_completions
            base_url: https://a.example.com
            models: [model-a]
          - name: disabled-catch-all
            enabled: false
            outbound: responses
            base_url: https://b.example.com
    """)
    c = load_config(cfg)
    provider, mapped = c.resolve("model-a")
    assert provider.name == "active"
    assert mapped == "model-a"


def test_single_provider_catch_all(tmp_path):
    """单 provider 且 models 为空时作为兜底,匹配任意 model。"""
    cfg = _write(tmp_path, """
        providers:
          - name: only
            outbound: chat_completions
            base_url: https://only.example.com
    """)
    c = load_config(cfg)
    provider, mapped = c.resolve("anything")
    assert provider.name == "only"
    assert provider.catch_all is True
    assert mapped == "anything"


def test_legacy_single_upstream_compat(tmp_path):
    """旧版 outbound + upstream 配置合成单个兜底 provider。"""
    cfg = _write(tmp_path, """
        outbound: chat_completions
        upstream:
          base_url: https://api.deepseek.com
          api_key: sk-legacy
        model_map:
          gpt-4o: deepseek-chat
    """)
    c = load_config(cfg)
    assert len(c.providers) == 1
    provider, mapped = c.resolve("gpt-4o")
    assert provider.name == "default"
    assert provider.outbound == "chat_completions"
    assert provider.api_key == "sk-legacy"
    # 兜底 provider 仍按 model_map 映射出站名
    assert mapped == "deepseek-chat"


def test_invalid_outbound_rejected(tmp_path):
    """非法 outbound 应报错。"""
    cfg = _write(tmp_path, """
        providers:
          - name: a
            outbound: bogus
            base_url: https://a.example.com
            models: [m]
    """)
    with pytest.raises(ValueError, match="outbound 必须为"):
        load_config(cfg)


def test_provider_level_options_override_global(tmp_path):
    """provider 级选项覆盖全局 options/defaults。"""
    cfg = _write(tmp_path, """
        options:
          passthrough_unknown: true
        providers:
          - name: a
            outbound: chat_completions
            base_url: https://a.example.com
            models: [m1]
            passthrough_unknown: false
          - name: b
            outbound: chat_completions
            base_url: https://b.example.com
            models: [m2]
    """)
    c = load_config(cfg)
    a = c._model_index["m1"][0]
    b = c._model_index["m2"][0]
    assert a.passthrough_unknown is False
    assert b.passthrough_unknown is True


def test_messages_outbound_rejected(tmp_path):
    """Anthropic Messages 出站协议不再支持。"""
    cfg = _write(tmp_path, """
        providers:
          - name: claude
            outbound: messages
            base_url: https://gateway.example.com
            models: [claude-opus-4-8]
    """)
    with pytest.raises(ValueError, match="outbound 必须为"):
        load_config(cfg)
