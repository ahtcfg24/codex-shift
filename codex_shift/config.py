"""配置加载与校验。

从 YAML 文件读取配置,补全默认值,并对关键字段做合法性校验。

配置模型:
- 支持配置多个 provider(如 deepseek / mimo / qwen / glm 同时配置)。
- 每个 provider 拥有独立的出站协议(outbound)、上游地址、鉴权、超时及其支持的入站 model 列表。
- 每个 provider 可为单个入站 model 配置 mapped_model,用于发送给该 provider 的出站模型名;
  同一入站模型可由多个启用 provider 提供,请求时按 provider 权重选择。
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from typing import Any

import yaml

# 支持的出站协议
OUTBOUND_CHAT = "chat_completions"
# responses: 上游同为 OpenAI Responses 协议,代理仅做全透传(注入 api_key)+ /models 上下文代理,
# 不做任何请求/响应体协议转换。
OUTBOUND_RESPONSES = "responses"
_VALID_OUTBOUND = {OUTBOUND_CHAT, OUTBOUND_RESPONSES}

# 各出站协议的默认请求路径。
# responses 默认 /responses: 上游多以 base_url 末尾携带 /v1(如 .../v1)拼出 .../v1/responses;
# 若上游 base_url 为根地址,可在 provider 配 path: /v1/responses 覆盖。
_DEFAULT_PATH = {
    OUTBOUND_CHAT: "/v1/chat/completions",
    OUTBOUND_RESPONSES: "/responses",
}


@dataclass
class ModelMeta:
    """单个 model 的元数据(用于 /models 端点向 codex 等客户端暴露上下文窗口等信息)。

    这些字段仅影响 /models 目录的输出,不影响请求路由与协议转换。
    未显式配置的字段为 None,由 /models 构建时回退到全局默认值。
    """

    name: str
    # 模型上下文窗口(token 数);codex 据此计算可用上下文与自动压缩阈值
    context_window: int | None = None
    # 上下文窗口上限(用于 config 覆盖的封顶);未配置时与 context_window 一致
    max_context_window: int | None = None
    # 触发自动压缩的 token 阈值;未配置时 codex 默认取上下文窗口的 90%
    auto_compact_token_limit: int | None = None
    # 可用于输入的上下文百分比(codex 默认 95);None 表示回退全局默认
    effective_context_window_percent: int | None = None
    # 展示名/描述;未配置时展示名回退为 slug 本身
    display_name: str | None = None
    description: str | None = None
    # 该模型的 base_instructions(系统提示词)。
    # 注意: codex 在拉取 /models 后会用返回的 ModelInfo 整体覆盖该 slug 的元数据,
    # 包含 base_instructions;未配置时回退全局默认,仍为空时输出空串,
    # 此时应在 codex config.toml 中通过 base_instructions 自行指定(其优先级更高)。
    base_instructions: str | None = None


@dataclass
class ProviderConfig:
    """单个上游 provider 的配置。

    每个 provider 独立持有出站协议、上游地址、鉴权与可调选项,
    并声明其支持的 model 列表用于路由。
    """

    name: str
    outbound: str
    base_url: str
    api_key: str
    path: str
    # 这里的 models 是入站模型名,也是 /models 目录对客户端暴露的 slug
    models: list[str]
    # enabled=false 时 provider 不参与路由与 /models 输出,但仍保留在配置中供控制台重新启用
    enabled: bool = True
    # 同一模型存在多个启用 provider 时的加权路由权重
    weight: float = 1.0
    # model 名 -> 该 model 的元数据(仅 models 写成对象形式时才有条目)
    model_meta: dict[str, ModelMeta] = field(default_factory=dict)
    # 入站 model 名 -> 该 provider 实际出站 model 名;未配置时默认同名
    model_map: dict[str, str] = field(default_factory=dict)
    timeout: float = 300.0
    # 出站为 chat 时是否透传未识别的顶层字段
    passthrough_unknown: bool = True
    # models 为空表示"全匹配"兜底 provider(仅当配置中只有一个 provider 时允许)
    catch_all: bool = False
    # 是否支持 web_search 内置工具(如 mimo);不支持时自动丢弃
    supports_web_search: bool = False
    # 是否在出站请求体顶层附加 webSearchEnabled=true(部分 mimo 端点如 token-plan 强制要求,
    # 标准端点 api.xiaomimimo.com 不需要也不识别此字段);仅当请求确实携带 web_search 工具时生效
    web_search_enabled: bool = False


@dataclass
class Config:
    """代理整体配置。"""

    host: str
    port: int
    providers: list[ProviderConfig]
    model_map: dict[str, str] = field(default_factory=dict)
    # /models 目录的全局默认: 模型未单独配置时的上下文窗口(token 数),None 表示不暴露
    default_context_window: int | None = None
    # /models 目录的全局默认: 可用上下文百分比
    default_effective_context_window_percent: int = 95
    # /models 目录的全局默认: base_instructions(系统提示词)
    default_base_instructions: str | None = None
    # /models 模板文件路径(自定义 ModelInfo 模板);为空时使用内置 gpt-5.5 模板
    model_template_path: str | None = None
    # 路由索引: 入站 model 名 -> 启用 provider 候选列表;构造后填充
    _model_index: dict[str, list[ProviderConfig]] = field(default_factory=dict, repr=False)
    _catch_all: ProviderConfig | None = field(default=None, repr=False)

    @property
    def active_providers(self) -> list[ProviderConfig]:
        """返回 enabled=true 的 provider。"""
        return [p for p in self.providers if p.enabled]

    def map_model(self, model: str | None) -> str | None:
        """按全局 model_map 映射模型名,仅用于旧配置兼容。"""
        if model is None:
            return None
        return self.model_map.get(model, model)

    def resolve(self, model: str | None) -> tuple[ProviderConfig | None, str | None]:
        """按入站 model 解析目标 provider 与出站 model 名。

        流程:
        1. 以入站 model 名精确匹配启用 provider 的 models 列表;
        2. 命中后按 provider.model_map 将入站 model 映射为该 provider 的出站 model 名;
        3. 未命中时尝试旧版全局 model_map 兼容路径;
        4. 仍未命中时若存在兜底 provider(models 为空)则路由到它;
        5. 仍无匹配返回 (None, 入站或兼容映射后的 model 名),由调用方报错。

        返回 (provider 或 None, 出站 model 名 或 None)。
        """
        inbound = model
        if inbound is not None and inbound in self._model_index:
            provider = _pick_weighted(self._model_index[inbound])
            return provider, _provider_outbound_model(provider, inbound)

        # 兼容旧版顶层 model_map: 入站别名 -> provider models 中声明的旧出站名。
        effective = self.map_model(inbound)
        if effective is not None and effective != inbound and effective in self._model_index:
            provider = _pick_weighted(self._model_index[effective])
            return provider, _provider_outbound_model(provider, effective)

        if self._catch_all is not None:
            return self._catch_all, effective
        return None, effective


def _provider_outbound_model(provider: ProviderConfig | None, inbound_model: str | None) -> str | None:
    """按 provider 内部映射得到实际发给上游的 model 名。"""
    if provider is None or inbound_model is None:
        return inbound_model
    return provider.model_map.get(inbound_model, inbound_model)


def _pick_weighted(providers: list[ProviderConfig]) -> ProviderConfig | None:
    """按 provider.weight 在候选 provider 中做加权随机选择。"""
    if not providers:
        return None
    if len(providers) == 1:
        return providers[0]
    weighted = [(p, p.weight) for p in providers if p.weight > 0]
    if not weighted:
        return providers[0]
    total = sum(weight for _p, weight in weighted)
    if total <= 0:
        return providers[0]
    point = random.uniform(0, total)
    upto = 0.0
    for provider, weight in weighted:
        upto += weight
        if point <= upto:
            return provider
    return weighted[-1][0]


def _resolve_api_key(raw: dict[str, Any]) -> str:
    """解析 api_key: 显式值优先,否则从环境变量读取。"""
    api_key = (raw.get("api_key") or "").strip()
    if api_key:
        return api_key
    env_name = raw.get("api_key_env")
    if env_name:
        return (os.environ.get(env_name) or "").strip()
    return ""


def _build_model_meta(provider_name: str, name: str, raw: dict[str, Any]) -> ModelMeta:
    """从对象形式的 model 条目构造 ModelMeta,对数值字段做整数校验。"""

    def _int_or_none(key: str) -> int | None:
        v = raw.get(key)
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ValueError(
                f"provider {provider_name!r} 的 model {name!r} 字段 {key} 必须为整数,当前为: {v!r}"
            )

    ecwp = raw.get("effective_context_window_percent")
    return ModelMeta(
        name=name,
        context_window=_int_or_none("context_window"),
        max_context_window=_int_or_none("max_context_window"),
        auto_compact_token_limit=_int_or_none("auto_compact_token_limit"),
        effective_context_window_percent=(int(ecwp) if ecwp is not None else None),
        display_name=(str(raw["display_name"]).strip() if raw.get("display_name") else None),
        description=(str(raw["description"]) if raw.get("description") else None),
        base_instructions=(str(raw["base_instructions"]) if raw.get("base_instructions") else None),
    )


def _parse_models(provider_name: str, models_raw: Any) -> tuple[list[str], dict[str, ModelMeta], dict[str, str]]:
    """解析 provider 的 models 列表。

    兼容两种条目写法,可在同一列表中混用:
    - 字符串: 入站 model 名,出站 model 默认同名,无附加元数据;
    - 对象: 必含 `name`,可附带 context_window 等元数据(供 /models 暴露),
      以及 mapped_model(实际发给该 provider 的模型名)。

    返回 (入站 model 名列表[保持声明顺序], model 名 -> ModelMeta 映射, model 名 -> 出站 model 名)。
    """
    if not isinstance(models_raw, list):
        raise ValueError(f"provider {provider_name!r} 的 models 必须是列表")

    seen: set[str] = set()
    models: list[str] = []
    meta_map: dict[str, ModelMeta] = {}
    model_map: dict[str, str] = {}
    for m in models_raw:
        if isinstance(m, dict):
            name = str(m.get("name") or "").strip()
            if not name:
                raise ValueError(f"provider {provider_name!r} 的 models 对象条目缺少 name 字段")
            meta: ModelMeta | None = _build_model_meta(provider_name, name, m)
            mapped_model = str(
                m.get("mapped_model") or m.get("target_model") or m.get("upstream_model") or name
            ).strip()
            if not mapped_model:
                mapped_model = name
        else:
            name = str(m).strip()
            meta = None
            mapped_model = name
        if not name:
            continue
        if name in seen:
            raise ValueError(f"provider {provider_name!r} 的 models 内部存在重复项: {name!r}")
        seen.add(name)
        models.append(name)
        if meta is not None:
            meta_map[name] = meta
        model_map[name] = mapped_model
    return models, meta_map, model_map


def _build_provider(name: str, raw: dict[str, Any], *,
                    global_options: dict[str, Any]) -> ProviderConfig:
    """从单个 provider 原始配置构造 ProviderConfig,缺省回退到全局默认/选项。"""
    if not isinstance(raw, dict):
        raise ValueError(f"provider {name!r} 必须是映射(对象)")

    # 出站协议校验
    outbound = str(raw.get("outbound", "")).strip()
    if outbound not in _VALID_OUTBOUND:
        raise ValueError(
            f"provider {name!r} 的 outbound 必须为 {sorted(_VALID_OUTBOUND)} 之一,当前为: {outbound!r}"
        )

    # 上游地址
    base_url = str(raw.get("base_url", "")).strip().rstrip("/")
    if not base_url:
        raise ValueError(f"provider {name!r} 的 base_url 不能为空")

    # 请求路径: 未配置时按出站协议取默认值
    path = str(raw.get("path") or _DEFAULT_PATH[outbound]).strip()
    if not path.startswith("/"):
        path = "/" + path

    # models 列表(去重并保持声明顺序);允许为空表示兜底 provider。
    # 条目可为字符串或带元数据的对象(见 _parse_models)。
    models, model_meta, provider_model_map = _parse_models(name, raw.get("models") or [])

    enabled = bool(raw.get("enabled", True))
    try:
        weight = float(raw.get("weight", 1))
    except (TypeError, ValueError):
        raise ValueError(f"provider {name!r} 的 weight 必须为数字")
    if weight < 0:
        raise ValueError(f"provider {name!r} 的 weight 不能为负数")

    # 选项: provider 级优先,缺省回退全局
    def _opt(key: str, default: Any) -> Any:
        if key in raw:
            return raw[key]
        if key in global_options:
            return global_options[key]
        return default

    return ProviderConfig(
        name=name,
        outbound=outbound,
        base_url=base_url,
        api_key=_resolve_api_key(raw),
        path=path,
        models=models,
        enabled=enabled,
        weight=weight,
        model_meta=model_meta,
        model_map=provider_model_map,
        timeout=float(raw.get("timeout", 300)),
        passthrough_unknown=bool(_opt("passthrough_unknown", True)),
        catch_all=not models,
        supports_web_search=bool(raw.get("web_search", False)),
        web_search_enabled=bool(raw.get("web_search_enabled", False)),
    )


def _collect_providers(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """从原始配置收集 provider 列表。

    兼容两种写法:
    - 新版: 顶层 `providers`,可为列表(含 name 字段)或映射(键即 name)。
    - 旧版: 顶层 `outbound` + `upstream`,合成单个名为 "default" 的 provider。
    """
    providers_raw = raw.get("providers")

    if providers_raw is not None:
        result: list[dict[str, Any]] = []
        if isinstance(providers_raw, list):
            for idx, p in enumerate(providers_raw):
                if not isinstance(p, dict):
                    raise ValueError(f"providers[{idx}] 必须是映射(对象)")
                name = str(p.get("name") or f"provider_{idx}").strip()
                entry = dict(p)
                entry["name"] = name
                result.append(entry)
        elif isinstance(providers_raw, dict):
            for name, p in providers_raw.items():
                if not isinstance(p, dict):
                    raise ValueError(f"provider {name!r} 必须是映射(对象)")
                entry = dict(p)
                entry["name"] = str(name)
                result.append(entry)
        else:
            raise ValueError("providers 必须是列表或映射(对象)")
        return result

    # 向后兼容: 旧版单上游配置 outbound + upstream
    if "upstream" in raw or "outbound" in raw:
        upstream_raw = raw.get("upstream") or {}
        if not isinstance(upstream_raw, dict):
            raise ValueError("upstream 必须是映射(对象)")
        entry = dict(upstream_raw)
        entry["name"] = "default"
        entry["outbound"] = raw.get("outbound", "")
        # 旧版无 models -> 作为兜底 provider 全匹配
        entry.setdefault("models", [])
        return [entry]

    raise ValueError("缺少 providers 配置(也未提供兼容的 outbound + upstream)")


def load_config(path: str) -> Config:
    """从指定路径加载配置文件。

    校验失败时抛出 ValueError,便于启动阶段快速失败。
    """
    if not os.path.exists(path):
        raise ValueError(f"配置文件不存在: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件根节点必须是映射(对象)")

    server = raw.get("server") or {}
    global_defaults = raw.get("defaults") or {}
    global_options = raw.get("options") or {}

    # /models 目录的全局默认(模型未单独配置上下文时回退到此)
    _dcw = global_defaults.get("context_window")
    default_context_window = int(_dcw) if _dcw is not None else None
    default_effective_context_window_percent = int(
        global_defaults.get("effective_context_window_percent", 95)
    )
    _dbi = global_defaults.get("base_instructions")
    default_base_instructions = str(_dbi) if _dbi else None
    _mt = global_defaults.get("model_template")
    model_template_path = str(_mt) if _mt else None

    # 旧版全局模型映射: 入站 model -> provider models 中声明的模型名。
    # 新配置应使用 providers[].models[].mapped_model;这里仅用于兼容旧配置。
    model_map_raw = raw.get("model_map") or {}
    if not isinstance(model_map_raw, dict):
        raise ValueError("model_map 必须是映射(对象)")
    model_map = {str(k): str(v) for k, v in model_map_raw.items()}

    # 构造各 provider
    provider_entries = _collect_providers(raw)
    if not provider_entries:
        raise ValueError("至少需要配置一个 provider")

    providers: list[ProviderConfig] = []
    names_seen: set[str] = set()
    for entry in provider_entries:
        name = entry["name"]
        if name in names_seen:
            raise ValueError(f"provider 名称重复: {name!r}")
        names_seen.add(name)
        providers.append(_build_provider(
            name, entry,
            global_options=global_options,
        ))

    # 路由索引: 仅 enabled provider 参与路由;同一 model 可对应多个 provider 候选
    model_index: dict[str, list[ProviderConfig]] = {}
    catch_all: ProviderConfig | None = None
    active_providers = [p for p in providers if p.enabled]
    catch_all_providers = [p for p in active_providers if p.catch_all]

    if catch_all_providers:
        # models 为空的兜底 provider 仅在"唯一启用 provider"场景下允许
        if len(active_providers) > 1:
            names = ", ".join(p.name for p in catch_all_providers)
            raise ValueError(
                f"配置了多个启用的 provider 时,每个启用 provider 都必须声明非空 models;"
                f"以下 provider 缺少 models: {names}"
            )
        catch_all = catch_all_providers[0]

    for p in active_providers:
        for m in p.models:
            model_index.setdefault(m, []).append(p)

    return Config(
        host=str(server.get("host", "127.0.0.1")),
        port=int(server.get("port", 8080)),
        providers=providers,
        model_map=model_map,
        default_context_window=default_context_window,
        default_effective_context_window_percent=default_effective_context_window_percent,
        default_base_instructions=default_base_instructions,
        model_template_path=model_template_path,
        _model_index=model_index,
        _catch_all=catch_all,
    )
