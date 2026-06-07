# Contributing to codex-shift

感谢你愿意改进 `codex-shift`。本项目关注 Codex 的 Responses 协议适配、provider 路由和 Codex 兼容模型目录，欢迎提交文档、测试、兼容性修复和新 provider 示例。

## 开发环境

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install pytest
```

启动本地服务：

```bash
cp .env.example .env
cp config.example.yaml config.yaml
./start.sh
```

## 提交前检查

```bash
pytest
```

如果改动了启动脚本、配置字段、API 端点或协议行为，请同步更新：

- `README.md`
- `docs/user-manual/zh/` 下对应章节
- `config.example.yaml` 中相关注释

## 贡献范围

适合提交的改动：

- 修复 Responses 与 Chat Completions 转换中的兼容性问题。
- 增加或完善协议转换测试。
- 改进 provider 配置示例。
- 改进 Codex `/models` 兼容性。
- 修正文档中不准确或过期的说明。

不建议在同一个 PR 中混合大范围重构与行为改动。协议语义变更应附带测试和文档说明。

## 安全要求

- 不要提交真实 API Key、内部域名、账号信息或请求日志。
- 不要提交本地 `config.yaml`、`.env`、`.codex-shift.log` 或 `.codex-shift.pid`。
- debug 日志可能包含请求体和上游响应，提交 issue 时请先脱敏。

## 文档风格

- 优先使用可验证的命令、路径和端点。
- 不夸大尚未实现的能力。
- 新增配置项时说明默认值、适用出站协议和安全影响。
- 用户手册使用中文章节编号，根 README 保持简洁入口定位。
