"""转换层共享工具函数。

集中放置 ID 生成、时间戳、Responses 输入项归一化等被多个转换模块复用的逻辑。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

# Responses input 中被视为"消息"的角色集合
MESSAGE_ROLES = {"user", "assistant", "system", "developer"}
# 归类为系统指令的角色(Chat 中保留为 system)
SYSTEM_ROLES = {"system", "developer"}


def new_id(prefix: str) -> str:
    """生成带前缀的唯一 ID,用于构造 Responses 输出对象。"""
    return f"{prefix}_{uuid.uuid4().hex}"


def now_ts() -> int:
    """当前 Unix 时间戳(秒)。"""
    return int(time.time())


def normalize_input(req_input: Any) -> list[dict[str, Any]]:
    """将 Responses 的 `input` 归一化为输入项列表。

    `input` 可能是:
    - 字符串: 视为单条 user 文本消息
    - 数组: 每个元素是一个输入项(message / function_call / function_call_output / reasoning 等)
    """
    if req_input is None:
        return []
    if isinstance(req_input, str):
        return [{"type": "message", "role": "user", "content": req_input}]
    if isinstance(req_input, list):
        items: list[dict[str, Any]] = []
        for it in req_input:
            if isinstance(it, str):
                # 容错: 列表中混入裸字符串时按 user 文本处理
                items.append({"type": "message", "role": "user", "content": it})
            elif isinstance(it, dict):
                items.append(it)
        return items
    return []


def item_type(item: dict[str, Any]) -> str:
    """推断输入/输出项的类型。

    未显式声明 type 但带有 role 时,按 message 处理(Responses 的 EasyInputMessage 允许省略 type)。
    """
    t = item.get("type")
    if t:
        return t
    if "role" in item:
        return "message"
    return "unknown"


def content_parts_to_text(content: Any) -> str:
    """将消息 content 中的所有文本部分拼接为纯文本(丢弃非文本部分)。

    用于无法承载结构化 content 的场景(如 system 文本合并)。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict):
                # 兼容 input_text / output_text / text 等文本部分
                if part.get("type") in ("input_text", "output_text", "text"):
                    texts.append(part.get("text", ""))
        return "".join(texts)
    return ""


def namespaced_child_name(namespace: str, name: str) -> str:
    """按 Codex 规则将命名空间与子工具名组合成扁平名。

    与 Codex 的 code_mode_name_for_tool_name 保持一致:
    - 命名空间以 '_' 结尾,或子名以 '_' 开头时,直接拼接(无分隔符);
    - 否则用 '__' 分隔。
    """
    if namespace.endswith("_") or name.startswith("_"):
        return f"{namespace}{name}"
    return f"{namespace}__{name}"


def combine_call_name(name: str, namespace: str | None) -> str:
    """将(裸名, 命名空间)组合为出站使用的扁平调用名。"""
    if namespace:
        return namespaced_child_name(namespace, name)
    return name


def flatten_function_tools(tools: Any) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], list[str], list[dict[str, Any]]]:
    """将 Responses 工具列表展平为 function 描述符。

    返回四元组:
    - func_descs: 归一化的 function 描述符列表 [{name, description, parameters, strict?}]
    - name_map:   组合名 -> {"namespace": ns, "name": 裸名},用于回程拆名
    - dropped:    无法表达为 function 的工具类型列表(供调用方记日志)
    - web_search: web_search 工具列表(需 provider 支持才可转换)
    """
    func_descs: list[dict[str, Any]] = []
    name_map: dict[str, dict[str, str]] = {}
    dropped: list[str] = []
    web_search: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return func_descs, name_map, dropped, web_search

    def _pick(src: dict[str, Any], name: str) -> dict[str, Any]:
        d: dict[str, Any] = {"name": name}
        if "description" in src:
            d["description"] = src["description"]
        if "parameters" in src:
            d["parameters"] = src["parameters"]
        if "strict" in src:
            d["strict"] = src["strict"]
        return d

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        ttype = tool.get("type")
        if ttype == "function":
            func_descs.append(_pick(tool, tool.get("name", "")))
        elif ttype == "namespace":
            ns = tool.get("name", "")
            for child in tool.get("tools") or []:
                if isinstance(child, dict) and child.get("type") == "function":
                    bare = child.get("name", "")
                    combined = namespaced_child_name(ns, bare)
                    func_descs.append(_pick(child, combined))
                    name_map[combined] = {"namespace": ns, "name": bare}
        elif ttype == "web_search":
            # web_search 是可转换的内置工具,单独收集
            web_search.append(tool)
        else:
            # file_search / custom 等无法表达为纯 function,记录类型供丢弃
            dropped.append(str(ttype))
    return func_descs, name_map, dropped, web_search


def split_call_name(name: str, name_map: dict[str, dict[str, str]] | None) -> tuple[str, str | None]:
    """回程拆名: 若组合名在映射中,返回(裸名, 命名空间),否则原样返回(name, None)。"""
    if name_map and name in name_map:
        entry = name_map[name]
        return entry["name"], entry["namespace"]
    return name, None


def data_url_to_parts(url: str) -> tuple[str | None, str | None]:
    """解析 data URL,返回 (media_type, base64_data)。

    若不是 data URL 则返回 (None, None)。
    形如: data:image/png;base64,XXXX
    """
    if not url.startswith("data:"):
        return None, None
    try:
        header, b64 = url.split(",", 1)
        media_type = header[len("data:"):].split(";", 1)[0] or None
        return media_type, b64
    except ValueError:
        return None, None
