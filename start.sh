#!/usr/bin/env bash
# codex-shift 一键本地启动脚本(系统 service)
#
# 默认行为(start):
#   1. 生成/更新当前项目的 launchd 或 systemd user service
#   2. 启用 service 自启动(macOS 为用户登录后自启,Linux 会尝试启用 linger)
#   3. 准备虚拟环境、依赖、config.yaml 和 .env
#   4. 停止已有实例并检查端口占用
#   5. 通过 service 启动前台进程,并轮询 /health 确认就绪
#
# 子命令:
#   ./start.sh [启动参数...]      启动/重启 service(默认),额外参数透传给服务,如 --log-level debug
#   ./start.sh stop               停止当前 service 实例(自启配置保留)
#   ./start.sh status             查看 service 运行状态
#   ./start.sh restart [参数]     重启 service
#   ./start.sh run [启动参数...]  内部前台入口,供 service manager 调用

set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
cd "$ROOT_DIR"

VENV_DIR=".venv"
PY="$VENV_DIR/bin/python"
PYTHON_VERSION="3.12"
PID_FILE=".codex-shift.pid"
LOG_FILE=".codex-shift.log"
RUNNER_FILE=".codex-shift-service-runner.sh"
SERVICE_ID="$(printf '%s' "$ROOT_DIR" | cksum | awk '{print $1}')"
SERVICE_LABEL="com.codex-shift.$SERVICE_ID"
SERVICE_UNIT="codex-shift-$SERVICE_ID.service"
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
    # 等待刚被杀掉的进程释放端口（TIME_WAIT），最多等 5 秒
    import time as _time
    for _ in range(10):
        _time.sleep(0.5)
        if can_bind(target_port):
            break

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

# --- 准备 Python 环境 ---
ensure_python_env() {
  if [ -x "$PY" ]; then
    return
  fi

  echo "[start] 未发现虚拟环境,正在创建 $VENV_DIR ..."
  if command -v uv >/dev/null 2>&1; then
    uv venv --python "$PYTHON_VERSION" "$VENV_DIR"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -m venv "$VENV_DIR"
  else
    echo "[start] 错误: 未找到 uv 或 python3,无法创建虚拟环境" >&2
    exit 1
  fi
}

# --- 安装运行依赖 ---
ensure_dependencies() {
  if "$PY" -c "import fastapi, uvicorn, httpx, yaml" >/dev/null 2>&1; then
    return
  fi

  echo "[start] 正在安装依赖 ..."
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PY" -r requirements.txt
  else
    "$PY" -m pip install -r requirements.txt
  fi
}

# --- 准备配置文件 ---
ensure_config() {
  if [ -f config.yaml ]; then
    return
  fi

  echo "[start] 未发现 config.yaml,已从 config.example.yaml 复制(请按需修改)"
  cp config.example.yaml config.yaml
}

# --- 准备运行时并计算健康检查地址 ---
prepare_runtime() {
  ensure_python_env
  ensure_dependencies
  ensure_config
  load_env

  read HOST PORT KEY_EMPTY < <(read_config)
  if [ "$KEY_EMPTY" = "1" ]; then
    echo "[start] 警告: 上游 API Key 为空,上游可能返回 401。" \
         "请在 config.yaml 设置 api_key,或导出对应环境变量(如 DEEPSEEK_API_KEY)。"
  fi

  PORT_SELECTION="$(select_port "$HOST" "$PORT")"
  read SELECTED_PORT PORT_CHANGED PORT_SEARCH_START <<<"$PORT_SELECTION"
  if [ "$PORT_CHANGED" = "1" ]; then
    echo "[start] 端口 $PORT 已被占用,已自动选择 $SELECTED_PORT 并写回 config.yaml"
    echo "[start] 端口搜索起点: $PORT_SEARCH_START"
    PORT="$SELECTED_PORT"
  fi

  # 0.0.0.0 只用于监听;本机探活固定走 127.0.0.1。
  PROBE_HOST="$HOST"
  if [ "$PROBE_HOST" = "0.0.0.0" ]; then
    PROBE_HOST="127.0.0.1"
  fi
}

