#!/usr/bin/env python3
"""
devloop — 研发闭环工具箱（单一入口，四阶段共用一套扫描结果）

子命令:
  scan     项目画像：语言、构建/测试命令、文档清单、入口点、git 状态
  impact   变更影响面：改动文件 -> 调用方 / 测试 / 文档 / 风险分
  graph    改造清单依赖图：清单文件之间谁依赖谁、改动会往哪传
  drift    文档漂移：文档里写的路径、符号、接口是否还存在
  state    阶段状态读写，供跨轮次续做使用

所有产出写入 <root>/.devloop/，不污染仓库。

用法示例:
  python devloop.py scan   --root .
  python devloop.py impact --root . --base HEAD~1
  python devloop.py impact --root . --files src/api/user.py,docs/api.md
  python devloop.py graph  --root . --files src/api/user.py,src/service/order.py
  python devloop.py drift  --root .
  python devloop.py state  --root . --set S2 --note "改完 3 个文件"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

VERSION = "1.8.2"
STATE_DIR = ".devloop"
MAX_FILE_BYTES = 1_200_000
MAX_FILES = 60000

SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "bower_components", "vendor", "dist",
    "build", "out", "target", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".idea", ".vscode", ".gradle", ".next", ".nuxt",
    "coverage", "htmlcov", ".tox", ".devloop", "Pods", "obj", ".terraform",
    ".serverless", "site-packages", ".cache",
}

LANG_BY_EXT = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".mjs": "JavaScript",
    ".cjs": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".vue": "Vue", ".svelte": "Svelte", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
    ".go": "Go", ".rs": "Rust", ".cs": "C#", ".c": "C", ".h": "C/C++", ".cpp": "C++",
    ".cc": "C++", ".hpp": "C++", ".php": "PHP", ".rb": "Ruby", ".swift": "Swift",
    ".m": "Objective-C", ".scala": "Scala", ".sql": "SQL", ".sh": "Shell", ".bash": "Shell",
    ".ps1": "PowerShell", ".lua": "Lua", ".dart": "Dart", ".ex": "Elixir", ".exs": "Elixir",
    ".rb2": "Ruby", ".pl": "Perl", ".r": "R", ".groovy": "Groovy", ".sol": "Solidity",
}
CODE_EXTS = set(LANG_BY_EXT)
DOC_EXTS = {".md", ".markdown", ".rst", ".adoc", ".txt"}
SPEC_EXTS = {".proto", ".graphql", ".gql", ".yaml", ".yml", ".json"}

TEST_DIR_HINTS = ("test", "tests", "__tests__", "spec", "specs", "e2e", "integration")
PUBLIC_HINTS = ("api", "apis", "route", "routes", "router", "controller", "controllers",
                "handler", "handlers", "endpoint", "endpoints", "resource", "resources",
                "views", "http", "server", "service", "services", "public", "sdk", "client")
DATA_HINTS = ("model", "models", "entity", "entities", "migration", "migrations", "schema",
              "schemas", "repository", "repositories", "dao", "db", "database", "orm", "sql",
              "store", "stores", "persistence")
CONFIG_HINTS = ("config", "configs", "settings", "conf", "env", ".env", "application.yml",
                "application.properties", "properties", "constants", "feature", "flags")

STOPWORDS = {
    "self", "this", "init", "main", "test", "tests", "get", "set", "run", "new", "init",
    "value", "values", "data", "item", "items", "list", "dict", "str", "int", "bool",
    "none", "null", "true", "false", "return", "class", "function", "const", "import",
    "export", "default", "props", "state", "index", "utils", "util", "helper", "common",
    "result", "response", "request", "args", "kwargs", "user", "name", "type", "id",
}

DEF_RE = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:declare[ \t]+)?"
    r"(?:(?:public|private|protected|internal|static|final|abstract|async|suspend|override|"
    r"open|sealed|partial|data)[ \t]+)*"
    r"(?:def|class|func|function|fn|interface|type|struct|enum|const|let|var|module|impl|"
    r"trait|record|object)[ \t]+([A-Za-z_$][\w$]{2,})",
    re.MULTILINE,
)
ROUTE_RE = re.compile(r"""["'](/[A-Za-z0-9_\-./{}<>:$]{3,})["']""")
IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{3,})\b")
# 最少 1 字符：否则单字符行内代码会让整行的反引号配对整体错位
CODE_SPAN_RE = re.compile(r"`([^`\n]{1,120})`")
FENCE_RE = re.compile(r"```[ \t]*([\w+-]*)\n(.*?)```", re.DOTALL)
PATH_HINT_RE = re.compile(r"[/\\]|\.[A-Za-z0-9]{1,6}$")


# ----------------------------------------------------------------------------- utils

def reconfigure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def git(root: Path, *args: str, with_stderr: bool = False):
    """跑 git 并返回 (returncode, 输出)。

    git 的失败原因几乎都在 stderr；默认只回 stdout 是为了让调用方用「输出为空」
    判断"没变化"。push 这类需要看到失败原因的场景传 with_stderr=True。
    """
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        text = proc.stdout.strip()
        if with_stderr and proc.returncode != 0:
            text = "\n".join(x for x in (text, proc.stderr.strip()) if x)
        return proc.returncode, text
    except Exception as exc:  # noqa: BLE001
        return 1, f"<git failed: {exc}>"


def has_upstream(root: Path) -> bool:
    rc, _ = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    return rc == 0


def is_git_repo(root: Path) -> bool:
    rc, _ = git(root, "rev-parse", "--git-dir")
    return rc == 0


def iter_files(root: Path):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(".") and d != "venv"
        ]
        for name in filenames:
            count += 1
            if count > MAX_FILES:
                return
            yield Path(dirpath) / name


_TEXT_CACHE: dict[tuple, str] = {}


def read_text(path: Path, limit: int = MAX_FILE_BYTES) -> str:
    """读取文本，按 (路径, 大小, mtime) 缓存，避免多文件影响面分析时重复读盘。"""
    try:
        st = path.stat()
    except Exception:
        return ""
    if st.st_size > limit:
        return ""
    key = (str(path), st.st_size, int(st.st_mtime))
    cached = _TEXT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    if len(_TEXT_CACHE) < 6000:
        _TEXT_CACHE[key] = text
    return text


def rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def ensure_state_dir(root: Path) -> Path:
    d = root / STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_profile(root: Path) -> dict:
    p = root / STATE_DIR / "profile.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def slug_hit(path_str: str, hints) -> bool:
    low = path_str.lower()
    parts = re.split(r"[/\\._\-]", low)
    return any(h in parts or h in low for h in hints)


# ----------------------------------------------------------------------------- scan

def detect_commands(root: Path, files: list[Path]) -> dict:
    names = {p.name for p in files}
    dir_names = {p.parent.name for p in files}
    cmds: dict[str, str] = {}

    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(read_text(pkg))
            scripts = data.get("scripts", {}) or {}
            for key in ("test", "build", "lint", "typecheck", "dev", "start"):
                if key in scripts:
                    cmds[key] = f"npm run {key}"
            if "test" not in cmds and (root / "jest.config.js").exists():
                cmds["test"] = "npx jest"
        except Exception:
            pass

    if "pyproject.toml" in names or "requirements.txt" in names or "setup.py" in names:
        if any("test" in d for d in dir_names):
            cmds.setdefault("test", "python -m pytest -q")
        if "pyproject.toml" in names:
            cmds.setdefault("lint", "ruff check .")

    if "go.mod" in names:
        cmds.setdefault("test", "go test ./...")
        cmds.setdefault("build", "go build ./...")
        cmds.setdefault("lint", "go vet ./...")

    if "pom.xml" in names:
        cmds.setdefault("test", "mvn -q test")
        cmds.setdefault("build", "mvn -q -DskipTests package")

    if "build.gradle" in names or "build.gradle.kts" in names:
        gradlew = "./gradlew" if (root / "gradlew").exists() else "gradle"
        cmds.setdefault("test", f"{gradlew} test")
        cmds.setdefault("build", f"{gradlew} build")

    if "Cargo.toml" in names:
        cmds.setdefault("test", "cargo test")
        cmds.setdefault("build", "cargo build")

    if "composer.json" in names:
        cmds.setdefault("test", "./vendor/bin/phpunit")
    if any(n.endswith(".csproj") or n.endswith(".sln") for n in names):
        cmds.setdefault("test", "dotnet test")

    mk = root / "Makefile"
    if mk.exists():
        txt = read_text(mk)
        for target in ("test", "lint", "build", "check"):
            if re.search(rf"^{target}\s*:", txt, re.MULTILINE):
                cmds.setdefault(target, f"make {target}")
    return cmds


def cmd_scan(args) -> int:
    root = Path(args.root).resolve()
    files = list(iter_files(root))

    lang_counts: dict[str, int] = {}
    total_code = 0
    for f in files:
        lang = LANG_BY_EXT.get(f.suffix.lower())
        if lang:
            lang_counts[lang] = lang_counts.get(lang, 0) + 1
            total_code += 1
    languages = sorted(lang_counts.items(), key=lambda kv: -kv[1])[:6]

    root_docs, docs = [], []
    for f in files:
        r = rel(root, f)
        low = r.lower()
        suffix = f.suffix.lower()
        if suffix in DOC_EXTS:
            if "/" not in r or low.startswith(("docs/", "doc/", "documentation/", "wiki/")):
                docs.append(r)
        elif suffix in (".proto", ".graphql", ".gql"):
            docs.append(r)
        elif re.match(r".*(openapi|swagger)[\w.-]*\.(ya?ml|json)$", low):
            docs.append(r)
        elif re.search(r"(^|/)(schema|migration)[\w.-]*\.sql$", low):
            docs.append(r)
        if "/" not in r and suffix in (".md", ".rst", ".adoc"):
            root_docs.append(r)

    entrypoints = []
    for f in files:
        if f.name in {"main.py", "app.py", "manage.py", "wsgi.py", "asgi.py", "index.js",
                      "index.ts", "server.js", "server.ts", "main.go", "main.rs",
                      "Program.cs", "Application.java", "package.json", "pyproject.toml"}:
            entrypoints.append(rel(root, f))
    entrypoints = sorted(set(entrypoints))[:15]

    test_dirs = sorted({rel(root, p.parent) for p in files
                        if any(h in p.parent.as_posix().lower() for h in TEST_DIR_HINTS)})[:12]

    info = {
        "generated_at": now(),
        "root": str(root),
        "is_git": is_git_repo(root),
        "total_files": len(files),
        "code_files": total_code,
        "languages": [{"lang": k, "files": v} for k, v in languages],
        "commands": detect_commands(root, files),
        "docs": sorted(set(docs))[:200],
        "root_docs": sorted(set(root_docs))[:50],
        "entrypoints": entrypoints,
        "test_dirs": test_dirs,
    }

    if info["is_git"]:
        _, branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
        _, head = git(root, "rev-parse", "--short", "HEAD")
        _, dirty = git(root, "status", "--porcelain")
        _, last = git(root, "log", "-3", "--pretty=%h %ad %s", "--date=short")
        info["git"] = {
            "branch": branch, "head": head,
            "dirty_files": len(dirty.splitlines()) if dirty else 0,
            "recent_commits": last.splitlines() if last else [],
        }

    out = ensure_state_dir(root) / "profile.json"
    write_json(out, info)

    print(f"项目画像 -> {out}")
    print(f"  文件 {info['total_files']} 个，代码 {total_code} 个")
    if languages:
        print("  语言: " + ", ".join(f"{k}({v})" for k, v in languages))
    if info["commands"]:
        print("  命令: " + " | ".join(f"{k}={v}" for k, v in info["commands"].items()))
    print(f"  文档 {len(info['docs'])} 个，根级文档 {len(info['root_docs'])} 个")
    if info.get("git"):
        print(f"  git: {info['git']['branch']} @ {info['git']['head']}，"
              f"未提交 {info['git']['dirty_files']} 个文件")
    return 0


