"""本地配置控制台: 管理 provider、模型上下文窗口并热加载。"""

from __future__ import annotations

import threading
import os
import tempfile
from dataclasses import dataclass
from typing import Any

import yaml

from .config import Config, load_config


@dataclass
class RuntimeConfig:
    """进程内当前配置容器,支持热替换。"""

    path: str
    current: Config
    lock: threading.RLock

    @classmethod
    def create(cls, path: str, cfg: Config) -> "RuntimeConfig":
        return cls(path=path, current=cfg, lock=threading.RLock())

    def get(self) -> Config:
        with self.lock:
            return self.current

    def reload(self) -> Config:
        cfg = load_config(self.path)
        with self.lock:
            self.current = cfg
        return cfg


def _read_config_raw(config_path: str) -> dict[str, Any]:
    """读取原始 YAML 配置,供控制台保留可展示字段。"""
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件根节点必须是映射(对象)")
    return raw


def _provider_entries(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """把 providers 的列表/映射写法统一为带 name 的列表。"""
    providers_raw = raw.get("providers")
    if providers_raw is None:
        raise ValueError("控制台暂只支持新版 providers 配置")
    if isinstance(providers_raw, list):
        entries: list[dict[str, Any]] = []
        for idx, provider in enumerate(providers_raw):
            if not isinstance(provider, dict):
                continue
            entry = dict(provider)
            entry["name"] = str(provider.get("name") or f"provider_{idx}").strip()
            entries.append(entry)
        return entries
    if isinstance(providers_raw, dict):
        entries = []
        for name, provider in providers_raw.items():
            if not isinstance(provider, dict):
                continue
            entry = dict(provider)
            entry["name"] = str(name)
            entries.append(entry)
        return entries
    raise ValueError("providers 必须是列表或映射(对象)")


def _model_items_from_raw(
    raw_models: Any,
    model_names: list[str],
    model_map: dict[str, str],
    legacy_aliases: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """返回控制台可编辑的模型条目,保留上下文字段。"""
    legacy_aliases = legacy_aliases or {}
    by_name: dict[str, dict[str, Any]] = {}
    if isinstance(raw_models, list):
        for item in raw_models:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    by_name[name] = dict(item)
            else:
                name = str(item).strip()
                if name:
                    by_name[name] = {"name": name}

    result: list[dict[str, Any]] = []
    ordered = list(model_names)
    for name in by_name:
        if name not in ordered:
            ordered.append(name)
    for alias, target in legacy_aliases.items():
        if target in model_names and alias not in ordered:
            ordered.append(alias)
    for name in ordered:
        legacy_target = legacy_aliases.get(name)
        raw = by_name.get(name) or by_name.get(legacy_target or "") or {"name": name}
        mapped_model = (
            raw.get("mapped_model")
            or raw.get("target_model")
            or raw.get("upstream_model")
            or model_map.get(name)
            or legacy_target
            or name
        )
        result.append({
            "name": name,
            "mapped_model": mapped_model,
            "context_window": raw.get("context_window"),
            "max_context_window": raw.get("max_context_window"),
            "auto_compact_token_limit": raw.get("auto_compact_token_limit"),
            "effective_context_window_percent": raw.get("effective_context_window_percent"),
        })
    return result


def provider_summary(cfg: Config, config_path: str | None = None) -> list[dict[str, Any]]:
    """返回控制台/API 展示用 provider 摘要。"""
    raw_by_name: dict[str, dict[str, Any]] = {}
    legacy_aliases = cfg.model_map
    if config_path:
        try:
            raw = _read_config_raw(config_path)
            raw_by_name = {p["name"]: p for p in _provider_entries(raw)}
            raw_model_map = raw.get("model_map")
            if isinstance(raw_model_map, dict):
                legacy_aliases = {str(k): str(v) for k, v in raw_model_map.items()}
        except Exception:
            raw_by_name = {}
    return [
        {
            "name": p.name,
            "enabled": p.enabled,
            "weight": p.weight,
            "outbound": p.outbound,
            "base_url": p.base_url,
            "path": p.path,
            "models": p.models,
            "model_items": _model_items_from_raw(
                raw_by_name.get(p.name, {}).get("models"), p.models, p.model_map, legacy_aliases,
            ),
            "catch_all": p.catch_all,
            "api_key_env": raw_by_name.get(p.name, {}).get("api_key_env", ""),
            "api_key_set": bool(raw_by_name.get(p.name, {}).get("api_key")),
            "timeout": p.timeout,
            "passthrough_unknown": p.passthrough_unknown,
            "web_search": p.supports_web_search,
            "web_search_enabled": p.web_search_enabled,
        }
        for p in cfg.providers
    ]


def _is_control_only_update(updates: list[dict[str, Any]]) -> bool:
    """旧控制台请求只包含 name/enabled/weight,继续走局部更新。"""
    allowed = {"name", "enabled", "weight"}
    return bool(updates) and all(
        isinstance(item, dict) and set(item.keys()) <= allowed for item in updates
    )


def _atomic_write_verified(config_path: str, raw: dict[str, Any]) -> None:
    """写临时配置并验证,通过后原子替换正式配置。"""
    directory = os.path.dirname(os.path.abspath(config_path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".codex_shift_config_", suffix=".yaml", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
        load_config(tmp_path)
        os.replace(tmp_path, config_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _update_provider_controls_only(raw: dict[str, Any], updates: list[dict[str, Any]]) -> None:
    """只更新配置文件中的 provider enabled/weight 字段。"""
    providers_raw = raw.get("providers")
    if providers_raw is None:
        raise ValueError("控制台暂只支持新版 providers 配置")
    update_map: dict[str, dict[str, Any]] = {}
    for item in updates:
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("provider 更新项缺少 name")
        if "enabled" not in item or "weight" not in item:
            raise ValueError(f"provider {name!r} 更新项必须包含 enabled 与 weight")
        try:
            weight = float(item["weight"])
        except (TypeError, ValueError):
            raise ValueError(f"provider {name!r} 的 weight 必须为数字")
        if weight < 0:
            raise ValueError(f"provider {name!r} 的 weight 不能为负数")
        update_map[name] = {"enabled": bool(item["enabled"]), "weight": weight}

    seen: set[str] = set()
    if isinstance(providers_raw, list):
        for idx, provider in enumerate(providers_raw):
            if not isinstance(provider, dict):
                continue
            name = str(provider.get("name") or f"provider_{idx}").strip()
            if name in update_map:
                provider["enabled"] = update_map[name]["enabled"]
                provider["weight"] = update_map[name]["weight"]
                seen.add(name)
    elif isinstance(providers_raw, dict):
        for name, provider in providers_raw.items():
            if not isinstance(provider, dict):
                continue
            pname = str(name)
            if pname in update_map:
                provider["enabled"] = update_map[pname]["enabled"]
                provider["weight"] = update_map[pname]["weight"]
                seen.add(pname)
    else:
        raise ValueError("providers 必须是列表或映射(对象)")

    missing = sorted(set(update_map) - seen)
    if missing:
        raise ValueError(f"配置中不存在这些 provider: {', '.join(missing)}")


def _number_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"上下文窗口字段必须为整数,当前为: {value!r}")
    if number < 0:
        raise ValueError("上下文窗口字段不能为负数")
    return number


def _clean_model_items(items: Any, existing_models: Any) -> list[Any]:
    """清理控制台提交的模型列表,并尽量保留未展示的模型元数据字段。"""
    if not isinstance(items, list):
        raise ValueError("provider models 必须是数组")
    existing: dict[str, dict[str, Any]] = {}
    if isinstance(existing_models, list):
        for item in existing_models:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    existing[name] = dict(item)

    cleaned: list[Any] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item).strip()
            item = {"name": name}
        if not name:
            continue
        if name in seen:
            raise ValueError(f"models 内部存在重复项: {name!r}")
        seen.add(name)

        base = existing.get(name, {"name": name})
        base["name"] = name
        mapped_model = str(
            item.get("mapped_model") or item.get("target_model") or item.get("upstream_model") or name
        ).strip()
        if not mapped_model:
            mapped_model = name
        if mapped_model == name:
            base.pop("mapped_model", None)
            base.pop("target_model", None)
            base.pop("upstream_model", None)
        else:
            base["mapped_model"] = mapped_model
        touched_meta = False
        for key in (
            "context_window",
            "max_context_window",
            "auto_compact_token_limit",
            "effective_context_window_percent",
        ):
            if key in item:
                value = _number_or_none(item.get(key))
                touched_meta = touched_meta or value is not None
                if value is None:
                    base.pop(key, None)
                else:
                    base[key] = value
        has_meta = any(k in base for k in (
            "context_window",
            "max_context_window",
            "auto_compact_token_limit",
            "effective_context_window_percent",
            "display_name",
            "description",
            "base_instructions",
            "mapped_model",
        ))
        cleaned.append(base if has_meta or touched_meta else name)
    return cleaned


def _replace_providers(raw: dict[str, Any], submitted: list[dict[str, Any]]) -> None:
    """用控制台提交的完整 provider 列表替换 providers 配置。"""
    existing = {p["name"]: p for p in _provider_entries(raw)}
    providers: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in submitted:
        if not isinstance(item, dict):
            raise ValueError("providers 数组中的条目必须是对象")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("provider 缺少 name")
        if name in seen:
            raise ValueError(f"provider 名称重复: {name!r}")
        seen.add(name)

        provider = dict(existing.get(name, {}))
        provider["name"] = name
        for key in ("enabled", "passthrough_unknown", "web_search", "web_search_enabled"):
            if key in item:
                provider[key] = bool(item[key])
        if "weight" in item:
            try:
                provider["weight"] = float(item["weight"])
            except (TypeError, ValueError):
                raise ValueError(f"provider {name!r} 的 weight 必须为数字")
            if provider["weight"] < 0:
                raise ValueError(f"provider {name!r} 的 weight 不能为负数")
        for key in ("outbound", "base_url", "path", "api_key_env"):
            if key in item:
                value = str(item.get(key) or "").strip()
                if value:
                    provider[key] = value
                else:
                    provider.pop(key, None)
        if item.get("api_key"):
            provider["api_key"] = str(item["api_key"]).strip()
        if "timeout" in item and item.get("timeout") not in (None, ""):
            try:
                provider["timeout"] = float(item["timeout"])
            except (TypeError, ValueError):
                raise ValueError(f"provider {name!r} 的 timeout 必须为数字")
        if "model_items" in item:
            provider["models"] = _clean_model_items(item["model_items"], provider.get("models"))
        elif "models" in item:
            provider["models"] = _clean_model_items(item["models"], provider.get("models"))

        providers.append(provider)

    raw["providers"] = providers
    raw.pop("model_map", None)


def update_provider_controls(config_path: str, updates: list[dict[str, Any]]) -> None:
    """更新 provider 配置。

    兼容旧请求: 只包含 name/enabled/weight 时仅局部更新;
    新控制台提交完整 provider 列表时,支持新增、删除与模型上下文窗口编辑。
    """
    raw = _read_config_raw(config_path)
    if _is_control_only_update(updates):
        _update_provider_controls_only(raw, updates)
    else:
        _replace_providers(raw, updates)
    _atomic_write_verified(config_path, raw)


ADMIN_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>codex-shift 控制台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7f9;
      --panel: #ffffff;
      --panel-soft: #f0f4f7;
      --line: #d6dee7;
      --line-strong: #aebccc;
      --text: #16202b;
      --muted: #667487;
      --accent: #0d806c;
      --accent-2: #356fe8;
      --danger: #b33b3b;
      --ok: #087a58;
      --warn: #94650c;
      --shadow: 0 14px 38px rgba(20, 32, 46, 0.09);
      --focus: 0 0 0 3px rgba(13, 128, 108, 0.22);
    }
    @media (prefers-color-scheme: dark) {
      :root {
        color-scheme: dark;
        --bg: #111820;
        --panel: #19222d;
        --panel-soft: #121a23;
        --line: #2b3948;
        --line-strong: #43566b;
        --text: #edf3f8;
        --muted: #a0adbb;
        --accent: #46c6aa;
        --accent-2: #7da4ff;
        --danger: #ff8181;
        --ok: #58d6aa;
        --warn: #f0bf68;
        --shadow: 0 14px 38px rgba(0, 0, 0, 0.28);
        --focus: 0 0 0 3px rgba(70, 198, 170, 0.24);
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 20;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      box-shadow: 0 1px 0 rgba(20, 32, 46, 0.02);
    }
    .topbar {
      max-width: 1360px;
      margin: 0 auto;
      padding: 16px 22px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
    }
    h1 { margin: 0; font-size: 21px; letter-spacing: 0; }
    .subtitle { margin-top: 2px; color: var(--muted); }
    main { max-width: 1360px; margin: 0 auto; padding: 18px 22px 36px; }
    .toolbar, .inline-actions, .model-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    button {
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 0 11px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
      cursor: pointer;
    }
    button:hover { border-color: var(--line-strong); }
    button:focus-visible, input:focus-visible, select:focus-visible {
      outline: none;
      box-shadow: var(--focus);
    }
    .primary { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 650; }
    .danger { color: var(--danger); }
    .section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    .section + .section { margin-top: 14px; }
    .section-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 13px 15px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-soft);
    }
    .section-title { font-weight: 720; }
    #status { min-height: 20px; color: var(--muted); }
    #status.error { color: var(--danger); }
    #status.ok { color: var(--ok); }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 1040px; }
    .providers-table {
      table-layout: fixed;
      min-width: 1712px;
    }
    .providers-table .col-provider { width: 190px; }
    .providers-table .col-enabled { width: 84px; }
    .providers-table .col-weight { width: 84px; }
    .providers-table .col-protocol { width: 172px; }
    .providers-table .col-upstream { width: 310px; }
    .providers-table .col-auth { width: 220px; }
    .providers-table .col-models { width: 560px; }
    .providers-table .col-actions { width: 92px; }
    th, td {
      padding: 11px 12px;
      text-align: left;
      vertical-align: top;
      border-bottom: 1px solid var(--line);
    }
    th { color: var(--muted); font-size: 12px; font-weight: 650; background: var(--panel-soft); }
    tr:last-child td { border-bottom: 0; }
    input, select {
      width: 100%;
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 0 9px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
    }
    input[type="checkbox"] { width: 18px; min-height: 18px; accent-color: var(--accent); }
    .field-stack { display: grid; gap: 7px; min-width: 180px; }
    .field-note { color: var(--muted); font-size: 12px; }
    .provider-name { min-width: 150px; }
    .endpoint { min-width: 260px; }
    .number { width: 60px; min-width: 60px; max-width: none; }
    .protocol-select { min-width: 148px; }
    .models-editor { display: grid; gap: 8px; min-width: 520px; }
    .model-row {
      display: grid;
      grid-template-columns: minmax(150px, 1fr) minmax(150px, 1fr) 128px 34px;
      align-items: center;
      gap: 7px;
    }
    .model-header {
      display: grid;
      grid-template-columns: minmax(150px, 1fr) minmax(150px, 1fr) 128px 34px;
      gap: 7px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }
    .icon-button {
      width: 34px;
      padding: 0;
      text-align: center;
      font-weight: 760;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 8px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      background: var(--panel);
      font-size: 12px;
      font-weight: 650;
      white-space: nowrap;
    }
    .pill.on { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 34%, var(--line)); }
    .pill.off { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 32%, var(--line)); }
    .route-stack { display: grid; gap: 7px; min-width: 280px; }
    .route-line {
      display: grid;
      grid-template-columns: minmax(110px, 1fr) minmax(120px, 180px) 52px;
      gap: 8px;
      align-items: center;
    }
    .route-provider { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 650; }
    .route-bar { height: 8px; border-radius: 999px; background: var(--panel-soft); border: 1px solid var(--line); overflow: hidden; }
    .route-fill { height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--accent), var(--accent-2)); }
    .route-percent { text-align: right; font-variant-numeric: tabular-nums; }
    .empty { color: var(--muted); padding: 16px; }
    @media (max-width: 820px) {
      .topbar { align-items: flex-start; flex-direction: column; }
      main { padding: 14px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div>
        <h1>codex-shift 控制台</h1>
        <div class="subtitle">Provider、模型和上下文窗口热管理</div>
      </div>
      <div class="toolbar">
        <button id="add-provider">新增 Provider</button>
        <button id="refresh">刷新</button>
        <button id="save" class="primary">保存并热加载</button>
      </div>
    </div>
  </header>
  <main>
    <section class="section">
      <div class="section-head">
        <div class="section-title">Provider 路由池</div>
        <div id="status"></div>
      </div>
      <div class="table-wrap">
        <table class="providers-table">
          <colgroup>
            <col class="col-provider">
            <col class="col-enabled">
            <col class="col-weight">
            <col class="col-protocol">
            <col class="col-upstream">
            <col class="col-auth">
            <col class="col-models">
            <col class="col-actions">
          </colgroup>
          <thead>
            <tr>
              <th>Provider</th>
              <th>启用 Provider</th>
              <th>权重</th>
              <th>协议</th>
              <th>上游</th>
              <th>鉴权</th>
              <th>模型与上下文窗口</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody id="providers"></tbody>
        </table>
      </div>
    </section>
    <section class="section">
      <div class="section-head">
        <div class="section-title">模型路由表</div>
        <div class="field-note">根据当前编辑状态实时计算</div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>模型</th>
              <th>启用 Provider</th>
              <th>总权重</th>
              <th>路由概率</th>
            </tr>
          </thead>
          <tbody id="routes"></tbody>
        </table>
      </div>
    </section>
  </main>
  <script>
    const tbody = document.getElementById("providers");
    const routesBody = document.getElementById("routes");
    const statusEl = document.getElementById("status");
    let providers = [];

    function esc(value) {
      return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[ch]));
    }

    function setStatus(text, error = false) {
      statusEl.textContent = text;
      statusEl.className = error ? "error" : (text.includes("成功") ? "ok" : "");
    }

    function normalizeProvider(p = {}) {
      const modelItems = Array.isArray(p.model_items) && p.model_items.length
        ? p.model_items
        : (p.models || []).map((name) => ({name, mapped_model: name, context_window: ""}));
      return {
        name: p.name || nextProviderName(),
        enabled: p.enabled !== false,
        weight: Number(p.weight ?? 1),
        outbound: p.outbound || "chat_completions",
        base_url: p.base_url || "",
        path: p.path || "",
        api_key_env: p.api_key_env || "",
        api_key: "",
        api_key_set: Boolean(p.api_key_set),
        timeout: Number(p.timeout ?? 300),
        passthrough_unknown: p.passthrough_unknown !== false,
        web_search: Boolean(p.web_search),
        web_search_enabled: Boolean(p.web_search_enabled),
        model_items: modelItems.map((m) => ({
          name: m.name || "",
          mapped_model: m.mapped_model || m.name || "",
          context_window: m.context_window ?? "",
        })),
      };
    }

    function nextProviderName() {
      let idx = providers.length + 1;
      let name = `provider_${idx}`;
      const names = new Set(providers.map((p) => p.name));
      while (names.has(name)) name = `provider_${++idx}`;
      return name;
    }

    function providerModelNames(provider) {
      return (provider.model_items || [])
        .map((m) => String(m.name || "").trim())
        .filter(Boolean);
    }

    function providerMappedModel(provider, inboundModel) {
      const item = (provider.model_items || []).find((m) => String(m.name || "").trim() === inboundModel);
      return String(item?.mapped_model || inboundModel).trim() || inboundModel;
    }

    function formatNumber(value) {
      return Number(value || 0).toFixed(1).replace(/\\.0$/, "");
    }

    function routeRows() {
      const models = new Map();
      for (const provider of providers) {
        for (const model of providerModelNames(provider)) {
          if (!models.has(model)) models.set(model, []);
          models.get(model).push(provider);
        }
      }
      return [...models.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([model, candidates]) => {
          const active = candidates.filter((p) => p.enabled);
          const positive = active.filter((p) => Number(p.weight || 0) > 0);
          const totalWeight = positive.reduce((sum, p) => sum + Number(p.weight || 0), 0);
          return {model, active, totalWeight};
        });
    }

    function renderRoutes() {
      routesBody.innerHTML = "";
      const rows = routeRows();
      if (!rows.length) {
        routesBody.innerHTML = `<tr><td colspan="4" class="empty">暂无模型</td></tr>`;
        return;
      }
      for (const row of rows) {
        const fallbackProvider = row.active[0]?.name;
        const routeHtml = row.active.length
          ? row.active.map((p) => {
              let probability = 0;
              if (row.totalWeight > 0) {
                probability = Number(p.weight || 0) > 0 ? Number(p.weight || 0) / row.totalWeight * 100 : 0;
              } else if (p.name === fallbackProvider) {
                probability = 100;
              }
              return `
                <div class="route-line">
                  <div class="route-provider" title="${esc(p.name)} -> ${esc(providerMappedModel(p, row.model))}">
                    ${esc(p.name)} -> ${esc(providerMappedModel(p, row.model))}
                  </div>
                  <div class="route-bar" aria-hidden="true"><div class="route-fill" style="width:${probability}%"></div></div>
                  <div class="route-percent">${probability.toFixed(1).replace(/\\.0$/, "")}%</div>
                </div>
              `;
            }).join("")
          : `<div class="field-note">无启用 provider</div>`;
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><strong>${esc(row.model)}</strong></td>
          <td><span class="pill ${row.active.length ? "on" : "off"}">${row.active.length}</span></td>
          <td>${formatNumber(row.totalWeight)}</td>
          <td><div class="route-stack">${routeHtml}</div></td>
        `;
        routesBody.appendChild(tr);
      }
    }

    function renderModels(provider, providerIndex) {
      const rows = provider.model_items.map((model, modelIndex) => `
        <div class="model-row">
          <input data-provider="${providerIndex}" data-model="${modelIndex}" data-field="name"
                 value="${esc(model.name)}" placeholder="入站模型">
          <input data-provider="${providerIndex}" data-model="${modelIndex}" data-field="mapped_model"
                 value="${esc(model.mapped_model || model.name)}" placeholder="出站模型">
          <input data-provider="${providerIndex}" data-model="${modelIndex}" data-field="context_window"
                 value="${esc(model.context_window)}" inputmode="numeric" placeholder="context_window">
          <button class="icon-button danger" data-action="delete-model" data-provider="${providerIndex}" data-model="${modelIndex}" title="删除模型">×</button>
        </div>
      `).join("");
      return `
        <div class="models-editor">
          <div class="model-header"><span>入站模型</span><span>出站模型</span><span>上下文窗口</span><span></span></div>
          ${rows || `<div class="field-note">暂无模型</div>`}
          <div class="model-actions">
            <button data-action="add-model" data-provider="${providerIndex}">添加模型</button>
          </div>
        </div>
      `;
    }

    function renderProviders() {
      tbody.innerHTML = "";
      if (!providers.length) {
        tbody.innerHTML = `<tr><td colspan="8" class="empty">暂无 provider，请先新增。</td></tr>`;
        return;
      }
      providers.forEach((p, index) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>
            <div class="field-stack provider-name">
              <input data-provider="${index}" data-field="name" value="${esc(p.name)}" placeholder="provider name">
              <span class="field-note">${p.enabled ? "运行中" : "已停用"}</span>
            </div>
          </td>
          <td><input type="checkbox" data-provider="${index}" data-field="enabled" ${p.enabled ? "checked" : ""}></td>
          <td><input class="number" type="number" min="0" step="0.1" data-provider="${index}" data-field="weight" value="${esc(p.weight)}"></td>
          <td>
            <select class="protocol-select" data-provider="${index}" data-field="outbound">
              <option value="chat_completions" ${p.outbound === "chat_completions" ? "selected" : ""}>chat_completions</option>
              <option value="responses" ${p.outbound === "responses" ? "selected" : ""}>responses</option>
            </select>
          </td>
          <td>
            <div class="field-stack endpoint">
              <input data-provider="${index}" data-field="base_url" value="${esc(p.base_url)}" placeholder="https://api.example.com">
              <input data-provider="${index}" data-field="path" value="${esc(p.path)}" placeholder="path, 可留空使用默认">
            </div>
          </td>
          <td>
            <div class="field-stack">
              <input data-provider="${index}" data-field="api_key_env" value="${esc(p.api_key_env)}" placeholder="API_KEY_ENV">
              <input data-provider="${index}" data-field="api_key" value="" placeholder="${p.api_key_set ? "已设置，留空保留" : "直接 API Key，可留空"}">
            </div>
          </td>
          <td>${renderModels(p, index)}</td>
          <td>
            <div class="inline-actions">
              <button class="danger" data-action="delete-provider" data-provider="${index}">删除</button>
            </div>
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    function render() {
      renderProviders();
      renderRoutes();
    }

    function updateStateFromInput(target) {
      const pIndex = Number(target.dataset.provider);
      if (!Number.isInteger(pIndex) || !providers[pIndex]) return;
      const field = target.dataset.field;
      const mIndex = target.dataset.model !== undefined ? Number(target.dataset.model) : null;
      let value = target.type === "checkbox" ? target.checked : target.value;
      if (field === "weight" || field === "timeout") value = Number(value || 0);
      if (mIndex !== null && Number.isInteger(mIndex)) {
        providers[pIndex].model_items[mIndex][field] = value;
      } else {
        providers[pIndex][field] = value;
      }
      renderRoutes();
    }

    function payloadProviders() {
      return providers.map((p) => ({
        name: String(p.name || "").trim(),
        enabled: Boolean(p.enabled),
        weight: Number(p.weight || 0),
        outbound: p.outbound,
        base_url: String(p.base_url || "").trim(),
        path: String(p.path || "").trim(),
        api_key_env: String(p.api_key_env || "").trim(),
        api_key: String(p.api_key || "").trim(),
        timeout: Number(p.timeout || 300),
        passthrough_unknown: Boolean(p.passthrough_unknown),
        web_search: Boolean(p.web_search),
        web_search_enabled: Boolean(p.web_search_enabled),
        model_items: (p.model_items || []).map((m) => ({
          name: String(m.name || "").trim(),
          mapped_model: String(m.mapped_model || m.name || "").trim(),
          context_window: String(m.context_window ?? "").trim(),
        })).filter((m) => m.name),
      }));
    }

    async function load() {
      setStatus("正在读取配置...");
      const resp = await fetch("/admin/api/config");
      if (!resp.ok) throw new Error(await resp.text());
      const data = await resp.json();
      providers = (data.providers || []).map(normalizeProvider);
      render();
      setStatus(`已加载 ${providers.length} 个 provider`);
    }

    async function save() {
      setStatus("正在保存并热加载...");
      const resp = await fetch("/admin/api/config", {
        method: "POST",
        headers: {"content-type": "application/json"},
        body: JSON.stringify({providers: payloadProviders()}),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.error?.message || await resp.text());
      }
      const data = await resp.json();
      providers = (data.providers || []).map(normalizeProvider);
      render();
      setStatus("保存成功, 新配置已生效");
    }

    tbody.addEventListener("input", (event) => updateStateFromInput(event.target));
    tbody.addEventListener("change", (event) => updateStateFromInput(event.target));
    tbody.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      const pIndex = Number(button.dataset.provider);
      const mIndex = Number(button.dataset.model);
      if (button.dataset.action === "delete-provider") {
        providers.splice(pIndex, 1);
      } else if (button.dataset.action === "add-model") {
        providers[pIndex].model_items.push({name: "", mapped_model: "", context_window: ""});
      } else if (button.dataset.action === "delete-model") {
        providers[pIndex].model_items.splice(mIndex, 1);
      }
      render();
    });

    document.getElementById("add-provider").addEventListener("click", () => {
      providers.push(normalizeProvider({
        name: nextProviderName(),
        enabled: false,
        weight: 1,
        outbound: "chat_completions",
        base_url: "",
        path: "",
        api_key_env: "",
        model_items: [{name: "", mapped_model: "", context_window: ""}],
      }));
      render();
    });
    document.getElementById("refresh").addEventListener("click", () => load().catch((e) => setStatus(e.message, true)));
    document.getElementById("save").addEventListener("click", () => save().catch((e) => setStatus(e.message, true)));
    load().catch((e) => setStatus(e.message, true));
  </script>
</body>
</html>
"""