# --- 识别当前系统可用的 service manager ---
detect_service_backend() {
  case "$(uname -s)" in
    Darwin)
      if ! command -v launchctl >/dev/null 2>&1; then
        echo "[start] 错误: 当前系统未找到 launchctl" >&2
        exit 1
      fi
      echo "launchd"
      ;;
    Linux)
      if ! command -v systemctl >/dev/null 2>&1; then
        echo "[start] 错误: 当前系统未找到 systemctl" >&2
        exit 1
      fi
      echo "systemd"
      ;;
    *)
      echo "[start] 错误: 当前系统暂不支持自动 service 启动" >&2
      exit 1
      ;;
  esac
}

# --- 为 launchd plist 转义 XML 文本 ---
xml_escape() {
  local value="$1"
  value="${value//&/&amp;}"
  value="${value//</&lt;}"
  value="${value//>/&gt;}"
  value="${value//\"/&quot;}"
  value="${value//\'/&apos;}"
  printf '%s' "$value"
}

# --- 为 systemd unit 的带引号字段转义 ---
systemd_escape_value() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "$value"
}

launchd_domain() {
  echo "gui/$(id -u)"
}

launchd_target() {
  echo "$(launchd_domain)/$SERVICE_LABEL"
}

launchd_plist_path() {
  echo "$HOME/Library/LaunchAgents/$SERVICE_LABEL.plist"
}

systemd_unit_path() {
  echo "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$SERVICE_UNIT"
}

# --- 生成 service 调用的前台 runner,用于保存启动参数和统一日志 ---
write_service_runner() {
  {
    echo "#!/usr/bin/env bash"
    echo "set -euo pipefail"
    printf 'cd %q\n' "$ROOT_DIR"
    printf 'exec %q run' "$ROOT_DIR/start.sh"
    for arg in "$@"; do
      printf ' %q' "$arg"
    done
    printf ' >> %q 2>&1\n' "$ROOT_DIR/$LOG_FILE"
  } > "$RUNNER_FILE"
  chmod +x "$RUNNER_FILE"
}

# --- 写入 macOS LaunchAgent 配置 ---
install_launchd_service() {
  local plist
  plist="$(launchd_plist_path)"
  mkdir -p "$(dirname "$plist")"

  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$(xml_escape "$SERVICE_LABEL")</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$(xml_escape "$ROOT_DIR/$RUNNER_FILE")</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$(xml_escape "$ROOT_DIR")</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
  <key>StandardOutPath</key>
  <string>$(xml_escape "$ROOT_DIR/$LOG_FILE")</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "$ROOT_DIR/$LOG_FILE")</string>
</dict>
</plist>
EOF
}

# --- 写入 Linux systemd user service 配置 ---
install_systemd_service() {
  local unit
  unit="$(systemd_unit_path)"
  mkdir -p "$(dirname "$unit")"

  cat > "$unit" <<EOF
[Unit]
Description=codex-shift local proxy ($ROOT_DIR)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory="$(systemd_escape_value "$ROOT_DIR")"
ExecStart=/bin/bash "$(systemd_escape_value "$ROOT_DIR/$RUNNER_FILE")"
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
}

# --- Linux 用户服务需要 linger 才能在无登录会话时随系统启动 ---
enable_systemd_linger() {
  if ! command -v loginctl >/dev/null 2>&1 || [ -z "${USER:-}" ]; then
    return
  fi

  if loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q "Linger=no"; then
    if loginctl enable-linger "$USER" >/dev/null 2>&1; then
      echo "[start] 已为用户 $USER 启用 systemd linger"
    else
      echo "[start] 警告: 无法自动启用 systemd linger;重启后可能需要登录用户会话才会启动" >&2
    fi
  fi
}

# --- 根据系统类型安装或更新 service 文件 ---
install_service() {
  local backend="$1"
  case "$backend" in
    launchd)
      install_launchd_service
      ;;
    systemd)
      install_systemd_service
      ;;
  esac
}

