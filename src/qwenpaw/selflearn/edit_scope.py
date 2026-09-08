# -*- coding: utf-8 -*-
"""Small, declarative edit boundaries; never execute harness Python."""

import ast
import re
from pathlib import Path, PurePosixPath


def harness_path(root: Path, relative: str) -> Path:
    root = root.resolve()
    parts = PurePosixPath(relative).parts
    if (
        not parts
        or PurePosixPath(relative).is_absolute()
        or any(p in {"..", ".git"} for p in parts)
    ):
        raise ValueError(f"无效的 Harness 路径：{relative}")
    path = root / relative
    if any(
        (root / Path(*parts[:i])).is_symlink()
        for i in range(1, len(parts) + 1)
    ):
        raise ValueError("Harness 文件不能使用符号链接")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Harness 路径超出工作目录")
    return path


def outside_sections(text: str, names: list[str]) -> str:
    if names == ["*"]:
        return ""
    headings = list(re.finditer(r"^(#{1,6}) (.+)\n", text, re.M))
    spans = []
    for name in names:
        matches = [h for h in headings if h[2].strip() == name]
        if len(matches) != 1:
            raise ValueError(f"需要唯一的 Markdown 小节：{name}")
        heading = matches[0]
        end = next(
            (
                h.start()
                for h in headings
                if h.start() > heading.start() and len(h[1]) <= len(heading[1])
            ),
            len(text),
        )
        spans.append((heading.end(), end, "<editable>\n"))
    for start, end, marker in sorted(spans, reverse=True):
        text = text[:start] + marker + text[end:]
    return text


# Keep the three explicit boundary checks together; never execute this code.
# pylint: disable-next=too-many-branches
def python_boundary(text: str, rules: dict) -> bytes:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise ValueError(
            f"Python 语法错误，第 {exc.lineno} 行：{exc.msg}",
        ) from exc
    lines = text.encode().splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    spans = []

    def mask(node, marker):
        spans.append(
            (
                offsets[node.lineno - 1] + node.col_offset,
                offsets[node.end_lineno - 1] + node.end_col_offset,
                marker.encode(),
            ),
        )

    for name in rules.get("function_returns", []):
        fn = next(
            (
                n
                for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name
            ),
            None,
        )
        returns = (
            [n for n in fn.body if isinstance(n, ast.Return)] if fn else []
        )
        if len(returns) != 1:
            raise ValueError(f"需要唯一的顶层提示词 return：{name}")
        value = returns[0].value
        if isinstance(value, ast.JoinedStr):
            # Preserve every interpolation, its order, conversion and format.
            marker = repr(
                [
                    ast.dump(n)
                    for n in value.values
                    if not isinstance(n, ast.Constant)
                ],
            )
        elif isinstance(value, ast.Constant) and isinstance(value.value, str):
            marker = "[]"
        else:
            raise ValueError(f"{name} 必须直接返回提示词字符串")
        mask(value, marker)

    assignments = {
        n.targets[0].id: n.value
        for n in tree.body
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)
    }
    for name, sections in rules.get("string_sections", {}).items():
        value = assignments.get(name)
        if not isinstance(value, ast.Constant) or not isinstance(
            value.value,
            str,
        ):
            raise ValueError(f"需要字符串常量：{name}")
        mask(value, outside_sections(value.value, sections))
    for name in rules.get("env_defaults", []):
        value = assignments.get(name)
        # Only the default in int(os.environ.get(NAME, "123")) is editable.
        if (
            not isinstance(value, ast.Call)
            or ast.unparse(value.func) != "int"
            or len(value.args) != 1
            or value.keywords
        ):
            raise ValueError(f"无效的预算定义：{name}")
        call = value.args[0]
        if (
            # pylint: disable-next=too-many-boolean-expressions
            not isinstance(call, ast.Call)
            or ast.unparse(call.func) != "os.environ.get"
            or len(call.args) != 2
            or call.keywords
            or not isinstance(call.args[0], ast.Constant)
            or call.args[0].value != name
        ):
            raise ValueError(f"无效的环境变量默认值：{name}")
        default = call.args[1]
        if (
            not isinstance(default, ast.Constant)
            or not isinstance(default.value, str)
            or not default.value.isdecimal()
            or int(default.value) <= 0
        ):
            raise ValueError(f"{name} 默认值必须为正整数字符串")
        mask(default, "<budget>")
    result = text.encode()
    for start, end, replacement in sorted(spans, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def check_edit(target: dict, before: str, after: str) -> None:
    rules = target.get("edit_rules", {})
    if not rules or target["change_mode"] == "engineering":
        raise ValueError("该目标未开放自动编辑；需补充编辑规则或人工工程处理")
    if not after.strip():
        raise ValueError("不能清空 Harness 文件")
    suffix = Path(target["path"]).suffix
    if suffix == ".py":
        equal = python_boundary(before, rules) == python_boundary(after, rules)
    elif suffix == ".md" and rules.get("sections"):
        equal = outside_sections(
            before,
            rules["sections"],
        ) == outside_sections(
            after,
            rules["sections"],
        )
    else:
        raise ValueError(
            "初版自动编辑仅支持有明确规则的 Python 文本/预算和 Markdown",
        )
    if not equal:
        raise ValueError(
            "改动超出授权部分，或修改了执行逻辑、插值和受保护小节",
        )
