"""codex_shift: Codex Responses 协议适配层。

面向 Codex 暴露 OpenAI Responses 入站协议,支持同时配置多个 provider,
按入站 model 名路由到上游,并按各 provider 的出站协议
(OpenAI Chat Completions 或 OpenAI Responses 透传)转发。
"""

__version__ = "0.1.0"