# ----------------------------------------------------------------------------- impact

def changed_files(root: Path, args) -> list[dict]:
    raw_items: list[dict] = []
    if args.files:
        for raw in args.files.split(","):
            raw = raw.strip()
            if not raw:
                continue
            p = Path(raw)
            if not p.is_absolute():
                p = root / raw
            raw_items.append({"path": rel(root, p), "status": "M"})
    else:
        base = args.base
        if args.staged:
            rc, out = git(root, "diff", "--cached", "--name-status")
        else:
            rc, out = git(root, "diff", "--name-status", base)
            if rc != 0 or not out:
                rc, out = git(root, "diff", "--name-status", f"{base}~1", base)
        if not out:
            return []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            raw_items.append({"path": parts[-1], "status": parts[0][0]})

    # 排除本工具产物，避免 .devloop/ 自己被当成变更
    return [it for it in raw_items
            if not it["path"].replace("\\", "/").startswith(STATE_DIR + "/")]


def diff_body(root: Path, base: str, path: str, staged: bool) -> str:
    if staged:
        rc, out = git(root, "diff", "--cached", "-U3", "--", path)
    else:
        rc, out = git(root, "diff", "-U3", base, "--", path)
        if rc != 0:
            rc, out = git(root, "diff", "-U3", f"{base}~1", base, "--", path)
    return out or ""


