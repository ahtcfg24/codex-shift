"""/models 目录构建与 model 元数据解析测试。"""

import textwrap

from codex_shift.config import load_config
from codex_shift.models_catalog import build_models_catalog, catalog_etag

# codex ModelInfo 反序列化要求存在(无 serde 默认值)的字段;缺失会导致 codex 解析失败。
_REQUIRED_FIELDS = [
    "slug", "display_name", "description", "supported_reasoning_levels",
    "shell_type", "visibility", "supported_in_api", "priority",
    "availability_nux", "upgrade", "base_instructions",
    "supports_reasoning_summaries", "support_verbosity", "default_verbosity",
    "apply_patch_tool_type", "truncation_policy", "supports_parallel_tool_calls",
    "experimental_supported_tools",
]


def _write(tmp_path, text: str) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(p)


def test_object_model_entry_parsed(tmp_path):
    """models 列表中对象写法被解析为 ModelMeta,字符串写法无元数据。"""
    cfg = load_config(_write(tmp_path, """
        providers:
          - name: deepseek
            outbound: chat_completions
            base_url: https://api.deepseek.com
            models:
              - deepseek-v4-flash
              - name: deepseek-v4-pro
                context_window: 131072
                max_context_window: 131072
                auto_compact_token_limit: 117964
    """))
    p = cfg.providers[0]
    # 两种写法都进入路由 models 列表
    assert p.models == ["deepseek-v4-flash", "deepseek-v4-pro"]
    # 仅对象写法生成 ModelMeta
    assert "deepseek-v4-flash" not in p.model_meta
    meta = p.model_meta["deepseek-v4-pro"]
    assert meta.context_window == 131072
    assert meta.auto_compact_token_limit == 117964


def test_catalog_includes_required_fields_and_context(tmp_path):
    """目录条目含全部 codex 必填字段,且 context_window 取配置值。"""
    cfg = load_config(_write(tmp_path, """
        providers:
          - name: deepseek
            outbound: chat_completions
            base_url: https://api.deepseek.com
            models:
              - name: deepseek-v4-pro
                context_window: 131072
    """))
    catalog = build_models_catalog(cfg)
    assert set(catalog.keys()) == {"models"}
    assert len(catalog["models"]) == 1
    info = catalog["models"][0]
    for field in _REQUIRED_FIELDS:
        assert field in info, f"缺少必填字段 {field}"
    assert info["slug"] == "deepseek-v4-pro"
    assert info["context_window"] == 131072
    # max_context_window 未单独配置时回退为 context_window
    assert info["max_context_window"] == 131072
    assert info["effective_context_window_percent"] == 95
    # 默认保留模板(gpt-5.5)自带的完整 base_instructions(非空)
    assert isinstance(info["base_instructions"], str)
    assert len(info["base_instructions"]) > 100
    # 模板专属字段在自定义模型上被清理
    assert info["availability_nux"] is None
    assert "minimal_client_version" not in info
    assert "available_in_plans" not in info


def test_models_without_context_are_skipped(tmp_path):
    """未配置上下文窗口且无全局默认的模型不出现在目录中。"""
    cfg = load_config(_write(tmp_path, """
        providers:
          - name: deepseek
            outbound: chat_completions
            base_url: https://api.deepseek.com
            models:
              - plain-model
              - name: sized-model
                context_window: 65536
    """))
    slugs = [m["slug"] for m in build_models_catalog(cfg)["models"]]
    assert slugs == ["sized-model"]


def test_disabled_provider_models_are_skipped(tmp_path):
    """enabled=false 的 provider 不参与 /models 目录。"""
    cfg = load_config(_write(tmp_path, """
        model_map:
          alias-off: off-model
          alias-on: on-model
        providers:
          - name: off
            enabled: false
            outbound: chat_completions
            base_url: https://off.example.com
            models:
              - name: off-model
                context_window: 1000
          - name: on
            outbound: chat_completions
            base_url: https://on.example.com
            models:
              - name: on-model
                context_window: 2000
    """))
    slugs = {m["slug"] for m in build_models_catalog(cfg)["models"]}
    assert slugs == {"on-model", "alias-on"}


def test_global_default_context_window(tmp_path):
    """全局 defaults.context_window 对未单独配置的模型生效。"""
    cfg = load_config(_write(tmp_path, """
        defaults:
          context_window: 200000
          effective_context_window_percent: 90
        providers:
          - name: deepseek
            outbound: chat_completions
            base_url: https://api.deepseek.com
            models: [a, b]
    """))
    catalog = build_models_catalog(cfg)
    info = {m["slug"]: m for m in catalog["models"]}
    assert set(info) == {"a", "b"}
    assert info["a"]["context_window"] == 200000
    assert info["a"]["effective_context_window_percent"] == 90


def test_model_map_alias_inherits_target_context(tmp_path):
    """model_map 别名继承映射目标模型的上下文窗口。"""
    cfg = load_config(_write(tmp_path, """
        model_map:
          gpt-5.5: deepseek-v4-pro
        providers:
          - name: deepseek
            outbound: chat_completions
            base_url: https://api.deepseek.com
            models:
              - name: deepseek-v4-pro
                context_window: 131072
    """))
    info = {m["slug"]: m for m in build_models_catalog(cfg)["models"]}
    # provider 模型与别名都在目录中,且别名继承上下文
    assert info["deepseek-v4-pro"]["context_window"] == 131072
    assert info["gpt-5.5"]["context_window"] == 131072


def test_base_instructions_override(tmp_path):
    """模型级与全局 base_instructions 可配置,默认空串。"""
    cfg = load_config(_write(tmp_path, """
        defaults:
          base_instructions: "global prompt"
        providers:
          - name: deepseek
            outbound: chat_completions
            base_url: https://api.deepseek.com
            models:
              - name: a
                context_window: 1000
              - name: b
                context_window: 1000
                base_instructions: "model b prompt"
    """))
    info = {m["slug"]: m for m in build_models_catalog(cfg)["models"]}
    assert info["a"]["base_instructions"] == "global prompt"
    assert info["b"]["base_instructions"] == "model b prompt"


def test_catalog_etag_stable_and_content_sensitive(tmp_path):
    """ETag 对相同内容稳定,内容变化时改变。"""
    cfg = load_config(_write(tmp_path, """
        providers:
          - name: deepseek
            outbound: chat_completions
            base_url: https://api.deepseek.com
            models:
              - name: a
                context_window: 1000
    """))
    catalog = build_models_catalog(cfg)
    etag1 = catalog_etag(catalog)
    etag2 = catalog_etag(build_models_catalog(cfg))
    assert etag1 == etag2
    # 修改内容后 ETag 变化
    catalog["models"][0]["context_window"] = 2000
    assert catalog_etag(catalog) != etag1
