<div align="center">

# codex-shift

**让 Codex 接入任意支持 chat/completions 协议及 responses 协议的 llm api**

[快速开始](#快速开始) | [用户手册](docs/user-manual/zh/README.md) | [配置说明](docs/user-manual/zh/2-configuration/2.1-provider-config.md) | [Codex 集成](docs/user-manual/zh/4-integrations/4.1-codex.md) | [FAQ](docs/user-manual/zh/6-faq/6.1-questions.md)

</div>

---

## 为什么需要 codex-shift

Codex 使用 Responses 协议工作，但很多模型供应商仍以 Chat Completions 或自定义 Responses 网关提供服务。`codex-shift` 提供一个本地适配层，把这些上游统一成 Codex 可直接使用的 Responses 入口，并补齐模型目录、上下文窗口和 provider 路由能力。

它支持这些功能：

- **responses 协议转换**：将 chat/completions 协议和 responses 协议的 api 统一转换为 `POST /v1/responses`。
- **多 Provider 路由**：同一配置中管理 DeepSeek、Qwen、MiMo、内部网关等上游，并按模型名自动选择 provider。
- **Codex 模型目录**：通过实现 responses 协议的 `/models`接口 暴露 Codex 兼容的 `ModelInfo`，让自定义模型获得真实上下文窗口，比如开启 1M 上下文窗口，而不是 codex 内置的 272k。
- **本地热管理**：内置本地控制台，通过 `/admin`进入控制台，图形界面化启停 provider、调整权重并保存配置，不需要重启进程。
- **错误语义统一**：上游超时、非法 JSON、API 错误会转换为 Responses 风格错误响应。
- **纯本地转换**：轻量，简洁的本地转换，不做路由和转换以外的多余动作，性能几乎无损。
- **模型别名映射**：例如把 mimo-v2.5-pro 映射为 gpt-5.5，如果你在使用 codex 时，希望特定场景只用特定供应商的特定模型，这个映射功能将派上用场。

## 核心能力

### 协议转换与透传

- `chat_completions` 出站：将 OpenAI Responses 请求转换为 Chat Completions，并把响应转换回 Responses。
- `responses` 出站：上游同为 Responses 协议时进入全透传模式，仅注入 API Key，并保留 `/models` 上下文代理能力。
- 支持流式与非流式请求，流式 SSE 会转换为 Responses 事件序列。

### 路由与模型目录

- `model_map` 支持入站模型名到上游模型名的映射。
- 多个 provider 声明同一模型时，按 `weight` 做加权选择。
- `/models` 基于内置 Codex `ModelInfo` 模板克隆条目，只覆盖 slug 与上下文窗口等元数据。

### 工具与推理参数

- 支持 function tool 双向映射，包含 Codex namespace 展平与回程还原。
- Chat 出站可按 provider 配置透传 `web_search`。
- `reasoning.effort` 会在 Chat 出站时映射为 `reasoning_effort`，Responses 透传时保持原样。

## 快速开始

### 一键启动

```bash
git clone git@github.com:ahtcfg24/codex-shift.git
cd codex-shift

cp .env.example .env
$EDITOR .env

./start.sh
```

`start.sh` 会自动准备虚拟环境、安装依赖、复制 `config.yaml`、加载项目 `.env`、处理端口占用并在后台启动服务。启动完成后访问 `GET /health` 验证服务状态。

### 常用命令

```bash
./start.sh                    # 启动服务
./start.sh --log-level debug  # 启动并输出 debug 日志
./start.sh status             # 查看运行状态
./start.sh restart            # 重启服务
./start.sh stop               # 停止服务
```

默认监听地址来自 `config.yaml` 的 `server.host` 与 `server.port`。示例配置使用 `127.0.0.1:8080`，本地控制台地址为：

```text
http://127.0.0.1:8080/admin
```

如果 `start.sh` 因端口占用自动选择了新端口，请使用启动输出中的实际端口访问控制台。

## 最小请求示例

```bash
curl http://127.0.0.1:8080/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gpt-5.5",
    "input": [{"role": "user", "content": "Hello!"}],
    "stream": false
  }'
```

## 文档导航

| 文档 | 内容 |
|------|------|
| [用户手册](docs/user-manual/zh/README.md) | 从安装、配置到运行维护的完整目录 |
| [软件介绍](docs/user-manual/zh/1-getting-started/1.1-introduction.md) | 项目定位、核心概念与工作流程 |
| [安装与启动](docs/user-manual/zh/1-getting-started/1.2-installation.md) | 环境要求、启动脚本、健康检查 |
| [Provider 配置](docs/user-manual/zh/2-configuration/2.1-provider-config.md) | `config.yaml` 结构、字段说明与路由规则 |
| [API 端点](docs/user-manual/zh/3-api/3.1-endpoints.md) | `/v1/responses`、`/models`、`/health`、`/admin` |
| [Codex 集成](docs/user-manual/zh/4-integrations/4.1-codex.md) | 让 Codex 读取本代理的模型目录与上下文窗口 |
| [运行维护](docs/user-manual/zh/5-operations/5.1-admin-and-runtime.md) | 控制台、日志、端口与排错 |
| [FAQ](docs/user-manual/zh/6-faq/6.1-questions.md) | 常见问题、限制与安全提示 |

## 项目结构

```text
codex_shift/
├── codex_shift/              # FastAPI 服务、配置加载、协议转换与上游转发
├── tests/                    # 单元测试与协议转换测试
├── docs/user-manual/zh/      # 中文用户手册
├── config.example.yaml       # 配置示例
├── .env.example              # 环境变量示例
└── start.sh                  # 本地启动与守护脚本
```

## 开发与测试

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install pytest
pytest
```

开发配置请从 `config.example.yaml` 复制为 `config.yaml`，真实 API Key 建议写入 `.env`。`config.yaml` 与 `.env` 已在 `.gitignore` 中排除，请不要提交真实密钥。

## 贡献

欢迎通过 Issue 或 Pull Request 改进文档、协议转换兼容性、测试覆盖和 provider 示例。详细流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。提交前请确保：

- 不提交真实 API Key、内部域名或敏感配置。
- 新增协议行为时同步更新用户手册与测试。
- 文档中的命令、路径和端点已在本仓库中验证。

## License

[Apache-2.0](LICENSE)
