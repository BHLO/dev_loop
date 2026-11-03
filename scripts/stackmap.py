#!/usr/bin/env python3
"""栈帧映射：把报错堆栈里的帧映射到仓库内的文件与行，打印上下文并找抛出点。

用法:
  python stackmap.py --root . --log err.log
  python stackmap.py --root . --text "Traceback (most recent call last): ..."
  cat err.log | python stackmap.py --root .

输出: 终端摘要 + .devloop/trace.md（可用 --out 改路径）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git", "node_modules", ".devloop", "venv", ".venv", "__pycache__",
    "dist", "build", "target", "vendor", ".idea", ".vscode", "site-packages",
}

FRAME_PATTERNS = [
    # Python:  File "app/x.py", line 42, in handler
    ("python", re.compile(r'File "([^"]+)", line (\d+)(?:, in (.+))?')),
    # Java/Kotlin:  at com.foo.Bar.baz(Bar.java:42)
    ("java", re.compile(r'at\s+([\w$.<>]+)\(([\w$]+\.(?:java|kt)):(\d+)\)')),
    # JS/TS:  at fn (src/a.ts:42:7)  /  at src/a.ts:42:7
    ("js", re.compile(
        r'at\s+(?:(?:async\s+)?([^\s(]+)\s+\()?'
        r'([^\s():]+\.(?:js|ts|jsx|tsx|mjs|cjs|vue)):(\d+):(\d+)\)?')),
    # Go:  main/handler.go:42 +0x1d
    ("go", re.compile(r'([\w./\\-]+\.go):(\d+)(?:\s+\+0x[0-9a-f]+)?')),
    # .NET:  at Ns.Cls.M() in C:\src\A.cs:line 42
    ("dotnet", re.compile(r'at\s+([\w.<>+`\[\]]+)\s+in\s+([^:]+):line\s+(\d+)')),
]

EXC_RE = re.compile(
    r'^(?:panic:\s*)?([A-Za-z_][\w.]*(?:Error|Exception|Throwable|Fault|Panic))\b[:\s]*(.*)$')
FATAL_RE = re.compile(r'^(?:panic|fatal error):\s*(.+)$')


def walk_files(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p


def build_index(root: Path) -> dict[str, list[Path]]:
    idx: dict[str, list[Path]] = {}
    for p in walk_files(root):
        idx.setdefault(p.name, []).append(p)
    return idx


def resolve(raw: str, root: Path, idx: dict[str, list[Path]]) -> Path | None:
    raw = raw.strip().strip('"').strip("'")
    candidates = [Path(raw)]
    if not Path(raw).is_absolute():
        candidates.append(root / raw)
    # 去掉常见的构建前缀
    parts = Path(raw).parts
    for i in range(1, len(parts)):
        candidates.append(root / Path(*parts[i:]))
    for c in candidates:
        try:
            if c.is_file():
                return c.resolve()
        except OSError:
            continue
    name = Path(raw).name
    hits = idx.get(name, [])
    if len(hits) == 1:
        return hits[0].resolve()
    return None


def parse_frames(text: str):
    frames = []
    for line in text.splitlines():
        for lang, pat in FRAME_PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            g = m.groups()
            if lang == "python":
                frames.append({"lang": lang, "file": g[0], "line": int(g[1]),
                               "func": (g[2] or "").strip(), "raw": line.strip()})
            elif lang == "java":
                frames.append({"lang": lang, "file": g[1], "line": int(g[2]),
                               "func": g[0], "raw": line.strip()})
            elif lang == "js":
                frames.append({"lang": lang, "file": g[1], "line": int(g[2]),
                               "func": (g[0] or "").strip(), "raw": line.strip()})
            elif lang == "go":
                frames.append({"lang": lang, "file": g[0], "line": int(g[1]),
                               "func": "", "raw": line.strip()})
            elif lang == "dotnet":
                frames.append({"lang": lang, "file": g[1], "line": int(g[2]),
                               "func": g[0], "raw": line.strip()})
            break
    return frames


def parse_exception(text: str) -> tuple[str, str]:
    name, msg = "", ""
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = EXC_RE.match(s)
        if m and not s.startswith("at ") and not s.startswith("File "):
            name, msg = m.group(1), (m.group(2) or "").strip()
        m2 = FATAL_RE.match(s)
        if m2:
            name, msg = (name or "panic"), m2.group(1).strip()
    return name, msg


def find_throw_sites(root: Path, exc_name: str, limit: int = 12):
    if not exc_name:
        return []
    short = exc_name.split(".")[-1]
    pats = [
        re.compile(rf'\braise\s+{re.escape(short)}\b'),
        re.compile(rf'\bthrow\s+(?:new\s+)?{re.escape(short)}\b'),
        re.compile(rf'\breturn\s+.*{re.escape(short)}\s*\('),
    ]
    hits = []
    for p in walk_files(root):
        if p.suffix.lower() not in {".py", ".js", ".ts", ".jsx", ".tsx",
                                    ".java", ".kt", ".go", ".cs", ".php", ".rb"}:
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if any(pt.search(line) for pt in pats):
                hits.append((p, i, line.strip()))
                if len(hits) >= limit:
                    return hits
    return hits


def snippet(path: Path, lineno: int, pad: int = 12) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return "(无法读取)"
    lo = max(1, lineno - pad)
    hi = min(len(lines), lineno + pad)
    out = []
    for n in range(lo, hi + 1):
        mark = ">>>" if n == lineno else "   "
        out.append(f"{mark} {n:>5} | {lines[n - 1]}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="栈帧映射")
    ap.add_argument("--root", default=".", help="仓库根目录")
    ap.add_argument("--log", help="报错日志文件")
    ap.add_argument("--text", help="直接给报错文本")
    ap.add_argument("--out", default=".devloop/trace.md", help="输出文件")
    ap.add_argument("--pad", type=int, default=12, help="上下文行数")
    args = ap.parse_args()

    if args.log:
        text = Path(args.log).read_text(encoding="utf-8", errors="ignore")
    elif args.text:
        text = args.text
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        ap.error("需要 --log / --text 或管道输入")

    root = Path(args.root).resolve()
    idx = build_index(root)

    frames = parse_frames(text)
    # Python 的 traceback 是「most recent call last」，最后一行才是出错点；
    # Java/JS/Go 相反，第一帧就是最内层。统一成「内层在前」。
    if "most recent call last" in text.lower():
        frames = list(reversed(frames))
    mapped, outside = [], []
    for f in frames:
        p = resolve(f["file"], root, idx)
        if p is None:
            outside.append(f)
        else:
            f["abs"] = p
            try:
                f["rel"] = str(p.relative_to(root)).replace("\\", "/")
            except ValueError:
                f["rel"] = str(p)
            mapped.append(f)

    seen, uniq = set(), []
    for f in mapped:
        key = (f["rel"], f["line"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(f)

    exc_name, exc_msg = parse_exception(text)
    throws = find_throw_sites(root, exc_name)

    md = ["# 栈帧映射报告", ""]
    md.append(f"- 仓库：`{root}`")
    md.append(f"- 异常：`{exc_name or '未识别'}`" + (f" — {exc_msg}" if exc_msg else ""))
    md.append(f"- 帧总数 {len(frames)}，落在本仓库内 {len(uniq)}，仓库外 {len(outside)}")
    md.append("")

    if not uniq:
        md.append("## 结论")
        md.append("")
        md.append("没有帧映射到本仓库：报错可能来自三方库、构建产物，或栈里只给了类名。"
                  "改用 `/trace` 第 2 步（git log -S / git bisect / 加日志）继续定位。")
    else:
        md.append("## 项目内帧（按栈序，越靠前越内层）")
        md.append("")
        md.append("| # | 位置 | 函数 |")
        md.append("|---|---|---|")
        for i, f in enumerate(uniq, 1):
            md.append(f"| {i} | `{f['rel']}:{f['line']}` | `{f['func'] or '-'}` |")
        md.append("")
        primary = uniq[0]
        md.append(f"## 主嫌疑帧 `{primary['rel']}:{primary['line']}`")
        md.append("")
        md.append("```")
        md.append(snippet(primary["abs"], primary["line"], args.pad))
        md.append("```")
        md.append("")

    if throws:
        md.append(f"## 可疑抛出点（`{exc_name}`）")
        md.append("")
        md.append("| 文件:行 | 代码 |")
        md.append("|---|---|")
        for p, ln, code in throws:
            try:
                rel = str(p.relative_to(root)).replace("\\", "/")
            except ValueError:
                rel = str(p)
            md.append(f"| `{rel}:{ln}` | `{code[:120]}` |")
        md.append("")

    out = Path(args.out)
    if not out.is_absolute():
        out = root / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md), encoding="utf-8")

    print(f"栈帧映射 -> {out}")
    print(f"  异常 {exc_name or '未识别'}；项目内帧 {len(uniq)}/{len(frames)}；抛出点 {len(throws)}")
    for f in uniq[:5]:
        print(f"  [{f['rel']}:{f['line']}] {f['func'] or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
