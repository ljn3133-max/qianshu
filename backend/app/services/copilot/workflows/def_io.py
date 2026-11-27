"""自定义工作流定义的导入导出序列化：JSON + YAML 双格式。

红线：不引第三方依赖（PyYAML 不进 requirements）——YAML 走 **JSON 兼容子集** 手工实现，
导出/导入自成闭环（roundtrip 测试锁行为）：

  - 导出（dumps_yaml）：块状 mapping/sequence；字符串一律双引号（json.dumps 转义
    = YAML 1.2 双引号标量转义子集，引号/换行/冒号/#/{{占位符}} 原样保真）；
    数字/布尔/null 裸写；空对象 {}、空数组 []。
  - 导入（loads_def_text）：先 json.loads（.json 文件与 JSON 流式 YAML 直通），
    失败再走块状解析器（支持本导出子集 + 常见手改：块状嵌套、序列紧凑映射、
    双引号/单引号/纯标量、整行注释、同缩进序列、{{参数}} 整值裸写）。
  不支持：锚点/别名/多文档/块标量(| >)/行尾注释/未引号流式映射——遇到明确报错不猜。

导出载荷 = def_json 落档格式 = 导出文件 workflows[] 元素：
  {key,name,description,domain,steps,enabled}（version 由服务端管理，不进载荷）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import List

_KEY_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+(?:[eE][+-]?\d+)?$|^-?\d+[eE][+-]?\d+$")
_PLACEHOLDER_FULL = re.compile(r"\{\{\s*[a-zA-Z_][\w]*\s*\}\}")
_PAYLOAD_FIELDS = ("key", "name", "description", "domain", "steps", "enabled")


class DefFormatError(ValueError):
    """导入内容解析失败 / 无法序列化（API 层映射 400）。"""


# ---------------- 载荷（def_json / 导出 workflows[] 元素） ----------------

def payload_from_out(out: dict) -> dict:
    """def_out 字典 -> 导出/落档载荷（只留定义内容六字段）。"""
    return {k: out.get(k) for k in _PAYLOAD_FIELDS}


def export_envelope(payloads: List[dict]) -> dict:
    return {
        "format": "qianshu.workflow.v1",
        "exported_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "workflows": [dict(p) for p in payloads],
    }


def extract_defs(obj) -> list:
    """导入内容 -> 定义列表。兼容：导出包 {workflows:[...]} / 纯定义对象 / 定义数组。"""
    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict) and isinstance(obj.get("workflows"), list):
        items = obj["workflows"]
    elif isinstance(obj, dict) and "key" in obj and "steps" in obj:
        items = [obj]
    else:
        raise DefFormatError("内容里没有工作流定义（需要导出包 workflows 列表、定义数组或含 key/steps 的定义对象）")
    out = []
    for it in items:
        if not isinstance(it, dict):
            raise DefFormatError("工作流定义必须是对象")
        out.append(it)
    return out


def loads_def_text(text: str):
    """导入文本 -> 对象：JSON 优先，回落 YAML 子集解析。"""
    s = (text or "").lstrip("﻿").strip()
    if not s:
        raise DefFormatError("内容为空")
    try:
        return json.loads(s)
    except ValueError:
        return loads_yaml(s)


# ---------------- YAML 导出（块状 + JSON 转义双引号标量） ----------------

def dumps_yaml(obj) -> str:
    lines: list = []
    _emit_value(obj, 0, lines)
    return "\n".join(lines) + "\n"


def _scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)          # 数字裸写（json 编码=合法 YAML 数字）
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)   # 双引号=YAML 1.2 兼容 JSON 转义
    if isinstance(v, dict):
        return "{}"                                # 空对象内联
    if isinstance(v, list):
        return "[]"                                # 空数组内联
    raise DefFormatError(f"无法序列化的值类型：{type(v).__name__}")


def _key_token(k) -> str:
    if isinstance(k, str) and _KEY_SAFE.match(k):
        return k
    return json.dumps(k, ensure_ascii=False)


def _emit_value(value, indent: int, out: list) -> None:
    if isinstance(value, dict) and value:
        for k, v in value.items():
            _emit_pair(k, v, indent, out)
    elif isinstance(value, list) and value:
        _emit_seq(value, indent, out)
    else:
        out.append("  " * indent + _scalar(value))


def _emit_pair(key, value, indent: int, out: list) -> None:
    pad = "  " * indent
    head = f"{_key_token(key)}:"
    if isinstance(value, (dict, list)) and value:
        out.append(pad + head)
        _emit_value(value, indent + 1, out)
    else:
        out.append(f"{pad}{head} {_scalar(value)}")


def _emit_seq(items: list, indent: int, out: list) -> None:
    pad = "  " * indent
    for it in items:
        if isinstance(it, (dict, list)) and it:
            sub: list = []
            _emit_value(it, indent + 1, sub)
            out.append(pad + "- " + sub[0][len(pad) + 2:])
            out.extend(sub[1:])
        else:
            out.append(pad + "- " + _scalar(it))


# ---------------- YAML 导入（块状子集解析） ----------------

def loads_yaml(text: str):
    lines: list = []
    for ln in (text or "").splitlines():
        if not ln.strip():
            continue
        stripped = ln.lstrip(" ")
        if stripped.startswith("#"):
            continue                       # 整行注释（行尾注释不支持）
        if "\t" in ln[:len(ln) - len(ln.lstrip(" \t"))]:
            raise DefFormatError("YAML 缩进不支持制表符（请用空格）")
        lines.append(ln.rstrip())
    if not lines:
        raise DefFormatError("内容为空")
    val, i = _parse_block(lines, 0, _indent_of(lines[0]))
    if i != len(lines):
        raise DefFormatError(f"YAML 解析失败：{lines[i].strip()[:40]} 起无法解析")
    return val


def _indent_of(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def _is_seq_line(content: str) -> bool:
    c = content.strip()
    return c == "-" or c.startswith("- ")


def _parse_block(lines: list, i: int, indent: int):
    if _is_seq_line(lines[i]):
        return _parse_seq(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_map(lines: list, i: int, indent: int):
    out: dict = {}
    while i < len(lines):
        ind = _indent_of(lines[i])
        if ind < indent:
            break
        if ind > indent:
            raise DefFormatError(f"YAML 缩进错乱：{lines[i].strip()[:40]}")
        content = lines[i].strip()
        if _is_seq_line(content):
            break
        key, rest = _split_key(content)
        if key in out:
            raise DefFormatError(f"YAML 键重复：{key}")
        if rest == "":
            nxt = i + 1
            if nxt < len(lines) and _indent_of(lines[nxt]) > indent:
                out[key], i = _parse_block(lines, nxt, _indent_of(lines[nxt]))
            elif (nxt < len(lines) and _indent_of(lines[nxt]) == indent
                  and _is_seq_line(lines[nxt])):
                out[key], i = _parse_seq(lines, nxt, indent)   # 同缩进序列（手写友好）
            else:
                out[key] = None
                i = nxt
        else:
            out[key] = _parse_scalar(rest)
            i += 1
    return out, i


def _parse_seq(lines: list, i: int, indent: int):
    out: list = []
    while i < len(lines):
        ind = _indent_of(lines[i])
        if ind != indent or not _is_seq_line(lines[i]):
            break
        content = lines[i].strip()
        if content == "-":
            nxt = i + 1
            if nxt < len(lines) and _indent_of(lines[nxt]) > indent:
                val, i = _parse_block(lines, nxt, _indent_of(lines[nxt]))
                out.append(val)
            else:
                out.append(None)
                i = nxt
            continue
        rest = content[2:].strip()
        if rest == "":
            out.append(None)
            i += 1
            continue
        if not _is_flow_token(rest) and (_try_split_key(rest) is not None or _is_seq_line(rest)):
            # 紧凑写法："- " 换两个空格，落到 indent+2 解析（映射或嵌套序列）
            lines[i] = " " * (indent + 2) + rest
            if _is_seq_line(rest):
                val, i = _parse_seq(lines, i, indent + 2)
            else:
                val, i = _parse_map(lines, i, indent + 2)
            out.append(val)
        else:
            out.append(_parse_scalar(rest))
            i += 1
    return out, i


def _is_flow_token(token: str) -> bool:
    return token.startswith("{") or token.startswith("[")


def _try_split_key(content: str):
    """'key: rest' -> (key, rest)；不像键行返回 None（流式/纯标量）。"""
    if _is_flow_token(content):
        return None
    if content.startswith('"'):
        try:
            key, idx = json.JSONDecoder().raw_decode(content)
        except ValueError:
            return None
        if not isinstance(key, str):
            return None
        rest = content[idx:].strip()
        if not rest.startswith(":"):
            return None
        return key, rest[1:].strip()
    if content.startswith("'"):
        try:
            key, rest = _read_single_quoted(content)
        except DefFormatError:
            return None
        rest = rest.strip()
        if not rest.startswith(":"):
            return None
        return key, rest[1:].strip()
    if content.endswith(":") and ": " not in content:
        return content[:-1].strip(), ""
    idx = content.find(": ")
    if idx <= 0:
        return None
    return content[:idx].strip(), content[idx + 2:].strip()


def _split_key(content: str):
    got = _try_split_key(content)
    if got is None:
        raise DefFormatError(f"YAML 行不是 key: value 形式：{content[:40]}")
    return got


def _read_single_quoted(token: str):
    buf: list = []
    i = 1
    while i < len(token):
        ch = token[i]
        if ch == "'":
            if i + 1 < len(token) and token[i + 1] == "'":
                buf.append("'")
                i += 2
                continue
            return "".join(buf), token[i + 1:]
        buf.append(ch)
        i += 1
    raise DefFormatError(f"YAML 单引号字符串未闭合：{token[:40]}")


def _parse_scalar(token: str):
    token = token.strip()
    if token.startswith('"'):
        try:
            val, idx = json.JSONDecoder().raw_decode(token)
        except ValueError:
            raise DefFormatError(f"YAML 双引号字符串解析失败：{token[:40]}")
        if token[idx:].strip():
            raise DefFormatError(f"YAML 双引号字符串后有多余内容：{token[:40]}")
        return val
    if token.startswith("'"):
        val, rest = _read_single_quoted(token)
        if rest.strip():
            raise DefFormatError(f"YAML 单引号字符串后有多余内容：{token[:40]}")
        return val
    if _is_flow_token(token):
        if _PLACEHOLDER_FULL.fullmatch(token):
            return token                     # {{参数}} 整值裸写（手写友好）
        try:
            return json.loads(token)         # 流式仅收 JSON 兼容写法
        except ValueError:
            raise DefFormatError(
                f"YAML 流式写法只支持 JSON 兼容（键名带引号）或改用块式：{token[:40]}")
    if token in ("null", "~"):
        return None
    if token in ("true", "True", "TRUE"):
        return True
    if token in ("false", "False", "FALSE"):
        return False
    if _INT_RE.match(token):
        return int(token)
    if _FLOAT_RE.match(token):
        return float(token)
    return token
