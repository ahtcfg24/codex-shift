"""构建 codex 兼容的 /models 目录(基于完整 ModelInfo 模板)。

背景
====
codex(OpenAI Codex CLI)在拉取某个 provider 的 ``GET {base_url}/models`` 时,
期望响应体为 ``{"models": [ModelInfo, ...]}``。codex 会用返回条目的
``context_window`` / ``max_context_window`` 计算可用上下文与自动压缩阈值;
拉取后还会用该 ModelInfo **整体覆盖**对应 slug 的元数据(含系统提示词
``base_instructions`` 与各项工具能力)。

实现策略
========
本模块以一份**完整的 ModelInfo 模板**为基底,深拷贝后仅覆盖 ``slug`` 与上下文相关
字段(``context_window`` 等),从而让自定义模型(deepseek/mimo 等)在 codex 中:
- 获得用户设置的真实上下文长度;
- 同时保留模板自带的完整 ``base_instructions`` 与工具能力,避免系统提示词被清空。

默认模板取自 codex 源码 ``models.json`` 中的 ``gpt-5.5`` 条目(已内置于
``data/model_template.json``),可通过配置 ``defaults.model_template`` 指向自定义文件。

关键约束
========
1. codex 的 ModelInfo 含多个"必填(无 serde 默认值)"字段,缺失会反序列化失败。
   完整模板天然包含全部字段,故无需手工枚举。codex 对未知字段宽容(无
   deny_unknown_fields),模板中多余字段会被忽略。
2. **仅当某模型解析出非空 context_window 时才将其列入目录**;未配置上下文的模型
   不出现,codex 将沿用其自身的 fallback 元数据,避免无谓覆盖。
3. 渲染时清理仅适用于 gpt-5.5 的字段(``availability_nux`` NUX 弹窗、
   ``minimal_client_version`` 版本门槛、``available_in_plans`` 计划门槛),
   避免在自定义模型上产生误导或可见性限制。

注意
====
仅当 codex 侧满足触发条件(provider 配置了 command auth,或使用 ChatGPT 登录态)时,
才会真正请求 /models。否则推荐在 codex ``config.toml`` 中直接设置
``model_context_window``(详见 README)。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from typing import Any

from .config import Config, ModelMeta

# 内置默认模板: 取自 codex models.json 的 gpt-5.5 条目
_BUILTIN_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "data", "model_template.json")

# 模板缓存(按文件路径),避免每次请求重复读盘
_template_cache: dict[str, dict[str, Any]] = {}


def _load_template(cfg: Config) -> dict[str, Any]:
    """加载 ModelInfo 模板(自定义路径优先,否则内置 gpt-5.5),并按路径缓存。"""
    path = cfg.model_template_path or _BUILTIN_TEMPLATE_PATH
    cached = _template_cache.get(path)
    if cached is None:
        with open(path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if not isinstance(cached, dict):
            raise ValueError(f"模型模板 {path!r} 必须是 JSON 对象(单个 ModelInfo)")
        _template_cache[path] = cached
    return cached


def _resolve_context_window(cfg: Config, meta: ModelMeta | None) -> int | None:
    """解析模型的上下文窗口: 模型级 context_window > max_context_window > 全局默认。

    返回 None 表示未配置,调用方据此决定是否将该模型列入目录。
    """
    if meta is not None:
        if meta.context_window is not None:
            return meta.context_window
        if meta.max_context_window is not None:
            return meta.max_context_window
    return cfg.default_context_window


def _render(template: dict[str, Any], cfg: Config, slug: str,
            meta: ModelMeta | None, context_window: int) -> dict[str, Any]:
    """以模板为基底渲染单个模型的 ModelInfo: 覆盖 slug 与上下文字段,清理专属字段。"""
    info = copy.deepcopy(template)

    # 身份与展示
    info["slug"] = slug
    info["display_name"] = (meta.display_name if meta and meta.display_name else slug)
    # 描述默认置空(避免沿用 gpt-5.5 的描述误导),meta 可显式指定
    info["description"] = (meta.description if meta and meta.description is not None else None)

    # 上下文窗口(核心): 用用户设置值覆盖模板
    info["context_window"] = context_window
    info["max_context_window"] = (
        meta.max_context_window if meta and meta.max_context_window is not None else context_window
    )
    if meta and meta.auto_compact_token_limit is not None:
        info["auto_compact_token_limit"] = meta.auto_compact_token_limit
    info["effective_context_window_percent"] = (
        meta.effective_context_window_percent
        if meta and meta.effective_context_window_percent is not None
        else cfg.default_effective_context_window_percent
    )

    # 清理仅适用于 gpt-5.5 的字段,避免误导/门槛
    info["availability_nux"] = None
    info.pop("minimal_client_version", None)
    info.pop("available_in_plans", None)

    # base_instructions: 默认保留模板自带的完整 codex 提示词;配置可覆盖
    override_bi = (meta.base_instructions if meta and meta.base_instructions is not None else None)
    if override_bi is None:
        override_bi = cfg.default_base_instructions
    if override_bi is not None:
        info["base_instructions"] = override_bi
        # 覆盖 base_instructions 时清除 personality 模板,确保覆盖值实际生效
        info["model_messages"] = None

    return info


def _find_meta(cfg: Config, model_name: str) -> ModelMeta | None:
    """在所有 provider 中查找指定入站 model 名的 ModelMeta(旧 model_map 兼容用)。"""
    for p in cfg.active_providers:
        meta = p.model_meta.get(model_name)
        if meta is not None:
            return meta
    return None


def build_models_catalog(cfg: Config) -> dict[str, Any]:
    """构建 codex 兼容的 /models 目录: ``{"models": [ModelInfo, ...]}``。

    收录范围(仅当解析出非空 context_window 时收录):
    1. 各 provider 声明的入站 model 名(使用其自身 ModelMeta);
    2. 旧版顶层 model_map 的别名键,其上下文继承自映射目标模型的 ModelMeta,
       目标无元数据时回退全局默认。

    同名去重: provider 模型优先于别名。
    """
    template = _load_template(cfg)
    entries: dict[str, dict[str, Any]] = {}

    # 1. provider 声明的入站模型
    for p in cfg.active_providers:
        for name in p.models:
            meta = p.model_meta.get(name)
            cw = _resolve_context_window(cfg, meta)
            if cw is None:
                # 未配置上下文窗口的模型不列入,避免覆盖 codex 自带元数据
                continue
            entries[name] = _render(template, cfg, name, meta, cw)

    # 2. 旧版顶层 model_map 别名(兼容旧配置)
    for alias, target in cfg.model_map.items():
        if alias in entries:
            continue
        meta = _find_meta(cfg, target)
        cw = _resolve_context_window(cfg, meta)
        if cw is None:
            continue
        entries[alias] = _render(template, cfg, alias, meta, cw)

    return {"models": list(entries.values())}


def catalog_etag(catalog: dict[str, Any]) -> str:
    """根据目录内容计算稳定的强 ETag(内容不变则 ETag 不变)。

    codex 会缓存该 ETag,内容未变时仅续期本地缓存 TTL,避免无谓刷新。
    """
    payload = json.dumps(catalog, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return '"' + hashlib.sha256(payload).hexdigest()[:32] + '"'
