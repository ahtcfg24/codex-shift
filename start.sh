#!/usr/bin/env bash
# codex-shift 一键本地启动脚本(后台守护进程)
#
# 默认行为(start):
#   1. 准备虚拟环境 .venv(优先 uv;否则回退 python3 -m venv)
#   2. 按需安装依赖(仅当检测到缺失时)
#   3. 缺少 config.yaml 时从 config.example.yaml 复制
#   4. 默认加载项目 .env(提供 DEEPSEEK_API_KEY 等)
#   5. 杀掉已有实例(PID 文件 + 命令行匹配)
#   6. 若配置端口被占用,自动从 10000 起寻找可用端口并写回 config.yaml
#   7. 后台启动新进程,并轮询 /health 确认就绪
#
# 子命令:
#   ./start.sh [启动参数...]   启动(默认),额外参数透传给服务,如 --log-level debug
#   ./start.sh stop            停止后台进程
#   ./start.sh status          查看运行状态
#   ./start.sh restart [参数]  重启

set -euo pipefail
cd "$(dirname "$0")"

VENV_DIR=".venv"
PY="$VENV_DIR/bin/python"
PYTHON_VERSION="3.12"
PID_FILE=".codex-shift.pid"
LOG_FILE=".codex-shift.log"
# 用于命令行匹配的进程特征(杀掉历史实例)
PROC_PATTERN="codex_shift --config"

# --- 加载 .env ---
load_env() {
  if [ ! -f .env ]; then
    echo "[start] 未发现 .env,将仅使用当前 shell 环境变量"
    return
  fi
  echo "[start] 加载 .env 中的环境变量"
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
}

# --- 杀掉已有实例 ---
kill_existing() {
  # 1) 优先按 PID 文件停止
  if [ -f "$PID_FILE" ]; then
    local oldpid
    oldpid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
      echo "[start] 停止已有进程 PID=$oldpid"
      kill "$oldpid" 2>/dev/null || true
      # 最多等待 5 秒优雅退出,否则强杀
      for _ in $(seq 1 10); do
        kill -0 "$oldpid" 2>/dev/null || break
        sleep 0.5
      done
      kill -9 "$oldpid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
  fi
  # 2) 兜底: 按命令行特征清理可能的残留实例
  if command -v pkill >/dev/null 2>&1; then
    pkill -f "$PROC_PATTERN" 2>/dev/null || true
  fi
}

# --- 读取配置中的 host/port 与 API Key 是否为空 ---
# 输出三个以空格分隔的值: HOST PORT KEY_EMPTY(1/0)
read_config() {
  "$PY" - <<'PYCODE' 2>/dev/null || echo "127.0.0.1 8080 0"
from codex_shift.config import load_config
try:
    c = load_config("config.yaml")
    # 任一 provider 的 api_key 为空即视为存在空 Key(便于提示)
    empty = "1" if any(not p.api_key for p in c.providers) else "0"
    print(c.host, c.port, empty)
except Exception:
    print("127.0.0.1 8080 0")
PYCODE
}

# --- 选择可用端口,必要时写回 config.yaml ---
# 输出三个以空格分隔的值: PORT CHANGED(1/0) SEARCH_START
select_port() {
  local host="$1"
  local port="$2"
  "$PY" - "$host" "$port" <<'PYCODE'
from __future__ import annotations

import socket
import sys
from pathlib import Path

import yaml

config_path = Path("config.yaml")
host = sys.argv[1]
target_port = int(sys.argv[2])


def can_bind(candidate: int) -> bool:
    try:
        infos = socket.getaddrinfo(host, candidate, type=socket.SOCK_STREAM)
    except socket.gaierror:
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, candidate))]

    for family, socktype, proto, _canonname, sockaddr in infos:
        with socket.socket(family, socktype, proto) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(sockaddr)
            except OSError:
                continue
            return True
    return False


def persist_port(selected: int) -> None:
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    server_line = None
    port_line = None

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0 and stripped.startswith("server:"):
            server_line = index
            continue
        if server_line is not None and indent == 0:
            break
        if server_line is not None and stripped.startswith("port:"):
            port_line = index
            break

    if server_line is not None and port_line is not None:
        old_line = lines[port_line]
        newline = "\n" if old_line.endswith("\n") else ""
        body = old_line[:-1] if newline else old_line
        comment = ""
        if "#" in body:
            body, comment = body.split("#", 1)
            comment = " #" + comment
        indent_text = body[: len(body) - len(body.lstrip(" "))]
        lines[port_line] = f"{indent_text}port: {selected}{comment}{newline}"
        config_path.write_text("".join(lines), encoding="utf-8")
        return

    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        data = {}
    server = data.setdefault("server", {})
    if not isinstance(server, dict):
        server = {}
        data["server"] = server
    server["port"] = selected
    with config_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