# --- 停止当前 service 实例,但保留自启动配置 ---
stop_service_backend() {
  local backend="$1"
  case "$backend" in
    launchd)
      launchctl bootout "$(launchd_target)" >/dev/null 2>&1 || true
      ;;
    systemd)
      systemctl --user stop "$SERVICE_UNIT" >/dev/null 2>&1 || true
      ;;
  esac
}

# --- 启动并启用 service 自启动 ---
start_service_backend() {
  local backend="$1"
  case "$backend" in
    launchd)
      launchctl bootstrap "$(launchd_domain)" "$(launchd_plist_path)"
      launchctl enable "$(launchd_target)" >/dev/null 2>&1 || true
      launchctl kickstart -k "$(launchd_target)" >/dev/null 2>&1 || true
      ;;
    systemd)
      systemctl --user enable "$SERVICE_UNIT" >/dev/null
      enable_systemd_linger
      systemctl --user restart "$SERVICE_UNIT"
      ;;
  esac
}

# --- 等待 HTTP 健康检查就绪 ---
wait_for_health() {
  local ready=0
  for _ in $(seq 1 30); do
    if curl -s "http://$PROBE_HOST:$PORT/health" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 0.5
  done

  if [ "$ready" = "1" ]; then
    echo "[start] 已就绪,监听 http://$PROBE_HOST:$PORT"
    echo "[start] 控制台: http://$PROBE_HOST:$PORT/admin"
    echo "[start] 查看日志: tail -f $LOG_FILE   停止当前实例: ./start.sh stop"
    return
  fi

  echo "[start] 启动失败或未就绪,最近日志:" >&2
  tail -n 30 "$LOG_FILE" >&2 || true
  exit 1
}

# --- 兼容旧版 PID 文件状态展示 ---
legacy_status() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
    echo "[start] 旧版后台进程运行中, PID=$(cat "$PID_FILE")"
    return 0
  fi
  return 1
}

# --- 默认入口: 安装/重启 service 并确认健康状态 ---
start_managed_service() {
  local backend
  backend="$(detect_service_backend)"

  write_service_runner "$@"
  install_service "$backend"
  stop_service_backend "$backend"
  kill_existing
  prepare_runtime

  echo "[start] 通过 $backend service 启动 codex-shift (日志: $LOG_FILE) ..."
  start_service_backend "$backend"
  wait_for_health
}

# --- stop 子命令 ---
stop_managed_service() {
  local backend
  backend="$(detect_service_backend)"
  stop_service_backend "$backend"
  kill_existing
  echo "[start] 已停止当前 service 实例;自启动配置保留"
}

# --- status 子命令 ---
status_managed_service() {
  local backend
  backend="$(detect_service_backend)"

  case "$backend" in
    launchd)
      if launchctl print "$(launchd_target)" >/dev/null 2>&1; then
        if launchctl print "$(launchd_target)" 2>/dev/null | grep -q "state = running"; then
          echo "[start] service 运行中 ($SERVICE_LABEL)"
          return 0
        fi
        echo "[start] service 已安装但当前未运行 ($SERVICE_LABEL)"
        return 1
      fi
      ;;
    systemd)
      if systemctl --user is-active --quiet "$SERVICE_UNIT"; then
        echo "[start] service 运行中 ($SERVICE_UNIT)"
        return 0
      fi
      if systemctl --user is-enabled --quiet "$SERVICE_UNIT" 2>/dev/null; then
        echo "[start] service 已启用但当前未运行 ($SERVICE_UNIT)"
        return 1
      fi
      ;;
  esac

  if legacy_status; then
    return 0
  fi

  echo "[start] 未运行"
  return 1
}

# --- service manager 调用的前台入口 ---
run_foreground() {
  prepare_runtime
  echo "[start] 前台运行 codex-shift ..."
  exec "$PY" -m codex_shift --config config.yaml "$@"
}

case "${1:-}" in
  stop)
    stop_managed_service
    ;;
  status)
    status_managed_service
    ;;
  restart)
    shift
    start_managed_service "$@"
    ;;
  run)
    shift
    run_foreground "$@"
    ;;
  start)
    shift
    start_managed_service "$@"
    ;;
  *)
    start_managed_service "$@"
    ;;
esac