def extract_symbols(root: Path, item: dict, body: str) -> tuple[list[str], dict]:
    path = root / item["path"]
    text = read_text(path)
    syms: list[str] = []
    meta = {"hunks": 0, "added": 0, "removed": 0}

    for line in body.splitlines():
        if line.startswith("@@"):
            meta["hunks"] += 1
            tail = line.split("@@", 2)[-1].strip()
            m = re.search(r"([A-Za-z_$][\w$]{2,})\s*\(", tail)
            if m:
                syms.append(m.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            meta["added"] += 1
        elif line.startswith("-") and not line.startswith("---"):
            meta["removed"] += 1

    for m in DEF_RE.finditer(text):
        syms.append(m.group(1))
    for m in ROUTE_RE.finditer(body):
        syms.append(m.group(1))

    stem = Path(item["path"]).stem
    if len(stem) > 3:
        syms.append(stem)

    cleaned, seen = [], set()
    for s in syms:
        s = s.strip()
        if len(s) < 3 or s.startswith("_"):
            continue
        # 常见通用词只在全小写时过滤，保留 User / Order 这类实体名
        if s == s.lower() and s in STOPWORDS:
            continue
        if s in seen:
            continue
        seen.add(s)
        cleaned.append(s)
    return cleaned[:40], meta


def scan_refs(root: Path, files: list[Path], symbols: list[str], self_path: str):
    refs = {"code": [], "tests": [], "docs": []}
    if not symbols:
        return refs
    pattern = re.compile(r"\b(" + "|".join(sorted(map(re.escape, symbols), key=len, reverse=True)) + r")\b")
    for f in files:
        r = rel(root, f)
        if r == self_path:
            continue
        suffix = f.suffix.lower()
        is_doc = suffix in DOC_EXTS or suffix in SPEC_EXTS
        is_code = suffix in CODE_EXTS
        if not (is_doc or is_code):
            continue
        text = read_text(f, limit=400_000)
        if not text:
            continue
        hits = {}
        for m in pattern.finditer(text):
            hits[m.group(1)] = hits.get(m.group(1), 0) + 1
        if not hits:
            continue
        lineno = text[:pattern.search(text).start()].count("\n") + 1 if pattern.search(text) else 1
        entry = {
            "file": r,
            "symbols": sorted(hits)[:6],
            "count": sum(hits.values()),
            "line": lineno,
        }
        bucket = "docs" if is_doc else ("tests" if any(h in r.lower() for h in TEST_DIR_HINTS) else "code")
        refs[bucket].append(entry)
    for k in refs:
        refs[k].sort(key=lambda e: -e["count"])
        refs[k] = refs[k][:25]
    return refs


def risk_score(item, refs, meta, doc_refs) -> tuple[int, list[str]]:
    path = item["path"]
    reasons = []
    score = 0

    fanout = len(refs["code"]) + len(refs["tests"])
    s = min(30, fanout * 3)
    if s:
        score += s
        reasons.append(f"被 {fanout} 个文件引用 (+{s})")

    if slug_hit(path, PUBLIC_HINTS):
        score += 20
        reasons.append("触碰对外接口层 (+20)")
    if slug_hit(path, DATA_HINTS):
        score += 20
        reasons.append("触碰数据层/表结构 (+20)")
    if slug_hit(path, CONFIG_HINTS):
        score += 15
        reasons.append("触碰配置/开关 (+15)")
    if not refs["tests"]:
        score += 15
        reasons.append("无测试引用 (+15)")
    churn = meta.get("added", 0) + meta.get("removed", 0)
    if churn > 200:
        score += 10
        reasons.append(f"改动量大 {churn} 行 (+10)")
    if doc_refs:
        score += 10
        reasons.append(f"被 {len(doc_refs)} 个文档描述 (+10)")
    return min(100, score), reasons


def cmd_impact(args) -> int:
    root = Path(args.root).resolve()
    items = changed_files(root, args)
    if not items:
        print("没有检测到变更。可用 --files 指定文件，或确认 --base 是否正确。")
        return 2

    files = list(iter_files(root))
    results = []
    for item in items:
        path = item["path"]
        if item["status"] == "D":
            results.append({
                "file": path, "status": "D", "symbols": [], "refs": {"code": [], "tests": [], "docs": []},
                "risk": 60, "reasons": ["文件被删除，所有引用方需复核 (+60)"],
                "churn": {"added": 0, "removed": 0, "hunks": 0},
            })
            continue
        body = diff_body(root, args.base, path, args.staged)
        symbols, meta = extract_symbols(root, item, body)
        refs = scan_refs(root, files, symbols, path)
        score, reasons = risk_score(item, refs, meta, refs["docs"])
        results.append({
            "file": path, "status": item["status"], "symbols": symbols, "refs": refs,
            "risk": score, "reasons": reasons,
            "churn": {"added": meta["added"], "removed": meta["removed"], "hunks": meta["hunks"]},
        })

    results.sort(key=lambda r: -r["risk"])
    max_risk = max((r["risk"] for r in results), default=0)
    level = "高" if max_risk >= 60 else ("中" if max_risk >= 25 else "低")

    payload = {
        "generated_at": now(),
        "base": args.base,
        "staged": bool(args.staged),
        "level": level,
        "max_risk": max_risk,
        "files": results,
    }
    out_dir = ensure_state_dir(root)
    write_json(out_dir / "impact.json", payload)

    md = [f"# 变更影响面报告", "",
          f"- 生成时间：{payload['generated_at']}",
          f"- 对比基线：{args.base}{'（暂存区）' if args.staged else ''}",
          f"- 改动文件：{len(results)} 个",
          f"- 整体风险：**{level}**（最高 {max_risk}/100）", ""]
    for r in results:
        md.append(f"## {r['file']}  ·  {r['status']}  ·  风险 {r['risk']}")
        if r["reasons"]:
            md.append("风险来源：" + "；".join(r["reasons"]))
        ch = r["churn"]
        md.append(f"改动：+{ch['added']} / -{ch['removed']}，{ch['hunks']} 个 hunk")
        if r["symbols"]:
            md.append("涉及符号：" + ", ".join(f"`{s}`" for s in r["symbols"][:12]))
        for bucket, label in (("code", "调用方"), ("tests", "相关测试"), ("docs", "相关文档")):
            entries = r["refs"][bucket]
            md.append(f"**{label}（{len(entries)}）**")
            for e in entries[:12]:
                md.append(f"- `{e['file']}`:{e['line']} — {', '.join(e['symbols'][:4])}")
            if not entries:
                md.append("- 无")
        md.append("")
    (out_dir / "impact.md").write_text("\n".join(md), encoding="utf-8")

    print(f"影响面 -> {out_dir / 'impact.md'}（风险 {level}，{len(results)} 个文件）")
    for r in results:
        print(f"  [{r['risk']:>3}] {r['file']}  调用方 {len(r['refs']['code'])} / "
              f"测试 {len(r['refs']['tests'])} / 文档 {len(r['refs']['docs'])}")
    return 0


# ----------------------------------------------------------------------------- drift

def classify_claim(raw: str) -> tuple[str, str]:
    s = raw.strip()
    if not s:
        return "", ""
    if re.match(r"^(npm|pnpm|yarn|pip|poetry|go|mvn|gradle|dotnet|cargo|docker|git|make|python|pytest|ruff)\b", s):
        return "command", s
    if re.match(r"^(GET|POST|PUT|DELETE|PATCH)\s+/", s, re.IGNORECASE) or s.startswith("/api/"):
        return "endpoint", s.split()[-1]
    if PATH_HINT_RE.search(s) and " " not in s:
        return "path", s
    if re.match(r"^[A-Za-z_$][\w$.:#-]*$", s) and len(s) >= 4:
        return "symbol", s
    return "", ""


def cmd_drift(args) -> int:
    root = Path(args.root).resolve()
    profile = load_profile(root)
    doc_list = args.docs.split(",") if args.docs else profile.get("docs", [])
    if not doc_list:
        files = list(iter_files(root))
        doc_list = [rel(root, f) for f in files if f.suffix.lower() in DOC_EXTS][:200]

    code_files = [f for f in iter_files(root) if f.suffix.lower() in CODE_EXTS]

    # 一遍扫描建立标识符集合 + 文本抽样
    identifiers: set[str] = set()
    code_texts: dict[str, str] = {}
    for f in code_files:
        text = read_text(f, limit=300_000)
        if not text:
            continue
        r = rel(root, f)
        code_texts[r] = text
        for m in IDENT_RE.finditer(text):
            identifiers.add(m.group(1))
    blob = "\n".join(code_texts.values())

    findings = []
    for doc_rel in doc_list:
        doc_path = root / doc_rel
        if not doc_path.exists():
            continue
        text = read_text(doc_path, limit=400_000)
        if not text:
            continue
        claims = []
        for m in CODE_SPAN_RE.finditer(text):
            line = text[:m.start()].count("\n") + 1
            claims.append((m.group(1), line))
        for m in FENCE_RE.finditer(text):
            block, base_line = m.group(2), text[:m.start()].count("\n") + 1
            for idx, line in enumerate(block.splitlines()):
                if line.strip():
                    claims.append((line.strip(), base_line + idx))

        seen = set()
        for raw, line in claims:
            kind, value = classify_claim(raw)
            if not kind or (kind, value) in seen:
                continue
            seen.add((kind, value))
            problem = ""
            if kind == "path":
                target = (root / value.lstrip("./")).resolve()
                if not target.exists() and not (doc_path.parent / value).exists():
                    if "/" in value or "\\" in value:
                        problem = "文档引用的路径不存在"
            elif kind == "symbol":
                if value not in identifiers:
                    problem = "文档提到的符号在代码中找不到"
            elif kind == "endpoint":
                if value not in blob:
                    problem = "文档描述的接口在代码中找不到"
            if problem:
                findings.append({
                    "doc": doc_rel, "line": line, "kind": kind,
                    "value": value, "problem": problem,
                })

    # 反向：改动文件被哪些文档提及
    changed = args.changed.split(",") if args.changed else []
    touched = []
    for path in changed:
        path = path.strip()
        if not path:
            continue
        stem = Path(path).stem
        if len(stem) < 4:
            continue
        pat = re.compile(rf"\b{re.escape(stem)}\b")
        for doc_rel in doc_list:
            text = read_text(root / doc_rel, limit=300_000)
            if text and pat.search(text):
                touched.append({"changed": path, "doc": doc_rel})

    payload = {
        "generated_at": now(),
        "docs_scanned": len(doc_list),
        "findings": findings,
        "docs_touching_changes": touched,
    }
    out_dir = ensure_state_dir(root)
    write_json(out_dir / "drift.json", payload)

    md = ["# 文档漂移报告", "",
          f"- 生成时间：{payload['generated_at']}",
          f"- 扫描文档：{len(doc_list)} 个",
          f"- 疑似失配：{len(findings)} 条", ""]
    if touched:
        md.append("## 本次改动涉及的文档（需人工确认是否更新）")
        for t in touched:
            md.append(f"- `{t['changed']}` ← `{t['doc']}`")
        md.append("")
    md.append("## 失配明细")
    if findings:
        md.append("| 文档 | 行 | 类型 | 内容 | 问题 |")
        md.append("|---|---|---|---|---|")
        for f in findings[:120]:
            md.append(f"| `{f['doc']}` | {f['line']} | {f['kind']} | `{f['value']}` | {f['problem']} |")
    else:
        md.append("未发现路径/符号级失配。")
    (out_dir / "drift.md").write_text("\n".join(md), encoding="utf-8")

    print(f"文档漂移 -> {out_dir / 'drift.md'}（失配 {len(findings)} 条，"
          f"本次改动涉及 {len(touched)} 处文档）")
    return 0


# ----------------------------------------------------------------------------- graph

def collect_defs(root: Path, rel_path: str) -> list[str]:
    """取出一个文件对外可见的符号：顶层定义 + 模块名。"""
    text = read_text(root / rel_path, limit=400_000)
    syms: list[str] = []
    for m in DEF_RE.finditer(text):
        syms.append(m.group(1))
    stem = Path(rel_path).stem
    if len(stem) >= 4:
        syms.append(stem)

    out, seen = [], set()
    for s in syms:
        if len(s) < 3 or s.startswith("_"):
            continue
        if s == s.lower() and s in STOPWORDS:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out[:30]


def cmd_graph(args) -> int:
    """把改造清单画成依赖图：谁依赖谁、改动会往哪传。"""
    root = Path(args.root).resolve()
    out_dir = ensure_state_dir(root)

    if args.files:
        targets = [x.strip() for x in args.files.split(",") if x.strip()]
    else:
        impact_path = out_dir / "impact.json"
        if not impact_path.exists():
            print("没有指定文件，也没有 impact.json。先用 impact 生成，或传 --files。")
            return 2
        data = json.loads(impact_path.read_text(encoding="utf-8"))
        targets = [f["file"] for f in data["files"] if f.get("status") != "D"]
    targets = list(dict.fromkeys(targets))
    if not targets:
        print("改造清单里没有可用文件。")
        return 2
    missing = [t for t in targets if not (root / t).exists()]

    files = [f for f in iter_files(root) if f.suffix.lower() in CODE_EXTS]
    rels = [rel(root, f) for f in files]
    level_of: dict[str, int] = {t: 0 for t in targets}
    edges: dict[tuple[str, str], set[str]] = {}
    depth = max(1, min(args.depth, 3))

    # 1) 清单文件之间的互相引用（这是"关联关系"最需要看清的部分）
    target_defs = {t: collect_defs(root, t) for t in targets}
    for a in targets:
        text = read_text(root / a, limit=400_000)
        if not text:
            continue
        for b in targets:
            if a == b:
                continue
            hits = {s for s in target_defs[b]
                    if re.search(rf"\b{re.escape(s)}\b", text)}
            if hits:
                edges.setdefault((a, b), set()).update(hits)

    # 2) 逐层展开外部依赖方
    frontier = list(targets)
    visited = set(targets)
    for d in range(1, depth + 1):
        sym2src: dict[str, str] = {}
        for src in frontier:
            for s in collect_defs(root, src):
                sym2src.setdefault(s, src)
        if not sym2src:
            break

        pattern = re.compile(
            r"\b(" + "|".join(sorted(map(re.escape, sym2src), key=len, reverse=True)) + r")\b")
        nxt: list[str] = []
        for r in rels:
            if r in visited:
                continue
            text = read_text(root / r, limit=400_000)
            if not text:
                continue
            hits: set[str] = set()
            for m in pattern.finditer(text):
                hits.add(m.group(1))
            if not hits:
                continue
            for s in hits:
                src = sym2src[s]
                edges.setdefault((r, src), set()).add(s)
            level_of.setdefault(r, d)
            visited.add(r)
            nxt.append(r)
        frontier = nxt
        if not frontier:
            break

    if not edges:
        print("没有检测到文件间的引用关系。改造清单之间可能是弱耦合，可直接按清单逐条改。")

    nodes = sorted(level_of, key=lambda p: (level_of[p], p))
    if len(nodes) > args.max_nodes:
        keep = nodes[: args.max_nodes]
        cut = len(nodes) - len(keep)
        nodes = keep
    else:
        cut = 0
    index = {p: f"n{i}" for i, p in enumerate(nodes)}

    def label(p: str) -> str:
        return p if len(p) <= 34 else "…" + p[-32:]

    lines = ["flowchart LR"]
    for p in nodes:
        lines.append(f'  {index[p]}["{label(p)}"]')

    shown = 0
    for (src, dst), syms in sorted(edges.items()):
        if src not in index or dst not in index:
            continue
        tag = ", ".join(sorted(syms)[:2])
        lines.append(f'  {index[src]} -->|"{tag}"| {index[dst]}')
        shown += 1

    ascii_lines = ["改造范围（L0）"]
    for tgt in [p for p in nodes if level_of.get(p) == 0]:
        ascii_lines.append(f"├─ {tgt}" + ("　[待新建]" if tgt in missing else ""))
        deps = [(s, v) for (s, d), v in edges.items() if d == tgt]
        for i, (s, v) in enumerate(sorted(deps)[:8]):
            branch = "│   └──" if i == len(deps[:8]) - 1 else "│   ├──"
            ascii_lines.append(f"{branch} ← {s}　(L{level_of.get(s, '?')}，" + ", ".join(sorted(v)[:2]) + ")")
        if len(deps) > 8:
            ascii_lines.append(f"│   └── …… 另有 {len(deps) - 8} 个依赖方")

    lines.append("  classDef target fill:#DBEAFE,stroke:#2563EB,stroke-width:2px,color:#1E3A8A")
    lines.append("  classDef dep fill:#F8FAFC,stroke:#CBD5E1,color:#334155")
    lines.append("  classDef newone fill:#FFFBEB,stroke:#F59E0B,stroke-dasharray:5 3,color:#92400E")
    new_ids = [index[m] for m in missing if m in index]
    if new_ids:
        lines.append("  class " + ",".join(new_ids) + " newone")
    if targets:
        ids = [index[t] for t in targets if t in index]
        if ids:
            lines.append("  class " + ",".join(ids) + " target")
    dep_ids = [index[p] for p in nodes if p not in targets]
    if dep_ids:
        lines.append("  class " + ",".join(dep_ids) + " dep")

    (out_dir / "graph.mmd").write_text("\n".join(lines), encoding="utf-8")

    by_level: dict[int, list[str]] = {}
    for p in nodes:
        by_level.setdefault(level_of[p], []).append(p)

    md = ["# 改造清单依赖图", "",
          f"- 生成时间：{now()}",
          f"- 改造文件：{len(targets)} 个",
          f"- 关联文件：{len(nodes) - len(targets)} 个（展开 {depth} 层）", ""]
    if cut:
        md.append(f"> 节点过多，已按层级截断 {cut} 个；可收紧 --files 或降低 --depth。")
        md.append("")
    if missing:
        md.append("> 以下文件尚未在仓库中，按**待新建**处理（图中为虚线）：")
        for m in missing:
            md.append(f"> - `{m}`")
        md.append("")
    if args.format in ("ascii", "both"):
        md += ["## 终端视图", "", "```", *ascii_lines, "```", ""]
    if args.format in ("mermaid", "both"):
        md += ["## 图（Mermaid）", "", "```mermaid", *lines, "```", ""]
    md += ["## 分层", ""]
    names = {0: "改造范围（S1 清单内）", 1: "直接受影响", 2: "间接受影响", 3: "更远"}
    for lv in sorted(by_level):
        md.append(f"**{names.get(lv, f'第 {lv} 层')}**（{len(by_level[lv])}）")
        for p in by_level[lv][:40]:
            md.append(f"- `{p}`")
        if len(by_level[lv]) > 40:
            md.append(f"- …… 另有 {len(by_level[lv]) - 40} 个")
        md.append("")
    if edges:
        md += ["## 依赖关系", "", "| 依赖方 | 被依赖 | 引用符号 |", "|---|---|---|"]
        for (src, dst), syms in sorted(edges.items())[:120]:
            md.append(f"| `{src}` | `{dst}` | {', '.join(sorted(syms)[:4])} |")
        md.append("")
    (out_dir / "graph.md").write_text("\n".join(md), encoding="utf-8")

    write_json(out_dir / "graph.json", {
        "generated_at": now(),
        "targets": targets,
        "nodes": [{"file": p, "level": level_of[p]} for p in nodes],
        "edges": [{"from": s, "to": d, "symbols": sorted(v)} for (s, d), v in edges.items()],
        "truncated": cut,
    })

    print(f"依赖图 -> {out_dir / 'graph.md'}（{len(nodes)} 个节点，{shown} 条边）")
    if missing:
        print(f"  待新建 {len(missing)} 个：" + "、".join(missing))
    for lv in sorted(by_level):
        print(f"  L{lv} {names.get(lv, '')}：{len(by_level[lv])} 个")
    return 0


# ----------------------------------------------------------------------------- verify

SELFTEST_RULES = [
    (("api", "controller", "route", "handler", "endpoint"), "接口调用",
     "正常入参 / 缺参 / 越权 / 不存在的 id"),
    (("migration", "migrate", ".sql"), "数据库迁移",
     "开发库执行一遍，再回滚一次，确认可重复执行"),
    (("model", "entity", "dao", "repository", "schema"), "数据读写",
     "新数据写入后读回；存量数据能否正常解析"),
    (("job", "task", "scheduler", "cron"), "定时任务",
     "手动触发一次，观察是否重复执行"),
    (("mq", "kafka", "rabbit", "consumer", "producer"), "消息收发",
     "发一条消息，确认消费与失败重试"),
    (("config", "settings", ".env", "yaml", "yml"), "配置加载",
     "换环境启动一次，确认缺配置时有兜底"),
    (("web/", ".vue", ".tsx", ".jsx", "component", "page"), "页面操作",
     "打开页面走一遍主流程，看空态与错误提示"),
    (("auth", "security", "permission", "rbac"), "权限校验",
     "无权限账号访问一次，确认被拦"),
    (("docs/", ".md", ".rst", "readme", "changelog"), "文档",
     "渲染后读一遍：目录、链接、示例代码是否正常"),
]

DEFAULT_SENSITIVE_RULES = [
    ("高危", "数据库迁移", r"(^|/)(migrations?|alembic/versions)/"),
    ("高危", "SQL 脚本", r"\.sql$"),
    ("高危", "密钥/证书", r"\.(pem|key|p12|jks)$"),
    ("高危", "环境配置", r"(^|/)\.env"),
    ("高危", "CI/CD", r"(\.github/workflows/|\.gitlab-ci\.yml|Jenkinsfile|azure-pipelines)"),
    ("高危", "权限鉴权", r"(^|/)(auth|security)/|(permission|rbac|acl)"),
    ("高危", "生产配置", r"(prod|production)[\w.-]*\.(ya?ml|properties|env)$"),
    ("高危", "发布脚本", r"(^|/)(deploy|release|rollback)"),
    ("高危", "依赖锁文件", r"(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|poetry\.lock|go\.sum|Cargo\.lock)"),
    ("中危", "公共基础库", r"(^|/)(common|utils|core|shared|lib)/"),
    ("中危", "依赖清单", r"(package\.json|pom\.xml|requirements\.txt|pyproject\.toml|go\.mod|Cargo\.toml)$"),
    ("中危", "容器与编排", r"(Dockerfile|docker-compose|(^|/)k8s/|(^|/)helm/)"),
]

_RULES_CACHE: list | None = None


def rules_source_path() -> Path:
    return Path(__file__).resolve().parent.parent / "references" / "sensitive-files.md"


def load_sensitive_rules() -> list[tuple[str, str, str]]:
    """规则只有一份：references/sensitive-files.md 里的机器可读块。

    读不到或解析失败才退回内置默认，并给出警告——不让两份规则各自漂移。
    """
    global _RULES_CACHE
    if _RULES_CACHE is not None:
        return _RULES_CACHE

    p = rules_source_path()
    if not p.exists():
        print("[warn] 未找到 references/sensitive-files.md，使用内置默认规则", file=sys.stderr)
        _RULES_CACHE = DEFAULT_SENSITIVE_RULES
        return _RULES_CACHE

    text = read_text(p)
    block = ""
    for m in re.finditer(r"```ya?ml\n(.*?)```", text, re.S):
        if "level:" in m.group(1):
            block = m.group(1)
            break

    rules, cur = [], {}
    for line in block.splitlines():
        s = line.strip()
        if s.startswith("- level:"):
            if cur.get("level") and cur.get("pattern"):
                rules.append((cur["level"], cur.get("label", ""), cur["pattern"]))
            cur = {"level": s.split(":", 1)[1].strip()}
        elif s.startswith("label:") and cur:
            cur["label"] = s.split(":", 1)[1].strip()
        elif s.startswith("pattern:") and cur:
            raw = s.split(":", 1)[1].strip()
            cur["pattern"] = raw.strip("'\"")
    if cur.get("level") and cur.get("pattern"):
        rules.append((cur["level"], cur.get("label", ""), cur["pattern"]))

    valid = []
    for level, label, pat in rules:
        try:
            re.compile(pat)
            valid.append((level, label, pat))
        except re.error:
            print(f"[warn] 规则正则无效，已跳过：{label} {pat}", file=sys.stderr)

    if len(valid) < 5:
        print("[warn] 规则块解析结果过少，改用内置默认规则", file=sys.stderr)
        valid = DEFAULT_SENSITIVE_RULES

    _RULES_CACHE = valid
    return _RULES_CACHE


def classify_sensitive(files: list[str]) -> list[dict]:
    """返回 [{file, level, label}]，按清单顺序取第一个命中。"""
    out = []
    for f in files:
        for level, label, pat in load_sensitive_rules():
            if re.search(pat, f, re.IGNORECASE):
                out.append({"file": f, "level": level, "label": label})
                break
    return out


def git_status_entries(root: Path) -> list[dict]:
    """统一的 git status 解析：不受首行前导空格被 strip 的影响。"""
    rc, out = git(root, "status", "--porcelain", "-uall")
    if rc != 0:
        return []
    entries = []
    for line in out.splitlines():
        m = re.match(r"^\s*(\S{1,2})\s+(.+)$", line)
        if not m:
            continue
        code, path = m.group(1), m.group(2).strip()
        if " -> " in path:
            path = path.split(" -> ")[-1].strip()
        if path.startswith(STATE_DIR + "/"):
            continue
        entries.append({"status": code, "file": path})
    return entries


FEATURE_RULES = [
    ("签名变化", r"^\s*(?:async\s+)?(?:def|function|func|public|private|protected|static)\b.*\(",
     "调用方要跟着改：入参、返回值、异常各验一次"),
    ("表结构/字段", r"^\s*(?:ALTER|CREATE)\s|^\s*[a-z_]+\s*:\s*(?:int|str|bool|float|Decimal|Optional|List)",
     "新字段写入后读回；存量数据能否正常解析"),
    ("分支条件", r"^\s*(?:if|elif|else|switch|case)\b",
     "新条件的两条路径各走一次，确认默认分支没错"),
    ("异常处理", r"^\s*(?:try|except|catch|raise|throw)\b",
     "构造一次失败，看是否被吞掉、提示是否可读"),
    ("权限判断", r"(?:login_required|PreAuthorize|hasRole|check_permission|require_auth|@auth)",
     "用无权限账号访问一次，确认被拦住"),
    ("配置读取", r"(?:getenv|os\.environ|config\[|@Value|process\.env)",
     "缺配置启动一次，看是否有兜底而不是崩"),
]


def changed_lines(root: Path, path: str, base: str) -> list[str]:
    """取某个文件的改动行；未跟踪文件退回整文件。"""
    rc, out = git(root, "diff", base, "-U0", "--", path)
    if rc == 0 and out:
        # 去掉行首的 +/-，否则 ^def 之类的模式永远匹配不上
        return [ln[1:] for ln in out.splitlines()
                if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    return read_text(root / path, limit=400_000).splitlines()


def diff_features(root: Path, files: list[str], base: str) -> list[dict]:
    """从改了什么代码，推出该额外测什么——比只看路径精准一层。"""
    rows = []
    for f in files:
        lines = changed_lines(root, f, base)
        if not lines:
            continue
        for label, pat, how in FEATURE_RULES:
            if any(re.search(pat, ln) for ln in lines):
                rows.append({"scene": label, "how": how, "file": f, "from_diff": True})
    return rows


def build_selftest_checklist(files: list[str]) -> list[dict]:
    seen, rows = set(), []
    for f in files:
        low = f.lower()
        hit = None
        for keys, scene, how in SELFTEST_RULES:
            if any(k in low for k in keys):
                hit = (scene, how)
                break
        scene, how = hit if hit else ("回归", "从调用该模块的入口跑一遍主流程")
        if scene in seen:
            continue
        seen.add(scene)
        rows.append({"scene": scene, "how": how, "file": f})
    return rows[:8]


ENV_ERROR_RE = re.compile(
    r"(No module named|ModuleNotFoundError|command not found|"
    r"不是内部或外部命令|ENOENT|No such file or directory|"
    r"cannot find (the )?path|is not recognized as)", re.IGNORECASE)


def looks_like_env_error(text: str) -> bool:
    """跑不起来 ≠ 测试没过。环境问题不能算代码失败。"""
    return bool(ENV_ERROR_RE.search(text))


def parse_test_summary(text: str) -> dict:
    info = {"passed": 0, "failed": 0, "skipped": 0, "failures": []}
    for pat, key in ((r"(\d+)\s+passed", "passed"), (r"(\d+)\s+failed", "failed"),
                     (r"(\d+)\s+skipped", "skipped")):
        m = re.search(pat, text)
        if m:
            info[key] = int(m.group(1))
    for line in text.splitlines():
        if re.search(r"(FAILED|✕|✗|\bError\b.*Traceback)", line):
            s = line.strip()
            if s and s not in info["failures"]:
                info["failures"].append(s[:160])
    info["failures"] = info["failures"][:15]
    return info


def cmd_verify(args) -> int:
    root = Path(args.root).resolve()
    out_dir = ensure_state_dir(root)
    profile = load_profile(root)
    cmd = args.cmd or (profile.get("commands") or {}).get("test", "")

    changed = [x.strip() for x in args.files.split(",") if x.strip()] if args.files else []
    if not changed:
        changed = [e["file"] for e in git_status_entries(root)]
    checklist = build_selftest_checklist(changed)
    for feat in diff_features(root, changed, args.base):
        if any(r["scene"] == feat["scene"] and r["file"] == feat["file"] for r in checklist):
            continue
        checklist.insert(0, feat)
    checklist = checklist[:10]

    md = ["# 验证报告", "", f"- 生成时间：{now()}", f"- 项目根：`{root}`",
          f"- 涉及改动：{len(changed)} 个文件", ""]

    if not cmd:
        md += ["## 结果", "", "**NOT RUN** —— 没有检测到测试命令。", "",
               "请在 `--cmd` 里指定，例如 `--cmd \"pytest -q\"`，或补齐项目测试脚本。", ""]
        (out_dir / "verify.md").write_text("\n".join(md), encoding="utf-8")
        print("NOT RUN：没有测试命令。用 --cmd 指定后重跑。")
        return 2

    if args.dry_run:
        md += ["## 结果", "", f"**DRY RUN** —— 将要执行：`{cmd}`", ""]
        (out_dir / "verify.md").write_text("\n".join(md), encoding="utf-8")
        print(f"将要执行：{cmd}")
        return 0

    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(root), shell=True, capture_output=True,
                              encoding="utf-8", errors="replace", timeout=args.timeout)
        code = proc.returncode
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        note = ""
    except subprocess.TimeoutExpired:
        code, output, note = 124, "", f"执行超时（>{args.timeout}s）"
    except Exception as exc:  # noqa: BLE001
        code, output, note = 1, "", f"执行失败：{exc}"
    elapsed = round(time.time() - started, 1)

    ok = code == 0
    summary = parse_test_summary(output)
    env_issue = (not ok) and looks_like_env_error(output)
    verdict = "通过" if ok else ("NOT RUN（环境问题）" if env_issue else "未通过")

    md += ["## 结果", "",
           f"- 命令：`{cmd}`",
           f"- 退出码：{code}" + (f"（{note}）" if note else ""),
           f"- 耗时：{elapsed}s",
           f"- 结论：**{verdict}**", ""]
    if env_issue:
        md += ["命令没跑起来，不代表代码有问题。先解决环境：", "",
               "- 确认测试框架已安装（`pip install pytest` / `npm i` / `go mod download`）",
               "- 确认命令在项目根目录可执行",
               f"- 排查后重跑：`python scripts/devloop.py verify --root . --cmd \"{cmd}\"`", ""]
    if summary["passed"] or summary["failed"]:
        md += [f"- 用例：通过 {summary['passed']} / 失败 {summary['failed']} / 跳过 {summary['skipped']}", ""]
    if summary["failures"]:
        md += ["### 失败详情", ""] + [f"- `{f}`" for f in summary["failures"]] + [""]

    md += ["## 建议用户自测清单", "",
           "| ✓ | 场景 | 怎么测 | 涉及文件 |", "|---|---|---|---|"]
    for row in checklist:
        md.append(f"| □ | {row['scene']} | {row['how']} | `{row['file']}` |")
    if not checklist:
        md.append("| □ | 回归 | 从入口跑一遍主流程 | — |")
    md.append("")

    tail = [ln for ln in output.splitlines() if ln.strip()][-40:]
    md += ["## 输出尾部（最后 40 行）", "", "```", *tail, "```", ""]

    (out_dir / "verify.md").write_text("\n".join(md), encoding="utf-8")
    print(f"验证 -> {out_dir / 'verify.md'}（{verdict}，退出码 {code}，{elapsed}s）")
    print(f"  自测清单 {len(checklist)} 项，已写入报告")
    if ok:
        return 0
    return 2 if env_issue else 1


# ----------------------------------------------------------------------------- commit

def latest_tag(root: Path) -> str:
    rc, out = git(root, "describe", "--tags", "--abbrev=0")
    return out if rc == 0 and out else ""


def repo_version(root: Path) -> str:
    """没有 tag 历史时，退而读 CHANGELOG 顶部版本号。"""
    p = root / "CHANGELOG.md"
    if p.exists():
        m = re.search(r"##\s*\[(\d+\.\d+\.\d+)\]", read_text(p))
        if m:
            return m.group(1)
    return ""


def worktree_signature(root: Path, entries: list[dict]) -> str:
    """把"当前工作区的实际内容"压成一个指纹。

    只比文件名不够——清单给人看过之后又改了同一行代码，文件名没变，
    但用户看到的已经不是现在要提交的东西了。
    """
    parts = []
    _, diff = git(root, "diff", "HEAD")
    parts.append(diff)
    for e in entries:
        p = root / e["file"]
        if p.is_file():
            parts.append(f"{e['file']}:{doc_sha(p)}")
    return hashlib.sha1("\n".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


def parse_ver(text: str) -> tuple[int, int, int]:
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def suggest_tag(current: str, bump: str, root: Path | None = None) -> str:
    """建议标签。

    优先看 CHANGELOG 顶部版本：如果它已经超过最近 tag，说明本次内容就是那一版，
    直接用它——按 tag 递增会得出一个和 CHANGELOG 对不上的号。
    """
    tag_ver = parse_ver(current) if current else (0, 0, 0)
    cl_ver = parse_ver(repo_version(root)) if root else (0, 0, 0)
    if cl_ver > tag_ver:
        return "v%d.%d.%d" % cl_ver

    major, minor, patch = tag_ver
    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    return f"v{major}.{minor}.{patch}"


def cmd_commit(args) -> int:
    root = Path(args.root).resolve()
    out_dir = ensure_state_dir(root)

    entries = git_status_entries(root)
    if not entries:
        print("工作区干净，没有可提交的改动。")
        return 2

    _, numstat = git(root, "diff", "HEAD", "--numstat")
    stat = {}
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            add = int(parts[0]) if parts[0].isdigit() else 0
            rm = int(parts[1]) if parts[1].isdigit() else 0
            stat[parts[2]] = (add, rm)
    for e in entries:                      # 未跟踪文件不在 diff 里，按行数记
        if e["file"] in stat:
            continue
        p = root / e["file"]
        if p.is_file():
            try:
                stat[e["file"]] = (len(read_text(p).splitlines()), 0)
            except Exception:
                stat[e["file"]] = (0, 0)
    total_add = sum(v[0] for v in stat.values())
    total_rm = sum(v[1] for v in stat.values())

    groups: dict[str, list[dict]] = {}
    for e in entries:
        top = e["file"].split("/")[0] if "/" in e["file"] else "(根目录)"
        groups.setdefault(top, []).append(e)

    sensitive = classify_sensitive([e["file"] for e in entries])

    deleted = [e["file"] for e in entries if "D" in e["status"]]
    major_reasons = []
    if len(entries) >= 10:
        major_reasons.append(f"改动 {len(entries)} 个文件（≥10）")
    if total_add + total_rm >= 300:
        major_reasons.append(f"增减 {total_add + total_rm} 行（≥300）")
    if deleted:
        major_reasons.append(f"删除 {len(deleted)} 个文件")
    if any(s["level"] == "高危" for s in sensitive):
        high = sorted({s["file"] for s in sensitive if s["level"] == "高危"})
        major_reasons.append("涉及高危文件：" + "、".join(high[:4]))

    tag = ""
    tag_note = ""
    if args.bump:
        last = latest_tag(root)
        ver = repo_version(root)
        if ver and parse_ver(ver) > parse_ver(last):
            # CHANGELOG 已经写了更高的版本，说明本次内容就是那一版
            tag = f"v{ver}"
            tag_note = f"按 CHANGELOG 顶部版本（高于最近标签 {last or '无'}）"
        elif last:
            tag = suggest_tag(last, args.bump, root)
            tag_note = f"基于最近标签 {last}"
        else:
            tag = suggest_tag("", args.bump, root)
            tag_note = "仓库没有版本信息，从 v0.1.0 起"

    md = ["# 提交清单（待确认）", "", f"- 生成时间：{now()}",
          f"- 文件数：{len(entries)}（+{total_add} / -{total_rm}）",
          f"- 分支：{(git(root, 'rev-parse', '--abbrev-ref', 'HEAD')[1]) or '?'}",
          f"- 当前 HEAD：{(git(root, 'rev-parse', '--short', 'HEAD')[1]) or '?'}", ""]

    md += ["## 提交内容", "", "| 状态 | 文件 | 增/删 |", "|---|---|---|"]
    for group, items in sorted(groups.items()):
        md.append(f"| | **{group}**（{len(items)}） | |")
        for e in items:
            a, r = stat.get(e["file"], (0, 0))
            md.append(f"| {e['status']} | `{e['file']}` | +{a}/-{r} |")
    md.append("")

    if sensitive:
        md += ["## 敏感文件提醒", "", "| 级别 | 类别 | 文件 |", "|---|---|---|"]
        for s in sensitive:
            md.append(f"| {s['level']} | {s['label']} | `{s['file']}` |")
        md += ["", "高危文件需逐个确认已获授权，见 `references/sensitive-files.md`。", ""]
    else:
        md += ["## 敏感文件提醒", "", "无。", ""]

    md += ["## 建议提交信息", "", "```", args.message or "（待填写）", "```", ""]

    md += ["## 是否重大改动", ""]
    if major_reasons:
        md += ["**是**：" + "；".join(major_reasons), ""]
        suggested = args.tag or tag or suggest_tag(latest_tag(root), "minor")
        md += [f"建议打标签：`{suggested}`" + (f"（{tag_note}）" if tag_note else ""), ""]
    else:
        md += ["否。按普通提交处理即可。", ""]

    md += ["## 确认项", "", "- [ ] 文件清单无误，没有遗漏或多带",
           "- [ ] 提交信息描述准确",
           "- [ ] 验证已通过（`.devloop/verify.md`）",
           "- [ ] 敏感文件已获授权",
           "- [ ] 重大改动已打标签" if major_reasons else "", ""]

    # 只有"生成清单"这一趟才写文件；--apply 那一趟必须读到上一趟留下的清单，
    # 否则等于自己给自己开路条。
    if not args.apply:
        token = hashlib.sha1(
            ("|".join(sorted(e["file"] for e in entries)) + "\n" + (args.message or "")
             + "\n" + (git(root, "rev-parse", "HEAD")[1] or "")
             + "\n" + worktree_signature(root, entries)).encode("utf-8")
        ).hexdigest()[:16]
        md += ["## 清单指纹", "",
               f"`{token}`", "",
               "用户看过这份清单之后，如果文件集合、提交信息或 HEAD 有任何变化，"
               "指纹就对不上，提交会被拒绝——需要重新生成清单再看一次。", ""]
        (out_dir / "commit-plan.md").write_text("\n".join(md), encoding="utf-8")
        write_json(out_dir / "commit-plan.json", {
            "generated_at": now(), "token": token, "message": args.message or "",
            "files": sorted(e["file"] for e in entries), "major": bool(major_reasons),
            "suggested_tag": tag, "head": git(root, "rev-parse", "HEAD")[1] or "",
            "worktree": worktree_signature(root, entries),
        })
    if not args.apply:
        print(f"提交清单 -> {out_dir / 'commit-plan.md'}（{len(entries)} 个文件，"
              f"{'重大改动' if major_reasons else '普通改动'}）")
    for s in sensitive:
        print(f"  敏感：{s['level']} {s['file']}")

    if not args.apply:
        print("（仅生成清单，未提交。把清单和提交信息给用户过目，")
        print("  用户明确同意后再加 --apply --push --message \"...\" 执行）")
        return 0

    # ---- 提交前的三道校验，任何一道不过都不许提交 ----
    if not args.message:
        print("--apply 需要同时给出 --message（必须是用户确认过的那份），未提交。")
        return 2

    plan_json = out_dir / "commit-plan.json"
    if not plan_json.exists():
        print("没有找到本轮的提交清单。先跑一次不带 --apply 的 commit，把清单给用户过目。")
        return 2
    saved = json.loads(plan_json.read_text(encoding="utf-8"))
    expect = hashlib.sha1(
        ("|".join(saved.get("files", [])) + "\n" + args.message + "\n"
         + (git(root, "rev-parse", "HEAD")[1] or "")
         + "\n" + worktree_signature(root, entries)).encode("utf-8")
    ).hexdigest()[:16]
    if expect != saved.get("token"):
        cur_head = git(root, "rev-parse", "HEAD")[1] or ""
        cur_files = sorted(e["file"] for e in entries)
        cur_wt = worktree_signature(root, entries)
        if saved.get("message") != args.message:
            why = "提交信息与用户看过的清单不一致"
            detail = [f"清单里：{saved.get('message', '')}", f"你给的：{args.message}"]
        elif saved.get("files") != cur_files:
            added = sorted(set(cur_files) - set(saved.get("files", [])))
            gone = sorted(set(saved.get("files", [])) - set(cur_files))
            why = "文件集合在清单生成之后变了"
            detail = [f"新增：{added or '无'}", f"消失：{gone or '无'}"]
        elif saved.get("head") != cur_head:
            why = "HEAD 在清单生成之后变了（有人提交过）"
            detail = [f"清单时：{saved.get('head')}", f"现在：{cur_head}"]
        else:
            why = "文件内容在清单生成之后又被改过（文件名没变，但内容变了）"
            detail = ["用户看到的那版已经不是现在要提交的了"]
        print(f"{why}，拒绝提交。")
        for line in detail:
            print("  " + line)
        print("  重新跑一次不带 --apply 的 commit，把新清单给用户看过再提交。")
        return 2

    if args.push:
        rc, remotes = git(root, "remote")
        if rc != 0 or not remotes.strip():
            print("要求了 --push，但仓库没有配置远程地址，未提交也未推送。")
            print("  先设置远程：git remote add origin <url>")
            return 2

    git(root, "add", "-A", "--", ".", f":(exclude){STATE_DIR}")
    rc, out = git(root, "commit", "-m", args.message)
    if rc != 0:
        print(f"提交失败：{out}")
        return 1
    print(f"已提交：{out.splitlines()[0] if out else ''}")

    if args.tag:
        rc, out = git(root, "tag", "-a", args.tag, "-m", args.message)
        print(f"已打标签 {args.tag}" if rc == 0 else f"打标签失败：{out}")

    if args.push:
        # 没有 upstream 就直接建，别靠解析 stderr 判断要不要加 -u
        if has_upstream(root):
            rc, out = git(root, "push", with_stderr=True)
        else:
            rc, out = git(root, "push", "-u", "origin", "HEAD", with_stderr=True)
        if rc != 0:
            print("推送失败：")
            for line in (out or "（git 没给出原因）").splitlines():
                print("  " + line)
            print("  提交已在本地，修好远程后再 git push。")
            return 1
        print("已推送。")
        if args.tag:
            rc, out = git(root, "push", "origin", args.tag, with_stderr=True)
            print(f"已推送标签 {args.tag}" if rc == 0 else f"推送标签失败：\n  {out}")
    else:
        print("未推送（加 --push 才会推）。")
    return 0


# ----------------------------------------------------------------------------- sensitive

def cmd_sensitive(args) -> int:
    """清单定稿时先扫一遍：哪些文件动之前需要用户授权。"""
    root = Path(args.root).resolve()
    out_dir = ensure_state_dir(root)

    files = [x.strip() for x in args.files.split(",") if x.strip()] if args.files else []
    if not files:
        files = [e["file"] for e in git_status_entries(root)]
    if not files:
        print("没有可检查的文件。用 --files 指定清单里的文件。")
        return 0

    hits = classify_sensitive(files)
    high = [h for h in hits if h["level"] == "高危"]
    mid = [h for h in hits if h["level"] == "中危"]

    md = ["# 敏感文件预检", "", f"- 生成时间：{now()}",
          f"- 检查文件：{len(files)} 个",
          f"- 需授权：{len(hits)} 个（高危 {len(high)} / 中危 {len(mid)}）", ""]
    if hits:
        md += ["| 级别 | 类别 | 文件 | 确认方式 |", "|---|---|---|---|"]
        for h in hits:
            how = "逐文件单独确认" if h["level"] == "高危" else "可批量确认一次"
            md.append(f"| {h['level']} | {h['label']} | `{h['file']}` | {how} |")
        md += ["", "把这些项写进 S1 的「待确认项」，改动前先拿到明确同意。", ""]
    else:
        md += ["无敏感文件，按清单直接改。", ""]
    (out_dir / "sensitive.md").write_text("\n".join(md), encoding="utf-8")

    print(f"敏感预检 -> {out_dir / 'sensitive.md'}（需授权 {len(hits)}：高危 {len(high)} / 中危 {len(mid)}）")
    for h in hits:
        print(f"  {h['level']}  {h['label']}  {h['file']}")
    if high:
        return 3
    if mid:
        return 4
    return 0


# ----------------------------------------------------------------------------- audit

ACTION_MAP = [
    ("增", ("增", "新增", "新建", "add", "create", "new")),
    ("删", ("删", "删除", "移除", "delete", "del", "remove")),
    ("改", ("改", "修改", "调整", "重构", "modify", "update", "refactor", "fix")),
]


def parse_planned_action(text: str) -> str:
    """从 `C1 | 文件 | 动作 | 理由` 里取出动作；取不到返回空串。"""
    parts = [x.strip() for x in text.split("|")]
    if len(parts) >= 3:
        raw = parts[2].lower()
        for canon, keys in ACTION_MAP:
            if any(k in raw for k in keys):
                return canon
    for canon, keys in ACTION_MAP:      # 没有竖线分隔时，退化为全文匹配
        if any(k in text for k in keys):
            return canon
    return ""


def op_matches_status(action: str, status: str) -> bool:
    if action == "增":
        return status in ("A", "?")
    if action == "删":
        return status == "D"
    if action == "改":
        return status in ("M", "R")
    return True


PLAN_PATH_RE = re.compile(
    r"([\w./\\-]+\.(?:py|pyi|js|mjs|cjs|ts|tsx|jsx|vue|java|kt|go|rs|cs|cpp|cc|h|php|"
    r"rb|swift|scala|sql|md|rst|ya?ml|json|xml|toml|gradle|sh|ps1))")


def cmd_audit(args) -> int:
    """把 state 里的改造清单和实际 diff 对账：漏改 / 越界。"""
    root = Path(args.root).resolve()
    out_dir = ensure_state_dir(root)

    state_path = out_dir / "state.json"
    items = []
    if state_path.exists():
        try:
            items = json.loads(state_path.read_text(encoding="utf-8")).get("open_items", [])
        except Exception:
            items = []
    planned: dict[str, dict] = {}
    for it in items:
        if it.get("done"):
            continue
        text = it.get("item", "")
        m = PLAN_PATH_RE.search(text)
        if not m:
            continue
        path = m.group(1).replace("\\", "/")
        entry = planned.setdefault(path, {"items": [], "action": "", "source": "state"})
        entry["items"].append(text)
        if not entry["action"]:
            entry["action"] = parse_planned_action(text)

    # state 里没录清单，就退回 S1 阶段登记时的那份文件集合——否则所有改动都会被判越界
    plan_source = "state.open_items" if planned else ""
    if not planned:
        s1 = load_gates(root).get("S1") or {}
        for f in s1.get("files", []):
            f = f.replace("\\", "/")
            planned[f] = {"items": [f"S1 登记：{f}"], "action": "", "source": "gate"}
        if planned:
            plan_source = "gate S1 的 files（state 里没有明细清单）"

    base = args.base
    rc, diff = git(root, "diff", "--name-status", base)
    if rc != 0:
        print(f"git diff 失败：{diff}")
        return 2
    changed: dict[str, str] = {}
    for line in diff.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            changed[parts[-1]] = parts[0][0]
    for e in git_status_entries(root):          # 新建文件还没被跟踪，diff 里看不到
        changed.setdefault(e["file"], "A" if "?" in e["status"] else e["status"])

    def is_doc(path: str) -> bool:
        low = path.lower()
        return low.startswith(("docs/", "doc/", "documentation/")) or low.endswith((".md", ".rst", ".adoc"))

    scope = getattr(args, "scope", "code")
    if scope != "all":
        want_doc = scope == "docs"
        planned = {p: v for p, v in planned.items() if is_doc(p) == want_doc}
        changed = {p: v for p, v in changed.items() if is_doc(p) == want_doc}

    missing = [p for p in planned if p not in changed]
    extra = [p for p in changed if p not in planned]
    both = [p for p in planned if p in changed]

    # 动作核对：计划写"改"，实际是删除/新建，也算不符
    mismatched = []
    for p in both:
        want = planned[p]["action"]
        got = changed[p]
        if not want or op_matches_status(want, got):
            continue
        mismatched.append({"file": p, "plan": want, "actual": got})

    scope_name = {"code": "代码", "docs": "文档", "all": "全部"}.get(scope, scope)
    md = ["# 变更审计对账", "", f"- 生成时间：{now()}",
          f"- 对比基线：{base}",
          f"- 对账范围：{scope_name}",
          f"- 计划文件：{len(planned)} 个（来自 {plan_source or '无'}）",
          f"- 实际变更：{len(changed)} 个文件", ""]
    if not planned:
        md += ["> 没有可对账的计划：state 里没有未闭环项，S1 也没有登记过文件集合。",
               "> 先跑 `gate --stage S1 --files ...` 或 `state --add \"C1 | 文件 | 动作 | 理由\"`。", ""]

    md += ["## 漏改（计划里有、diff 里没有）", ""]
    if missing:
        for p in missing:
            md.append(f"- `{p}`　← {'；'.join(planned[p]['items'])[:80]}")
    else:
        md.append("无")
    md += ["", "## 越界（diff 里有、计划里没有）", ""]
    if extra:
        for p in extra:
            flag = "（新增文件）" if changed[p] == "A" else ""
            md.append(f"- `{p}` {flag}")
    else:
        md.append("无")
    md += ["", "## 动作不符（文件对上了，做的事不一样）", ""]
    if mismatched:
        names = {"M": "改", "A": "新增", "D": "删除", "R": "重命名"}
        for item in mismatched:
            md.append(f"- `{item['file']}`：计划「{item['plan']}」，实际「{names.get(item['actual'], item['actual'])}」")
    else:
        md.append("无")
    md += ["", "## 一致", ""]
    md += [f"- `{p}`（{changed[p]}）" for p in both if p not in {m['file'] for m in mismatched}] or ["无"]
    md += ["", "> 漏改不等于必须改：确认后可以把该项标为 SKIPPED 并写明原因。", ""]

    fname = "audit.md" if scope == "all" else f"audit-{scope}.md"
    (out_dir / fname).write_text("\n".join(md), encoding="utf-8")
    print(f"对账 -> {out_dir / fname}（漏改 {len(missing)} / 越界 {len(extra)} / "
          f"动作不符 {len(mismatched)} / 一致 {len(both) - len(mismatched)}）")
    for p in missing:
        print(f"  漏改：{p}")
    for p in extra:
        print(f"  越界：{p}")
    for item in mismatched:
        print(f"  动作不符：{item['file']} 计划 {item['plan']} 实际 {item['actual']}")
    return 1 if (missing or mismatched) else 0


# ----------------------------------------------------------------------------- gate

# 进入某阶段之前，上一阶段必须已被用户确认（或显式声明不适用）
GATE_ORDER = ["S1", "S2", "S3", "S4", "S5", "S6"]
PREV_STAGE = {GATE_ORDER[i]: GATE_ORDER[i - 1] for i in range(1, len(GATE_ORDER))}

DEFAULT_DOC = {
    "S1": "S1-impact.md", "S2": "S2-audit.md", "S3": "S3-docs.md",
    "S4": "S4-health.md", "S5": "S5-fix.md", "S6": "commit-plan.md",
}

STAGE_TITLE = {
    "S1": "需求影响面与技术方案",
    "S2": "变更审计",
    "S3": "文档同步",
    "S4": "健康检查",
    "S5": "缺陷定位与修复",
    "S6": "提交",
}

# 每个阶段交给用户过目的东西：标题在不算数，得有实质内容。
# (显示名, 可接受的标题关键词, 规则)
#   rule: {"min_items": n}      表格数据行 / 列表项至少 n 条
#         {"must_contain": str} 小节内必须出现该片段
#         {}                    只要小节非空即可（"无" 也算有效回答）
REQUIRED_SECTIONS = {
    "S1": [("技术路线", ("技术路线", "方案选型", "选型对比"), {"min_items": 2}),
           ("实现流程", ("实现流程", "执行步骤", "实施步骤"), {"min_items": 1}),
           ("架构图", ("架构图",), {"must_contain": "```mermaid"}),
           ("改造清单", ("改造清单",), {"min_items": 1})],
    "S2": [("漏改判定", ("漏改",), {}),
           ("越界判定", ("越界",), {})],
    "S3": [("文档判定", ("文档", "新建", "更新", "不改"), {"min_items": 1})],
    "S4": [("问题清单", ("健康", "问题", "P0", "P1"), {})],
    "S5": [("根因", ("根因",), {}),
           ("修复方案", ("修复",), {})],
    "S6": [("提交清单", ("提交",), {})],
}

HEADING_RE = re.compile(r"^(#{1,6})\s*(.+?)\s*$")
TABLE_SEP_RE = re.compile(r"^\|[\s:\-|]+\|$")


def section_body(text: str, keys: tuple[str, ...]) -> str:
    """取包含关键词的标题下的正文，到下一个标题为止。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line.strip())
        if not m:
            continue
        if any(k in m.group(2) for k in keys):
            body = []
            for nxt in lines[i + 1:]:
                if HEADING_RE.match(nxt.strip()):
                    break
                body.append(nxt)
            return "\n".join(body)
    return ""


def count_items(body: str) -> int:
    """数表格数据行（不含表头与分隔行）与列表项的条数。"""
    lines = [l.strip() for l in body.splitlines()]
    n, i = 0, 0
    while i < len(lines):
        if lines[i].startswith("|"):
            block = []
            while i < len(lines) and lines[i].startswith("|"):
                block.append(lines[i])
                i += 1
            rows = [r for r in block if not TABLE_SEP_RE.match(r)]
            # 有分隔行才说明是标准表格，首行是表头；没有分隔行就全按数据行算
            header = 1 if len(rows) < len(block) else 0
            n += max(0, len(rows) - header)
            continue
        if re.match(r"^([-*+]\s+\S|\d+[.)]\s+\S)", lines[i]):
            n += 1
        i += 1
    return n


def check_doc_sections(stage: str, text: str) -> list[str]:
    """返回不合格项的描述；空列表表示通过。"""
    problems = []
    for label, keys, rule in REQUIRED_SECTIONS.get(stage, []):
        body = section_body(text, keys)
        if not body.strip():
            problems.append(f"{label}（小节为空或只有标题）")
            continue
        if "must_contain" in rule and rule["must_contain"] not in body:
            problems.append(f"{label}（缺少 {rule['must_contain']} 代码块）")
            continue
        need = rule.get("min_items")
        if need:
            got = count_items(body)
            if got < need:
                problems.append(f"{label}（只有 {got} 条，至少 {need} 条）")
    return problems


def gates_path(root: Path) -> Path:
    return ensure_state_dir(root) / "gates.json"


def load_gates(root: Path) -> dict:
    p = gates_path(root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def gate_sig(files: list[str], doc: Path | None) -> str:
    """指纹只覆盖两样：清单文件集合 + 阶段报告内容。

    不覆盖整个工作区——S2 之后代码本来就在改，覆盖 worktree 会让每次批准都失效。
    报告被改过 = 用户看到的已经不是这份了。
    """
    body = "|".join(sorted(files))
    if doc and doc.exists():
        body += "\n" + doc_sha(doc)
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def cmd_gate(args) -> int:
    """阶段确认门：每个阶段产出清单 → 用户过目 → 批准 → 才能进下一阶段。"""
    root = Path(args.root).resolve()
    out_dir = ensure_state_dir(root)
    gates = load_gates(root)

    stage = (getattr(args, "stage", None) or "S1").upper()
    if stage not in GATE_ORDER:
        print(f"未知阶段 {stage}，可选：{'、'.join(GATE_ORDER)}")
        return 2

    do_skip = bool(getattr(args, "skip", False))
    if args.show or (not args.approve and not do_skip and not args.files):
        if not gates:
            print("还没有任何阶段被登记或确认。")
            return 0
        md = ["# 阶段确认状态", "", "| 阶段 | 内容 | 状态 | 时间 | 用户原话 |", "|---|---|---|---|---|"]
        names = {"pending": "**待确认**", "approved": "已确认", "skipped": "不适用"}
        for st in GATE_ORDER:
            g = gates.get(st)
            if not g:
                md.append(f"| {st} | {STAGE_TITLE[st]} | 未登记 | — | — |")
                continue
            md.append(f"| {st} | {STAGE_TITLE[st]} | {names.get(g.get('status'), '?')} | "
                      f"{g.get('at', '')} | {g.get('note', '') or '—'} |")
        md += ["", "> 待确认的阶段，agent 必须停下等用户点头；不许自行推进。", ""]
        (out_dir / "gates.md").write_text("\n".join(md), encoding="utf-8")
        print("\n".join(md))
        return 0

    # ---------------- 批准 / 跳过
    if do_skip and not args.approve and stage not in gates:
        # 阶段不适用时本来就没有清单文件，允许直接标记，但必须说明理由
        if not args.note.strip():
            print(f"--skip 必须带 --note：写清为什么这个阶段不适用，"
                  f"例如 \"本次只改代码，无文档变更\"。")
            return 2
        gates[stage] = {"at": now(), "status": "skipped", "summary": args.summary or "",
                        "files": [], "doc": "", "signature": "", "note": args.note.strip()}
        write_json(gates_path(root), gates)
        nxt = GATE_ORDER[GATE_ORDER.index(stage) + 1] if stage != GATE_ORDER[-1] else None
        print(f"{stage}（{STAGE_TITLE[stage]}）标记为不适用"
              + (f"——可以进入 {nxt}。" if nxt else "——流程结束。"))
        print(f"  用户原话：{args.note.strip()}")
        return 0

    if args.approve or do_skip:
        g = gates.get(stage)
        if not g:
            print(f"{stage} 还没有登记过，没有可确认的清单。")
            print(f"  先登记：gate --stage {stage} --files a.py,b.py --doc <报告>")
            return 2
        if g.get("status") == "approved":
            print(f"{stage} 已经确认过（{g.get('at')}），无需重复确认。")
            return 0
        if not args.note.strip():
            print(f"{'--approve' if args.approve else '--skip'} 必须带 --note：写用户的原话，"
                  f"例如 \"可以，按这个来\" 或 \"这次不涉及文档\"。")
            return 2

        doc = root / g["doc"] if g.get("doc") else None
        now_sig = gate_sig(g.get("files", []), doc)
        if now_sig != g.get("signature"):
            print(f"{stage} 已过期：清单登记之后报告或文件集合又变了，"
                  f"用户看到的那份已经不是现在这份。")
            print("  重新登记 → 再给用户看一次 → 再确认。")
            return 2

        g["status"] = "approved" if args.approve else "skipped"
        g["at"] = now()
        g["note"] = args.note.strip()
        gates[stage] = g
        write_json(gates_path(root), gates)

        nxt = GATE_ORDER[GATE_ORDER.index(stage) + 1] if stage != GATE_ORDER[-1] else None
        if args.approve:
            print(f"{stage}（{STAGE_TITLE[stage]}）已确认"
                  + (f"——可以进入 {nxt}。" if nxt else "——流程结束。"))
        else:
            print(f"{stage}（{STAGE_TITLE[stage]}）已标记为不适用"
                  + (f"——可以进入 {nxt}。" if nxt else "。"))
        print(f"  用户原话：{args.note.strip()}")
        return 0

    # ---------------- 登记待确认
    files = [x.strip() for x in args.files.split(",") if x.strip()]
    if not files:
        print("--files 必填：这个阶段的清单覆盖哪些文件，逗号分隔。")
        return 2

    doc_path = Path(args.doc) if args.doc else (out_dir / DEFAULT_DOC[stage])
    if not doc_path.is_absolute():
        doc_path = root / doc_path
    if not doc_path.exists():
        print(f"找不到阶段报告：{doc_path}")
        print(f"  {stage} 的报告默认是 {DEFAULT_DOC[stage]}，先写报告再登记；")
        print("  报告在别处就用 --doc 指定。没有报告就没有可给用户过目的东西。")
        return 2

    text = read_text(doc_path, limit=1_000_000)
    missing = check_doc_sections(stage, text)
    if missing:
        print(f"{stage} 报告不达要求，不予登记：")
        for item in missing:
            print(f"  - {item}")
        print(f"  报告：{rel(root, doc_path)}")
        print(f"  用户要过目的是 {stage} 的完整产出，不是几个空标题。补齐后再登记。")
        return 2

    signature = gate_sig(files, doc_path)
    gates[stage] = {
        "at": now(), "status": "pending", "summary": args.summary or "",
        "files": sorted(files), "doc": rel(root, doc_path), "signature": signature,
        "note": "",
    }
    write_json(gates_path(root), gates)

    md = [f"# {stage} · {STAGE_TITLE[stage]}（待用户确认）", "",
          f"- 登记时间：{gates[stage]['at']}",
          f"- 说明：{gates[stage]['summary'] or '（未填）'}",
          f"- 报告：`{gates[stage]['doc']}`",
          f"- 文件数：{len(files)}",
          f"- 指纹：`{signature}`", "",
          "## 清单覆盖的文件", "", "| 文件 |", "|---|"]
    for f in sorted(files):
        md.append(f"| `{f}` |")
    md += ["", "## 确认流程", "",
           f"1. 把 `{gates[stage]['doc']}` 全文贴给用户（不是只贴文件名）",
           "2. **停下来等**，不许继续做下一阶段",
           f"3. 用户明确同意后：`gate --stage {stage} --approve --note \"用户原话\"`",
           "4. 这个阶段不适用时：`gate --stage " + stage + " --skip --note \"为什么不需要\"`",
           f"5. 然后才能 `state --set {GATE_ORDER[min(GATE_ORDER.index(stage) + 1, 5)]}`", ""]
    (out_dir / f"{stage}-gate.md").write_text("\n".join(md), encoding="utf-8")

    print(f"{stage} 已登记待确认 -> {out_dir / f'{stage}-gate.md'}（{len(files)} 个文件）")
    if stage == "S1":
        print("  贴之前自查：技术路线是否 ≥2 条、有没有写淘汰理由、架构图是否只画了本次改动相关部分。")
    print(f"  下一步：把 {gates[stage]['doc']} 贴给用户，等他明确同意后再 gate --stage {stage} --approve --note ...")
    return 0


# ----------------------------------------------------------------------------- baseline

def doc_anchors(text: str) -> list[str]:
    """文档里引用的路径 / 符号 / 接口——将来文档失效，靠这些锚点检测。"""
    out = []
    for m in CODE_SPAN_RE.finditer(text):
        kind, val = classify_claim(m.group(1))
        if kind in ("path", "symbol", "endpoint") and val:
            out.append(val)
    return sorted(set(out))[:40]


def doc_sha(path: Path) -> str:
    return hashlib.sha1(read_text(path, limit=2_000_000).encode("utf-8", "replace")).hexdigest()[:16]


def cmd_baseline(args) -> int:
    root = Path(args.root).resolve()
    out_dir = ensure_state_dir(root)
    path = out_dir / "doc-baseline.json"

    if args.check:
        if not path.exists():
            print("还没有文档基线。先跑一次不带 --check 的 baseline。")
            return 2
        data = json.loads(path.read_text(encoding="utf-8"))
        broken, changed = [], []
        for d in data.get("docs", []):
            f = root / d["file"]
            if not f.exists():
                broken.append({"file": d["file"], "why": "文档已不存在"})
                continue
            text = read_text(f, limit=400_000)
            lost = [a for a in d.get("anchors", []) if a and a not in text]
            if lost:
                broken.append({"file": d["file"], "why": "锚点丢失：" + "、".join(lost[:5])})
            elif doc_sha(f) != d.get("sha"):
                changed.append(d["file"])

        md = ["# 文档基线检查", "", f"- 生成时间：{now()}",
              f"- 基线时间：{data.get('generated_at', '?')}",
              f"- 基线提交：{data.get('commit', '?')}",
              f"- 文档数：{len(data.get('docs', []))}", ""]
        md += ["## 失效（需要回 S3）", ""]
        md += [f"- `{b['file']}`：{b['why']}" for b in broken] or ["无"]
        md += ["", "## 内容已变（确认是否与本次代码改动有关）", ""]
        md += [f"- `{f}`" for f in changed] or ["无"]
        md += ["", "> 锚点丢失比内容变化严重：说明文档已经不再描述当前的代码符号。", ""]
        (out_dir / "baseline-check.md").write_text("\n".join(md), encoding="utf-8")

        print(f"基线检查 -> {out_dir / 'baseline-check.md'}（失效 {len(broken)} / 内容已变 {len(changed)}）")
        for b in broken:
            print(f"  失效：{b['file']} —— {b['why']}")
        return 1 if broken else 0

    files = [x.strip() for x in args.files.split(",") if x.strip()] if args.files else []
    if not files:
        profile = load_profile(root)
        files = profile.get("docs", [])
    files = [f for f in files if (root / f).exists()]
    if not files:
        print("没有可记录的文档。用 --files 指定。")
        return 2

    docs = []
    for f in files:
        text = read_text(root / f, limit=400_000)
        docs.append({"file": f, "sha": doc_sha(root / f), "anchors": doc_anchors(text),
                     "lines": len(text.splitlines())})
    payload = {"generated_at": now(), "commit": git(root, "rev-parse", "--short", "HEAD")[1] or "?",
               "docs": docs}
    write_json(path, payload)

    md = ["# 文档基线", "", f"- 生成时间：{payload['generated_at']}",
          f"- 提交：{payload['commit']}", f"- 文档数：{len(docs)}", "",
          "| 文档 | 行数 | 锚点数 |", "|---|---|---|"]
    for d in docs:
        md.append(f"| `{d['file']}` | {d['lines']} | {len(d['anchors'])} |")
    md += ["", "下一轮 S2 之后跑 `baseline --check` 复核；锚点丢失就回 S3。", ""]
    (out_dir / "doc-baseline.md").write_text("\n".join(md), encoding="utf-8")

    print(f"文档基线 -> {path}（{len(docs)} 个文档，{sum(len(d['anchors']) for d in docs)} 个锚点）")
    return 0


# ----------------------------------------------------------------------------- state

def cmd_state(args) -> int:
    root = Path(args.root).resolve()
    path = ensure_state_dir(root) / "state.json"
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    else:
        state = {}
    state.setdefault("stage", "IDLE")
    state.setdefault("history", [])
    state.setdefault("open_items", [])
    state.setdefault("created_at", now())

    if args.set in PREV_STAGE:
        prev = PREV_STAGE[args.set]
        g = load_gates(root).get(prev)
        if not g:
            print(f"不能进入 {args.set}：{prev}（{STAGE_TITLE[prev]}）还没登记过。")
            print(f"  1) 先把 {prev} 的产出写成报告")
            print(f"  2) 登记：gate --stage {prev} --files a.py,b.py --doc <报告>")
            print(f"  3) 把报告贴给用户看，等他明确同意")
            print(f"  4) 用户同意后：gate --stage {prev} --approve --note \"用户原话\"")
            return 3
        if g.get("status") == "pending":
            print(f"不能进入 {args.set}：{prev}（{STAGE_TITLE[prev]}）的清单还没给用户确认过。")
            print(f"  清单在 {rel(root, root / g['doc'])}，先贴给用户，等他点头。")
            print(f"  用户不认同的处理：改清单 → 重新登记 → 再看一次。")
            return 3
        if g.get("status") not in ("approved", "skipped"):
            print(f"不能进入 {args.set}：{prev} 的状态异常（{g.get('status')}）。")
            return 3

    if args.round:
        archived = state.get("open_items", [])
        state.setdefault("rounds", []).append(
            {"at": now(), "name": args.round, "items": archived})
        state["rounds"] = state["rounds"][-20:]
        state["open_items"] = []
        state["round"] = args.round
        state["stage"] = "S1"
        state["history"].append({"at": now(), "stage": "S1", "note": f"新一轮：{args.round}"})
        # 上一轮的报告不能留在原地——新一轮登记时会被误当成这一轮的产出
        out_dir = ensure_state_dir(root)
        arch_dir = out_dir / "archive" / f"round-{len(state['rounds']):02d}"
        moved = 0
        for name in list(DEFAULT_DOC.values()) + [
                "commit-plan.json", "verify.md", "gates.md", "sensitive.md",
                "audit-code.md", "audit-docs.md", "graph.md", "impact.md",
                "drift.md", "doc-baseline.json", "baseline-check.md"]:
            f = out_dir / name
            if f.exists():
                arch_dir.mkdir(parents=True, exist_ok=True)
                f.replace(arch_dir / name)
                moved += 1
        for stale in [gates_path(root)] + [out_dir / f"{st}-gate.md" for st in GATE_ORDER]:
            if stale.exists():
                stale.unlink()
        extra = f"，归档上一轮产物 {moved} 个到 archive/round-{len(state['rounds']):02d}" if moved else ""
        print(f"新一轮「{args.round}」：已归档上一轮清单（{len(archived)} 项）{extra}，阶段重置为 S1。")

    if args.set:
        state["stage"] = args.set
        state["history"].append({"at": now(), "stage": args.set, "note": args.note or ""})
        state["history"] = state["history"][-40:]
    if args.note and not args.set:
        state["note"] = args.note
    if args.add:
        state["open_items"].append({"item": args.add, "at": now(), "done": False})
    if args.close is not None:
        if 0 <= args.close < len(state["open_items"]):
            state["open_items"][args.close]["done"] = True
    state["updated_at"] = now()
    write_json(path, state)

    if args.show or not (args.set or args.add or args.close is not None
                         or args.note or args.round):
        gates = load_gates(root)
        print(json.dumps({
            "stage": state["stage"],
            "round": state.get("round", ""),
            "note": state.get("note", ""),
            "gates": {k: v.get("status") for k, v in gates.items()},
            "open_items": [i for i in state["open_items"] if not i["done"]],
            "history": state["history"][-6:],
        }, ensure_ascii=False, indent=2))
        return 0
    print(f"stage={state['stage']}  未闭环项 {len([i for i in state['open_items'] if not i['done']])} 个")
    return 0


# ----------------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="devloop", description="研发闭环工具箱")
    p.add_argument("--version", action="version", version=f"devloop {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="项目画像")
    s.add_argument("--root", default=".")
    s.set_defaults(func=cmd_scan)

    i = sub.add_parser("impact", help="变更影响面")
    i.add_argument("--root", default=".")
    i.add_argument("--base", default="HEAD", help="对比基线，如 HEAD、HEAD~1、main")
    i.add_argument("--staged", action="store_true", help="只分析暂存区")
    i.add_argument("--files", default="", help="直接指定文件，逗号分隔")
    i.set_defaults(func=cmd_impact)

    d = sub.add_parser("drift", help="文档漂移")
    d.add_argument("--root", default=".")
    d.add_argument("--docs", default="", help="文档列表，逗号分隔")
    d.add_argument("--changed", default="", help="本次改动的代码文件，逗号分隔")
    d.set_defaults(func=cmd_drift)

    g = sub.add_parser("graph", help="改造清单依赖图")
    g.add_argument("--root", default=".")
    g.add_argument("--files", default="", help="改造清单文件，逗号分隔；省略则读 impact.json")
    g.add_argument("--depth", type=int, default=1, help="向下展开层数，1-3，默认 1")
    g.add_argument("--max-nodes", type=int, default=40, help="节点上限，默认 40")
    g.add_argument("--format", choices=["ascii", "mermaid", "both"], default="both",
                   help="输出形态：终端视图 / Mermaid / 两者都要，默认 both")
    g.set_defaults(func=cmd_graph)

    v = sub.add_parser("verify", help="跑测试并生成自测清单")
    v.add_argument("--root", default=".")
    v.add_argument("--cmd", default="", help="测试命令，省略则用 profile 里检测到的")
    v.add_argument("--files", default="", help="只针对这些改动文件生成自测清单")
    v.add_argument("--timeout", type=int, default=900, help="超时秒数，默认 900")
    v.add_argument("--base", default="HEAD", help="取自测特征时的对比基线，默认 HEAD")
    v.add_argument("--dry-run", action="store_true", help="只显示将执行的命令")
    v.set_defaults(func=cmd_verify)

    se = sub.add_parser("sensitive", help="敏感文件预检")
    se.add_argument("--root", default=".")
    se.add_argument("--files", default="", help="清单里的文件，逗号分隔；省略则取工作区改动")
    se.set_defaults(func=cmd_sensitive)

    au = sub.add_parser("audit", help="清单与 diff 对账")
    au.add_argument("--root", default=".")
    au.add_argument("--base", default="HEAD", help="对比基线，默认 HEAD")
    au.add_argument("--scope", choices=["code", "docs", "all"], default="code",
                    help="对账范围：code 只对代码项（S2），docs 只对文档项（S3），默认 code")
    au.set_defaults(func=cmd_audit)

    b = sub.add_parser("baseline", help="文档基线：记录或复核")
    b.add_argument("--root", default=".")
    b.add_argument("--files", default="", help="要记录的文档，逗号分隔；省略则用 profile 里的文档清单")
    b.add_argument("--check", action="store_true", help="复核基线是否失效")
    b.set_defaults(func=cmd_baseline)

    c = sub.add_parser("commit", help="生成提交清单（默认不提交）")
    c.add_argument("--root", default=".")
    c.add_argument("--message", default="", help="建议的提交信息")
    c.add_argument("--bump", choices=["patch", "minor", "major"], default="",
                   help="给出标签递增建议")
    c.add_argument("--apply", action="store_true", help="用户确认后才加：执行 git commit")
    c.add_argument("--tag", default="", help="提交后要打的标签，如 v1.3.0")
    c.add_argument("--push", action="store_true",
                   help="提交后推送到远程；需与 --apply 一起，且用户已确认")
    c.set_defaults(func=cmd_commit)

    g = sub.add_parser("gate", help="阶段确认门：登记 / 用户确认 / 标记不适用")
    g.add_argument("--root", default=".")
    g.add_argument("--stage", default="S1", help="阶段 S1-S6，默认 S1")
    g.add_argument("--files", default="", help="本阶段清单覆盖的文件，逗号分隔")
    g.add_argument("--doc", default="", help="本阶段报告文件，默认 .devloop/<阶段>-gate.md")
    g.add_argument("--summary", default="", help="一句话说明")
    g.add_argument("--approve", action="store_true", help="用户确认后标记为已确认")
    g.add_argument("--skip", action="store_true", help="本阶段不适用（也要给原话）")
    g.add_argument("--note", default="", help="用户原话，确认或跳过时必填")
    g.add_argument("--show", action="store_true", help="查看各阶段确认状态")
    g.set_defaults(func=cmd_gate)

    pl = sub.add_parser("plan", help="兼容别名：等同 gate --stage S1")
    pl.add_argument("--root", default=".")
    pl.add_argument("--files", default="")
    pl.add_argument("--summary", default="")
    pl.add_argument("--approve", action="store_true")
    pl.add_argument("--note", default="")
    pl.set_defaults(func=cmd_gate, stage="S1")

    st = sub.add_parser("state", help="阶段状态")
    st.add_argument("--root", default=".")
    st.add_argument("--set", default="", help="设置当前阶段，如 S1/S2/S3/S4/S5")
    st.add_argument("--note", default="")
    st.add_argument("--round", default="", help="开新一轮需求：归档旧清单、重置到 S1")
    st.add_argument("--add", default="", help="新增未闭环项")
    st.add_argument("--close", type=int, default=None, help="关闭第 N 个未闭环项")
    st.add_argument("--show", action="store_true")
    st.set_defaults(func=cmd_state)
    return p


def main() -> int:
    reconfigure_stdout()
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"devloop 执行失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
