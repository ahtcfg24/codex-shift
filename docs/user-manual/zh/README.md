# codex-shift 用户手册

> 面向 Codex 的本地 Responses 协议适配层：统一接入多个上游 provider，并为 Codex 暴露模型目录与上下文窗口。

## 目录结构

```text
📚 codex-shift 用户手册
│
├── 1. 快速入门
│   ├── 1.1 软件介绍
│   └── 1.2 安装与启动
│
├── 2. 配置管理
│   └── 2.1 Provider 配置
│
├── 3. API 接口
│   └── 3.1 端点说明
│
├── 4. 客户端集成
│   └── 4.1 Codex 集成
│
├── 5. 运行维护
│   └── 5.1 控制台与运行状态
│
└── 6. 常见问题
    └── 6.1 FAQ
```

## 文件列表

### 1. 快速入门

| 文件 | 内容 |
|------|------|
| [1.1-introduction.md](./1-getting-started/1.1-introduction.md) | 项目定位、解决的问题、核心能力 |
| [1.2-installation.md](./1-getting-started/1.2-installation.md) | 环境要求、启动脚本、健康检查 |

### 2. 配置管理

| 文件 | 内容 |
|------|------|
| [2.1-provider-config.md](./2-configuration/2.1-provider-config.md) | `config.yaml` 结构、provider 字段、路由规则 |

### 3. API 接口

| 文件 | 内容 |
|------|------|
| [3.1-endpoints.md](./3-api/3.1-endpoints.md) | Responses 入站、模型目录、健康检查和控制台端点 |

### 4. 客户端集成

| 文件 | 内容 |
|------|------|
| [4.1-codex.md](./4-integrations/4.1-codex.md) | Codex `config.toml` 示例、`/models` 触发条件与缓存处理 |

### 5. 运行维护

| 文件 | 内容 |
|------|------|
| [5.1-admin-and-runtime.md](./5-operations/5.1-admin-and-runtime.md) | `/admin` 控制台、日志文件、端口冲突与排错 |

### 6. 常见问题

| 文件 | 内容 |
|------|------|
| [6.1-questions.md](./6-faq/6.1-questions.md) | 常见问题、已知限制、安全建议 |

## 快速链接

- **第一次使用**：从 [1.1 软件介绍](./1-getting-started/1.1-introduction.md) 开始。
- **需要启动服务**：查看 [1.2 安装与启动](./1-getting-started/1.2-installation.md)。
- **需要添加上游**：查看 [2.1 Provider 配置](./2-configuration/2.1-provider-config.md)。
- **需要接入 Codex**：查看 [4.1 Codex 集成](./4-integrations/4.1-codex.md)。
- **遇到问题**：查看 [6.1 FAQ](./6-faq/6.1-questions.md)。

## 版本信息

- 文档版本：v0.1
- 最后更新：2026-06-07
- 适用于当前 `codex-shift` 源码树

## 贡献

如果你改动了协议转换、配置字段、启动方式或 API 端点，请同步更新本手册与根目录 `README.md` 中对应链接。
