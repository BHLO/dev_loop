#!/usr/bin/env python3
"""代码客观指标：超长函数、深嵌套、参数过多、长文件、重复块、TODO 堆积。

用法:
  python metrics.py --root .                # 全量
  python metrics.py --root . src/api tests  # 只查指定路径
  python metrics.py --root . --top 30       # 控制输出条数

说明: 函数级指标用 Python AST 精确计算；其他语言只给文件级指标，
      不做假的复杂度估算。输出 markdown，便于直接贴进 .devloop/S5-健康检查.md。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import re
from pathlib import Path

SKIP_DIRS = {
    ".git", "node_modules", ".devloop", "venv", ".venv", "__pycache__",
    "dist", "build", "target", "vendor", ".idea", ".vscode", "site-packages",
    ".mypy_cache", ".pytest_cache", "coverage",
}
CODE_EXT = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".kt", ".go", ".cs",
    ".php", ".rb", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp", ".swift", ".scala",
}

TH = {
    "func_len": 50,      # 超过算长
    "func_len_hard": 100,
    "params": 5,
    "branches": 10,
    "nesting": 4,
    "file_lines": 500,
    "line_len": 160,
}

CTRL = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
        ast.Try, ast.Match if hasattr(ast, "Match") else ast.Try)


def walk_files(roots: list[Path]):
    for root in roots:
        if root.is_file():
            yield root
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            if p.suffix.lower() not in CODE_EXT:
                continue
            yield p


def count_branches(node: ast.AST) -> int:
    n = 0
    for x in ast.walk(node):
        if isinstance(x, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp)):
            n += 1
        elif isinstance(x, ast.BoolOp):
            n += max(0, len(x.values) - 1)
        elif hasattr(ast, "Match") and isinstance(x, ast.Match):
            n += max(0, len(x.cases) - 1)
    return n


def max_depth(node: ast.AST, depth: int = 0) -> int:
    best = depth
    for child in ast.iter_child_nodes(node):
        d = depth + 1 if isinstance(child, CTRL) else depth
        best = max(best, max_depth(child, d))
    return best


def py_metrics(path: Path):
    try:
        src = path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(src)
    except (SyntaxError, ValueError, OSError):
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", None)
        if end is None:
            continue
        a = node.args
        params = (len(a.posonlyargs) + len(a.args) + len(a.kwonlyargs)
                  + (1 if a.vararg else 0) + (1 if a.kwarg else 0))
        out.append({
            "file": path, "line": node.lineno, "name": node.name,
            "length": end - node.lineno + 1,
            "params": params,
            "branches": count_branches(node),
            "nesting": max_depth(node),
        })
    return out


def file_stats(path: Path):
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    code = [l for l in lines if l.strip() and not l.strip().startswith(("#", "//", "*", "/*"))]
    todo = sum(len(re.findall(r"\b(TODO|FIXME|HACK|XXX)\b", l)) for l in lines)
    return {
        "lines": len(lines), "code": len(code), "todo": todo,
        "longest": max((len(l) for l in lines), default=0),
    }


def norm_lines(path: Path, window: int = 6):
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    norm = []
    for i, l in enumerate(lines, 1):
        s = l.strip()
        if not s or s.startswith(("#", "//", "/*", "*", "}", "{")):
            continue
        norm.append((i, re.sub(r"\s+", " ", s)))
    blocks = []
    for i in range(len(norm) - window + 1):
        chunk = norm[i:i + window]
        text = "\n".join(c[1] for c in chunk)
        if len(text) < 80:
            continue
        h = hashlib.md5(text.encode("utf-8")).hexdigest()
        blocks.append((h, norm[i][0], text))
    return blocks


def main() -> int:
    ap = argparse.ArgumentParser(description="代码客观指标")
    ap.add_argument("--root", default=".")
    ap.add_argument("paths", nargs="*", help="限定路径，默认全仓库")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    root = Path(args.root).resolve()
    roots = [root / p for p in args.paths] if args.paths else [root]
    roots = [r for r in roots if r.exists()] or [root]

    files = sorted(set(walk_files(roots)))
    funcs, fstats = [], []
    for f in files:
        st = file_stats(f)
        if st:
            fstats.append((f, st))
        if f.suffix.lower() == ".py":
            funcs.extend(py_metrics(f))

    def rel(p: Path) -> str:
        try:
            return str(p.relative_to(root)).replace("\\", "/")
        except ValueError:
            return str(p)

    print(f"# 代码指标\n")
    print(f"- 仓库：`{root}`")
    print(f"- 扫描文件：{len(files)} 个（其中 Python {sum(1 for f in files if f.suffix == '.py')} 个）\n")

    hot = [x for x in funcs if x["length"] >= TH["func_len"] or x["params"] > TH["params"]
           or x["branches"] > TH["branches"] or x["nesting"] > TH["nesting"]]
    hot.sort(key=lambda x: (x["length"] + x["branches"] * 3 + x["nesting"] * 5), reverse=True)
    print("## 函数热点（Python 精确指标）\n")
    if hot:
        print("| 文件:行 | 函数 | 行数 | 参数 | 分支 | 嵌套 |")
        print("|---|---|---|---|---|---|")
        for x in hot[:args.top]:
            flag = []
            if x["length"] >= TH["func_len_hard"]:
                flag.append("极长")
            elif x["length"] >= TH["func_len"]:
                flag.append("超长")
            if x["params"] > TH["params"]:
                flag.append("参数多")
            if x["branches"] > TH["branches"]:
                flag.append("分支多")
            if x["nesting"] > TH["nesting"]:
                flag.append("深嵌套")
            name = x["name"] + (f" ({'/'.join(flag)})" if flag else "")
            print(f"| `{rel(x['file'])}:{x['line']}` | `{name}` | {x['length']} | "
                  f"{x['params']} | {x['branches']} | {x['nesting']} |")
    else:
        print("未发现超过阈值的函数。")
    print()

    big = [(f, s) for f, s in fstats if s["lines"] > TH["file_lines"]]
    big.sort(key=lambda x: x[1]["lines"], reverse=True)
    print("## 长文件\n")
    if big:
        print("| 文件 | 总行 | 代码行 | TODO | 最长行 |")
        print("|---|---|---|---|---|")
        for f, s in big[:args.top]:
            print(f"| `{rel(f)}` | {s['lines']} | {s['code']} | {s['todo']} | {s['longest']} |")
    else:
        print("未发现超长文件。")
    print()

    todos = [(f, s) for f, s in fstats if s["todo"] > 0]
    todos.sort(key=lambda x: x[1]["todo"], reverse=True)
    if todos:
        print("## TODO / FIXME 堆积\n")
        print("| 文件 | 数量 |")
        print("|---|---|")
        for f, s in todos[:args.top]:
            print(f"| `{rel(f)}` | {s['todo']} |")
        print()

    groups: dict[str, list] = {}
    for f in files:
        if f.suffix.lower() not in CODE_EXT:
            continue
        for h, line, text in norm_lines(f):
            groups.setdefault(h, []).append((f, line, text))
    dups = {h: v for h, v in groups.items() if len(v) >= 2}
    print("## 重复代码块（连续 6 行完全相同）\n")
    if dups:
        print("| 出现次数 | 位置 |")
        print("|---|---|")
        shown = 0
        for h, v in sorted(dups.items(), key=lambda kv: len(kv[1]), reverse=True):
            locs = ", ".join(f"`{rel(f)}:{ln}`" for f, ln, _ in v[:4])
            print(f"| {len(v)} | {locs} |")
            shown += 1
            if shown >= args.top:
                break
    else:
        print("未发现明显重复块。")
    print()

    print("## 判读提示\n")
    print("- 指标是证据，不是结论：长函数不一定是坏代码，但要能说清为什么长。")
    print("- 函数级指标只覆盖 Python；其他语言仅文件级。")
    print("- 重复块可能是有意为之（测试夹具、协议实现），命中后人工确认。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
