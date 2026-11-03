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
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

VERSION = "1.2.0"
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
CODE_SPAN_RE = re.compile(r"`([^`\n]{2,120})`")
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


def git(root: Path, *args: str):
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        return proc.returncode, proc.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return 1, f"<git failed: {exc}>"


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
    targets = [t for t in dict.fromkeys(targets) if (root / t).exists()]
    if not targets:
        print("改造清单里没有可用文件。")
        return 2

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

    lines.append("  classDef target fill:#DBEAFE,stroke:#2563EB,stroke-width:2px,color:#1E3A8A")
    lines.append("  classDef dep fill:#F8FAFC,stroke:#CBD5E1,color:#334155")
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
    md += ["## 图", "", "```mermaid", *lines, "```", "", "## 分层", ""]
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
    for lv in sorted(by_level):
        print(f"  L{lv} {names.get(lv, '')}：{len(by_level[lv])} 个")
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

    if args.show or not (args.set or args.add or args.close is not None or args.note):
        print(json.dumps({
            "stage": state["stage"],
            "note": state.get("note", ""),
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
    g.set_defaults(func=cmd_graph)

    st = sub.add_parser("state", help="阶段状态")
    st.add_argument("--root", default=".")
    st.add_argument("--set", default="", help="设置当前阶段，如 S1/S2/S3/S4/S5")
    st.add_argument("--note", default="")
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