search_start = 10000 if target_port < 10000 else target_port + 1
selected_port = target_port
changed = 0

if not can_bind(target_port):
    for candidate in range(search_start, 65536):
        if can_bind(candidate):
            selected_port = candidate
            changed = 1
            persist_port(selected_port)
            break
    else:
        raise SystemExit("未找到可用端口: 已扫描到 65535")

print(selected_port, changed, search_start)
PYCODE
}

# --- stop 子命令 ---
if [ "${1:-}" = "stop" ]; then
  kill_existing
  echo "[start] 已停止"
  exit 0
fi

# --- status 子命令 ---
if [ "${1:-}" = "status" ]; then
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
    echo "[start] 运行中, PID=$(cat "$PID_FILE")"
    exit 0
  fi
  echo "[start] 未运行"
  exit 1
fi

# --- restart: 等价于杀掉后继续走 start 流程 ---
if [ "${1:-}" = "restart" ]; then
  shift
fi

# === 以下为 start 流程 ===

# --- 1. 准备虚拟环境 ---
if [ ! -x "$PY" ]; then
  echo "[start] 未发现虚拟环境,正在创建 $VENV_DIR ..."
  if command -v uv >/dev/null 2>&1; then
    uv venv --python "$PYTHON_VERSION" "$VENV_DIR"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -m venv "$VENV_DIR"
  else
    echo "[start] 错误: 未找到 uv 或 python3,无法创建虚拟环境" >&2
    exit 1
  fi
fi

# --- 2. 安装依赖(仅当关键依赖缺失时) ---
if ! "$PY" -c "import fastapi, uvicorn, httpx, yaml" >/dev/null 2>&1; then
  echo "[start] 正在安装依赖 ..."
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PY" -r requirements.txt
  else
    "$PY" -m pip install -r requirements.txt
  fi
fi

# --- 3. 准备配置文件 ---
if [ ! -f config.yaml ]; then
  echo "[start] 未发现 config.yaml,已从 config.example.yaml 复制(请按需修改)"
  cp config.example.yaml config.yaml
fi

# --- 4. 加载 .env(若存在) ---
load_env

# --- 5. 读取配置并校验 API Key ---
read HOST PORT KEY_EMPTY < <(read_config)
if [ "$KEY_EMPTY" = "1" ]; then
  echo "[start] 警告: 上游 API Key 为空,上游可能返回 401。" \
       "请在 config.yaml 设置 api_key,或导出对应环境变量(如 DEEPSEEK_API_KEY)。"
fi

# --- 6. 杀掉已有实例并确认监听端口 ---
kill_existing
PORT_SELECTION="$(select_port "$HOST" "$PORT")"
read SELECTED_PORT PORT_CHANGED PORT_SEARCH_START <<<"$PORT_SELECTION"
if [ "$PORT_CHANGED" = "1" ]; then
  echo "[start] 端口 $PORT 已被占用,已自动选择 $SELECTED_PORT 并写回 config.yaml"
  echo "[start] 端口搜索起点: $PORT_SEARCH_START"
  PORT="$SELECTED_PORT"
fi
# 探活地址: 0.0.0.0 监听时用 127.0.0.1 探测
PROBE_HOST="$HOST"
[ "$PROBE_HOST" = "0.0.0.0" ] && PROBE_HOST="127.0.0.1"

# --- 7. 后台启动 ---
echo "[start] 后台启动 codex-shift (日志: $LOG_FILE) ..."
# nohup 脱离终端;输出追加到日志文件
nohup "$PY" -m codex_shift --config config.yaml "$@" >>"$LOG_FILE" 2>&1 &
NEWPID=$!
echo "$NEWPID" > "$PID_FILE"

# --- 轮询 /health 确认就绪(最多 ~15s) ---
ready=0
for _ in $(seq 1 30); do
  # 进程若已退出则立即报错
  if ! kill -0 "$NEWPID" 2>/dev/null; then
    break
  fi
  if curl -s "http://$PROBE_HOST:$PORT/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.5
done

if [ "$ready" = "1" ]; then
  echo "[start] 已就绪 PID=$NEWPID, 监听 http://$PROBE_HOST:$PORT"
  echo "[start] 控制台: http://$PROBE_HOST:$PORT/admin"
  echo "[start] 查看日志: tail -f $LOG_FILE   停止: ./start.sh stop"
else
  echo "[start] 启动失败或未就绪,最近日志:" >&2
  tail -n 30 "$LOG_FILE" >&2 || true
  rm -f "$PID_FILE"
  exit 1
fi
