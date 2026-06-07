"""本地配置控制台: 展示 provider 状态,保存 enabled/weight 并热加载。"""

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


def provider_summary(cfg: Config) -> list[dict[str, Any]]:
    """返回控制台/API 展示用 provider 摘要。"""
    return [
        {
            "name": p.name,
            "enabled": p.enabled,
            "weight": p.weight,
            "outbound": p.outbound,
            "base_url": p.base_url,
            "path": p.path,
            "models": p.models,
            "catch_all": p.catch_all,
        }
        for p in cfg.providers
    ]


def update_provider_controls(config_path: str, updates: list[dict[str, Any]]) -> None:
    """只更新配置文件中的 provider enabled/weight 字段。"""
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件根节点必须是映射(对象)")

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

    directory = os.path.dirname(os.path.abspath(config_path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".codex_shift_config_", suffix=".yaml", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
        # 先加载临时配置验证路由/字段约束,通过后再原子替换正式配置。
        load_config(tmp_path)
        os.replace(tmp_path, config_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


ADMIN_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>codex-shift 控制台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f6f8;
      --bg-soft: #e9edf2;
      --panel: #ffffff;
      --panel-2: #f9fbfc;
      --line: #d7dde5;
      --line-strong: #c5ced9;
      --text: #17202c;
      --muted: #627084;
      --soft-text: #354255;
      --accent: #11826f;
      --accent-2: #2f6fed;
      --accent-dark: #0a6657;
      --danger: #b13a3a;
      --warning: #a26b07;
      --ok: #0b7a59;
      --shadow: 0 18px 45px rgba(20, 32, 46, 0.10);
      --focus: 0 0 0 3px rgba(17, 130, 111, 0.20);
    }
    @media (prefers-color-scheme: dark) {
      :root {
        color-scheme: dark;
        --bg: #10151d;
        --bg-soft: #151c26;
        --panel: #19212c;
        --panel-2: #141b24;
        --line: #2a3544;
        --line-strong: #3a485a;
        --text: #edf3f8;
        --muted: #9ba9b9;
        --soft-text: #c8d2dd;
        --accent: #43c6a9;
        --accent-2: #7aa7ff;
        --accent-dark: #2ca88f;
        --danger: #ff7f7f;
        --warning: #f0bd64;
        --ok: #58d5a9;
        --shadow: 0 18px 45px rgba(0, 0, 0, 0.32);
        --focus: 0 0 0 3px rgba(67, 198, 169, 0.24);
      }
    }
    html[data-theme="light"] {
      color-scheme: light;
      --bg: #f4f6f8;
      --bg-soft: #e9edf2;
      --panel: #ffffff;
      --panel-2: #f9fbfc;
      --line: #d7dde5;
      --line-strong: #c5ced9;
      --text: #17202c;
      --muted: #627084;
      --soft-text: #354255;
      --accent: #11826f;
      --accent-2: #2f6fed;
      --accent-dark: #0a6657;
      --danger: #b13a3a;
      --warning: #a26b07;
      --ok: #0b7a59;
      --shadow: 0 18px 45px rgba(20, 32, 46, 0.10);
      --focus: 0 0 0 3px rgba(17, 130, 111, 0.20);
    }
    html[data-theme="dark"] {
      color-scheme: dark;
      --bg: #10151d;
      --bg-soft: #151c26;
      --panel: #19212c;
      --panel-2: #141b24;
      --line: #2a3544;
      --line-strong: #3a485a;
      --text: #edf3f8;
      --muted: #9ba9b9;
      --soft-text: #c8d2dd;
      --accent: #43c6a9;
      --accent-2: #7aa7ff;
      --accent-dark: #2ca88f;
      --danger: #ff7f7f;
      --warning: #f0bd64;
      --ok: #58d5a9;
      --shadow: 0 18px 45px rgba(0, 0, 0, 0.32);
      --focus: 0 0 0 3px rgba(67, 198, 169, 0.24);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at 18% 0%, rgba(47, 111, 237, 0.11), transparent 30%),
        linear-gradient(180deg, var(--bg-soft), var(--bg) 260px);
      color: var(--text);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      padding: 20px 24px 0;
    }
    .topbar {
      max-width: 1180px;
      margin: 0 auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      padding: 16px 0 20px;
      border-bottom: 1px solid var(--line);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }
    .mark {
      width: 42px;
      height: 42px;
      border-radius: 10px;
      display: grid;
      place-items: center;
      color: #fff;
      font-weight: 800;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      box-shadow: 0 12px 28px rgba(17, 130, 111, 0.22);
      flex: 0 0 auto;
    }
    h1 { margin: 0; font-size: 22px; font-weight: 720; letter-spacing: 0; }
    .subtitle { color: var(--muted); margin-top: 2px; }
    main { max-width: 1180px; margin: 0 auto; padding: 22px 24px 34px; }
    .toolbar {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      flex-wrap: wrap;
    }
    button {
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--panel);
      color: var(--text);
      min-height: 36px;
      padding: 0 13px;
      font: inherit;
      cursor: pointer;
      transition: border-color .16s ease, background .16s ease, transform .16s ease;
    }
    button:hover { border-color: var(--line-strong); transform: translateY(-1px); }
    button:focus-visible, input:focus-visible { outline: none; box-shadow: var(--focus); }
    button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
      font-weight: 600;
    }
    button.primary:hover { background: var(--accent-dark); }
    .theme-button { min-width: 92px; }
    .summary {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .metric {
      background: color-mix(in srgb, var(--panel) 92%, transparent);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px 16px;
      box-shadow: var(--shadow);
    }
    .metric-label { color: var(--muted); font-size: 12px; }
    .metric-value { margin-top: 5px; font-size: 24px; font-weight: 760; }
    .table-shell {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      overflow: hidden;
      box-shadow: var(--shadow);
    }
    .table-shell + .table-shell { margin-top: 16px; }
    .table-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 15px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-2);
    }
    .table-title { font-weight: 700; }
    #status { min-height: 20px; color: var(--muted); }
    #status.error { color: var(--danger); }
    #status.ok { color: var(--ok); }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    th, td {
      padding: 14px 16px;
      text-align: left;
      vertical-align: middle;
      border-bottom: 1px solid var(--line);
    }
    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      text-transform: uppercase;
      background: color-mix(in srgb, var(--panel-2) 86%, var(--panel));
    }
    tbody tr { transition: background .16s ease; }
    tbody tr:hover { background: color-mix(in srgb, var(--accent) 7%, transparent); }
    tr:last-child td { border-bottom: 0; }
    .provider-main {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 220px;
    }
    .provider-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--muted);
      box-shadow: 0 0 0 4px color-mix(in srgb, var(--muted) 16%, transparent);
      flex: 0 0 auto;
    }
    tr.is-enabled .provider-dot {
      background: var(--ok);
      box-shadow: 0 0 0 4px color-mix(in srgb, var(--ok) 18%, transparent);
    }
    .name { font-weight: 700; color: var(--text); }
    .subtle { color: var(--muted); font-size: 12px; margin-top: 2px; word-break: break-all; }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 9px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--soft-text);
      font-size: 12px;
      font-weight: 650;
      white-space: nowrap;
    }
    .pill.on {
      color: var(--ok);
      border-color: color-mix(in srgb, var(--ok) 30%, var(--line));
      background: color-mix(in srgb, var(--ok) 10%, var(--panel));
    }
    .pill.off {
      color: var(--warning);
      border-color: color-mix(in srgb, var(--warning) 28%, var(--line));
      background: color-mix(in srgb, var(--warning) 10%, var(--panel));
    }
    .models {
      max-width: 420px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .route-cell {
      min-width: 280px;
    }
    .route-stack {
      display: grid;
      gap: 8px;
      min-width: 260px;
    }
    .route-line {
      display: grid;
      grid-template-columns: minmax(120px, 1fr) minmax(120px, 180px) 56px;
      align-items: center;
      gap: 10px;
    }
    .route-provider {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--soft-text);
      font-weight: 650;
    }
    .route-bar {
      height: 8px;
      border-radius: 999px;
      background: var(--bg-soft);
      overflow: hidden;
      border: 1px solid color-mix(in srgb, var(--line) 70%, transparent);
    }
    .route-fill {
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
    }
    .route-percent {
      color: var(--text);
      font-variant-numeric: tabular-nums;
      text-align: right;
    }
    .route-note {
      color: var(--muted);
      font-size: 12px;
    }
    input[type="number"] {
      width: 104px;
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 0 8px;
      font: inherit;
      color: var(--text);
      background: var(--panel-2);
    }
    label.switch {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      color: var(--soft-text);
      white-space: nowrap;
    }
    .switch input {
      appearance: none;
      width: 42px;
      height: 24px;
      margin: 0;
      border-radius: 999px;
      border: 1px solid var(--line-strong);
      background: var(--bg-soft);
      position: relative;
      cursor: pointer;
      transition: background .18s ease, border-color .18s ease;
    }
    .switch input::after {
      content: "";
      position: absolute;
      width: 18px;
      height: 18px;
      top: 2px;
      left: 2px;
      border-radius: 50%;
      background: var(--panel);
      box-shadow: 0 2px 7px rgba(0, 0, 0, .22);
      transition: transform .18s ease;
    }
    .switch input:checked {
      background: var(--accent);
      border-color: var(--accent);
    }
    .switch input:checked::after { transform: translateX(18px); }
    @media (max-width: 760px) {
      header { padding: 14px 16px 0; }
      .topbar { align-items: flex-start; flex-direction: column; }
      .toolbar { width: 100%; justify-content: flex-start; }
      main { padding: 16px; }
      .summary { grid-template-columns: 1fr; }
      table, thead, tbody, th, td, tr { display: block; }
      thead { display: none; }
      tr { border-bottom: 1px solid var(--line); }
      td { border-bottom: 0; padding: 9px 14px; }
      td::before {
        content: attr(data-label);
        display: block;
        color: var(--muted);
        font-size: 12px;
        margin-bottom: 3px;
      }
      .provider-main { min-width: 0; }
      .models { max-width: none; }
      .route-cell { min-width: 0; }
      .route-stack { min-width: 0; }
      .route-line { grid-template-columns: 1fr; gap: 5px; }
      .route-percent { text-align: left; }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div class="brand">
        <div class="mark">LB</div>
        <div>
          <h1>codex-shift 控制台</h1>
          <div class="subtitle">Provider 开关、权重和热加载</div>
        </div>
      </div>
      <div class="toolbar">
        <button id="theme" class="theme-button" title="切换浅色/暗色模式">深色</button>
        <button id="refresh">刷新</button>
        <button id="save" class="primary">保存并热加载</button>
      </div>
    </div>
  </header>
  <main>
    <section class="summary" aria-label="Provider 概览">
      <div class="metric">
        <div class="metric-label">总数</div>
        <div class="metric-value" id="metric-total">0</div>
      </div>
      <div class="metric">
        <div class="metric-label">已启用</div>
        <div class="metric-value" id="metric-enabled">0</div>
      </div>
      <div class="metric">
        <div class="metric-label">总权重</div>
        <div class="metric-value" id="metric-weight">0</div>
      </div>
    </section>
    <section class="table-shell">
      <div class="table-head">
        <div class="table-title">Provider 路由池</div>
        <div id="status"></div>
      </div>
      <table>
        <thead>
          <tr>
            <th>Provider</th>
            <th>状态</th>
            <th>启用</th>
            <th>权重</th>
            <th>协议</th>
            <th>模型</th>
          </tr>
        </thead>
        <tbody id="providers"></tbody>
      </table>
    </section>
    <section class="table-shell">
      <div class="table-head">
        <div class="table-title">模型路由表</div>
        <div class="route-note">根据当前开关与权重实时计算</div>
      </div>
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
    </section>
  </main>
  <script>
    const tbody = document.getElementById("providers");
    const routesBody = document.getElementById("routes");
    const statusEl = document.getElementById("status");
    const themeButton = document.getElementById("theme");
    const totals = {
      total: document.getElementById("metric-total"),
      enabled: document.getElementById("metric-enabled"),
      weight: document.getElementById("metric-weight"),
    };

    function preferredTheme() {
      const saved = localStorage.getItem("codex_shift_theme");
      if (saved) return saved;
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }

    function applyTheme(theme) {
      document.documentElement.dataset.theme = theme;
      themeButton.textContent = theme === "dark" ? "浅色" : "深色";
      localStorage.setItem("codex_shift_theme", theme);
    }

    function setStatus(text, error = false) {
      statusEl.textContent = text;
      statusEl.className = error ? "error" : (text.includes("成功") ? "ok" : "");
    }

    function updateMetrics(providers) {
      const enabled = providers.filter((p) => p.enabled);
      totals.total.textContent = providers.length;
      totals.enabled.textContent = enabled.length;
      totals.weight.textContent = enabled.reduce((sum, p) => sum + Number(p.weight || 0), 0).toFixed(1).replace(/\\.0$/, "");
    }

    function formatNumber(value) {
      return Number(value || 0).toFixed(1).replace(/\\.0$/, "");
    }

    function routeRows(providers) {
      const models = new Map();
      for (const provider of providers) {
        for (const model of provider.models || []) {
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
          return { model, active, positive, totalWeight };
        });
    }

    function renderRoutes(providers) {
      routesBody.innerHTML = "";
      const rows = routeRows(providers);
      if (!rows.length) {
        routesBody.innerHTML = `<tr><td data-label="模型" colspan="4" class="route-note">暂无模型</td></tr>`;
        return;
      }
      for (const row of rows) {
        const tr = document.createElement("tr");
        const providerCount = row.active.length;
        const fallbackProvider = row.active[0]?.name;
        const routeHtml = row.active.length
          ? row.active.map((p) => {
              let probability = 0;
              if (p.enabled && row.totalWeight > 0) {
                probability = Number(p.weight || 0) > 0 ? Number(p.weight || 0) / row.totalWeight * 100 : 0;
              } else if (p.enabled && row.totalWeight <= 0 && p.name === fallbackProvider) {
                probability = 100;
              }
              return `
                <div class="route-line">
                  <div class="route-provider" title="${p.name}">${p.name}</div>
                  <div class="route-bar" aria-hidden="true"><div class="route-fill" style="width: ${probability}%"></div></div>
                  <div class="route-percent">${probability.toFixed(1).replace(/\\.0$/, "")}%</div>
                </div>
              `;
            }).join("")
          : `<div class="route-note">无启用 provider</div>`;
        tr.innerHTML = `
          <td data-label="模型"><div class="name">${row.model}</div></td>
          <td data-label="启用 Provider"><span class="pill ${providerCount ? "on" : "off"}">${providerCount}</span></td>
          <td data-label="总权重">${formatNumber(row.totalWeight)}</td>
          <td data-label="路由概率" class="route-cell"><div class="route-stack">${routeHtml}</div></td>
        `;
        routesBody.appendChild(tr);
      }
    }

    function render(providers) {
      tbody.innerHTML = "";
      updateMetrics(providers);
      renderRoutes(providers);
      for (const p of providers) {
        const tr = document.createElement("tr");
        tr.dataset.name = p.name;
        tr.className = p.enabled ? "is-enabled" : "";
        tr.innerHTML = `
          <td data-label="Provider">
            <div class="provider-main">
              <span class="provider-dot"></span>
              <div>
                <div class="name">${p.name}</div>
                <div class="subtle">${p.base_url}${p.path}</div>
              </div>
            </div>
          </td>
          <td data-label="状态">
            <span class="pill ${p.enabled ? "on" : "off"}">${p.enabled ? "运行中" : "已停用"}</span>
          </td>
          <td data-label="启用">
            <label class="switch"><input type="checkbox" class="enabled" ${p.enabled ? "checked" : ""}><span>${p.enabled ? "开启" : "关闭"}</span></label>
          </td>
          <td data-label="权重">
            <input type="number" class="weight" min="0" step="0.1" value="${p.weight}">
          </td>
          <td data-label="协议"><span class="pill">${p.outbound}</span></td>
          <td data-label="模型" class="models">${(p.models || []).join(", ") || "(兜底)"}</td>
        `;
        const checkbox = tr.querySelector(".enabled");
        const labelText = tr.querySelector(".switch span");
        checkbox.addEventListener("change", () => {
          tr.className = checkbox.checked ? "is-enabled" : "";
          labelText.textContent = checkbox.checked ? "开启" : "关闭";
          tr.querySelector(".pill").className = `pill ${checkbox.checked ? "on" : "off"}`;
          tr.querySelector(".pill").textContent = checkbox.checked ? "运行中" : "已停用";
          const current = [...tbody.querySelectorAll("tr")].map(readRow);
          updateMetrics(current);
          renderRoutes(mergeControls(current));
        });
        tr.querySelector(".weight").addEventListener("input", () => {
          const current = [...tbody.querySelectorAll("tr")].map(readRow);
          updateMetrics(current);
          renderRoutes(mergeControls(current));
        });
        tbody.appendChild(tr);
      }
    }

    let loadedProviders = [];

    function mergeControls(controls) {
      const byName = new Map(controls.map((p) => [p.name, p]));
      return loadedProviders.map((provider) => ({
        ...provider,
        ...(byName.get(provider.name) || {}),
      }));
    }

    function readRow(tr) {
      return {
        name: tr.dataset.name,
        enabled: tr.querySelector(".enabled").checked,
        weight: Number(tr.querySelector(".weight").value),
      };
    }

    async function load() {
      setStatus("正在读取配置...");
      const resp = await fetch("/admin/api/config");
      if (!resp.ok) throw new Error(await resp.text());
      const data = await resp.json();
      loadedProviders = data.providers;
      render(data.providers);
      setStatus(`已加载 ${data.providers.length} 个 provider`);
    }

    async function save() {
      const providers = [...tbody.querySelectorAll("tr")].map(readRow);
      setStatus("正在保存并热加载...");
      const resp = await fetch("/admin/api/config", {
        method: "POST",
        headers: {"content-type": "application/json"},
        body: JSON.stringify({providers}),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.error?.message || await resp.text());
      }
      const data = await resp.json();
      loadedProviders = data.providers;
      render(data.providers);
      setStatus("保存成功, 新配置已生效");
    }

    applyTheme(preferredTheme());
    themeButton.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(next);
    });
    document.getElementById("refresh").addEventListener("click", () => load().catch((e) => setStatus(e.message, true)));
    document.getElementById("save").addEventListener("click", () => save().catch((e) => setStatus(e.message, true)));
    load().catch((e) => setStatus(e.message, true));
  </script>
</body>
</html>
"""
