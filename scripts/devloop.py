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
  python devloop.py state  --root . --set S3 --note "改完 3 个文件"
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 同目录的辅助模块：以脚本方式运行时 sys.path[0] 就是这里，
# 被当模块导入时要自己补一次。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import astindex  # noqa: E402  符号索引与盲区检测
import quality as quality_mod  # noqa: E402  实现质量扫描
import report as report_mod
import diagram as diagram_mod  # noqa: E402  Markdown → HTML 报告

VERSION = "1.14.0"
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
        # core.quotepath=false：中文文件名否则会被转成 "\346\226..."
        # 这类八进制转义，路径前缀判断（比如排除 .devloop/）全部失效
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=false", *args], cwd=str(root),
            capture_output=True, encoding="utf-8", errors="replace", timeout=60,
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


# 1.12 起产物文件名改成中文；旧项目的英文名文件在首次运行时自动改名
LEGACY_ARTIFACTS = {
    "S0-clarify.md": "S0-需求澄清.md", "S1-impact.md": "S1-影响面与方案.md",
    "S1-draft.md": "S1-素材.md", "S2-tdd.md": "S2-实现.md",
    "S3-audit.md": "S3-变更审计.md", "S4-docs.md": "S4-文档同步.md",
    "S5-health.md": "S5-健康检查.md", "S6-fix.md": "S6-缺陷定位.md",
    "commit-plan.md": "S7-提交清单.md", "commit-plan.json": "S7-提交清单.json",
    "profile.json": "项目画像.json", "state.json": "状态.json",
    "gates.json": "确认记录.json", "gates.md": "确认状态.md",
    "report.html": "看板.html",
    "blind-spots.md": "静态盲区.md", "blind-spots.json": "静态盲区.json",
    "impact.md": "影响面.md", "impact.json": "影响面.json",
    "graph.md": "依赖图.md", "graph.mmd": "依赖图.mmd", "graph.json": "依赖图.json",
    "quality.md": "实现质量.md", "quality.json": "实现质量.json",
    "sensitive.md": "敏感文件预检.md",
    "audit-code.md": "代码对账.md", "audit-docs.md": "文档对账.md",
    "doc-baseline.json": "文档基线.json", "doc-baseline.md": "文档基线.md",
    "baseline-check.md": "基线复核.md",
    "drift.md": "文档漂移.md", "drift.json": "文档漂移.json",
    "trace.md": "调用链映射.md", "verify.md": "验证报告.md",
}


def migrate_artifacts(root: Path) -> None:
    """把旧版英文产物名改成中文名，并同步确认记录里的 doc 路径。

    只在真的有旧文件时才动，改完写一个隐藏标记，后续调用直接返回。
    """
    out = root / STATE_DIR
    if not out.is_dir() or (out / ".中文名").exists():
        return
    moved = 0
    for old, new in LEGACY_ARTIFACTS.items():
        src, dst = out / old, out / new
        if src.exists() and not dst.exists():
            try:
                src.replace(dst)
                moved += 1
            except OSError:
                pass
    for old, new in (("revisions", "历史版本"), ("history", "看板历史"), ("archive", "归档")):
        src, dst = out / old, out / new
        if src.is_dir() and not dst.exists():
            try:
                src.replace(dst)
                moved += 1
            except OSError:
                pass
    for gate_file in (out / "确认记录.json", out / "gates.json"):
        if gate_file.exists():
            try:
                gates = json.loads(gate_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            for rec in gates.values():
                if isinstance(rec, dict) and rec.get("doc"):
                    for old, new in LEGACY_ARTIFACTS.items():
                        if rec["doc"].endswith("/" + old):
                            rec["doc"] = rec["doc"][: -len(old)] + new
            write_json(gate_file, gates)
    # 归档目录里的 round-01 -> 第01轮
    arch = out / "归档"
    if arch.is_dir():
        for sub in arch.iterdir():
            m = re.fullmatch(r"round-(\d+)", sub.name)
            if m and sub.is_dir() and not (arch / f"第{m.group(1)}轮").exists():
                sub.replace(arch / f"第{m.group(1)}轮")
    (out / ".中文名").write_text("产物文件名已切换为中文\n", encoding="utf-8")
    if moved:
        print(f"[迁移] {moved} 个产物文件已改用中文名（.devloop/ 下）")


def ensure_state_dir(root: Path) -> Path:
    d = root / STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    migrate_artifacts(root)
    return d


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_profile(root: Path) -> dict:
    p = root / STATE_DIR / "项目画像.json"
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

    out = ensure_state_dir(root) / "项目画像.json"
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

    # 符号表只有一份，放在 astindex，按语言分派（Java/TS/Go 各有各的写法）
    syms.extend(astindex.symbol_names(text, Path(item["path"]).suffix))
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
    write_json(out_dir / "影响面.json", payload)

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
    # 静态分析的盲区：脚本看不见的调用，必须在这里讲清楚，不能让人误以为清单是完整的
    spots = astindex.blind_in_files(root, [r["file"] for r in results])
    if spots:
        by_cat: dict[str, list[dict]] = {}
        for s in spots:
            by_cat.setdefault(s["category"], []).append(s)
        md += ["## 静态分析看不见的部分", "",
               "以下位置是动态调用/反射/DI/拼装路由，**上面的调用方清单在这里不成立**，"
               "必须人工确认：", "",
               "| 类别 | 位置 | 说明 |", "|---|---|---|"]
        for cat in sorted(by_cat):
            for s in by_cat[cat][:6]:
                md.append(f"| {cat} | `{s['file']}:{s['line']}` | {s['why']} |")
            if len(by_cat[cat]) > 6:
                md.append(f"| {cat} | 另有 {len(by_cat[cat]) - 6} 处 | 见 `静态盲区.md` |")
        md += ["", "> 跨服务的调用、配置驱动的行为，静态分析一律看不见，只能人工补。", ""]

    (out_dir / "影响面.md").write_text("\n".join(md), encoding="utf-8")

    print(f"影响面 -> {out_dir / '影响面.md'}（风险 {level}，{len(results)} 个文件）")
    for r in results:
        print(f"  [{r['risk']:>3}] {r['file']}  调用方 {len(r['refs']['code'])} / "
              f"测试 {len(r['refs']['tests'])} / 文档 {len(r['refs']['docs'])}")
    if spots:
        print(f"  另有 {len(spots)} 处动态调用看不见，已列进报告，需人工确认")
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
    write_json(out_dir / "文档漂移.json", payload)

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
    (out_dir / "文档漂移.md").write_text("\n".join(md), encoding="utf-8")

    print(f"文档漂移 -> {out_dir / '文档漂移.md'}（失配 {len(findings)} 条，"
          f"本次改动涉及 {len(touched)} 处文档）")
    return 0


# ----------------------------------------------------------------------------- graph

def collect_defs(root: Path, rel_path: str) -> list[str]:
    """取出一个文件对外可见的符号：顶层定义 + 模块名。

    Python 走 ast（准确、能拿到真实定义），其余语言退回正则。
    """
    text = read_text(root / rel_path, limit=400_000)
    syms: list[str] = []

    suffix = Path(rel_path).suffix.lower()
    if suffix in astindex.PY_EXTS:
        info = astindex.index_file(root / rel_path)
        if info.get("engine") == "ast":
            # 用短名：调用方代码里出现的是 get_user，不是 UserService.get_user
            syms = [d["short"] for d in info.get("defs", [])]
        else:
            # 语法错误等情况退回正则，不让分析中断
            syms = astindex.symbol_names(text, suffix)
    else:
        syms = astindex.symbol_names(text, suffix)

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
        impact_path = out_dir / "影响面.json"
        if not impact_path.exists():
            print("没有指定文件，也没有 影响面.json。先用 impact 生成，或传 --files。")
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

    (out_dir / "依赖图.mmd").write_text("\n".join(lines), encoding="utf-8")

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
    (out_dir / "依赖图.md").write_text("\n".join(md), encoding="utf-8")

    write_json(out_dir / "依赖图.json", {
        "generated_at": now(),
        "targets": targets,
        "nodes": [{"file": p, "level": level_of[p]} for p in nodes],
        "edges": [{"from": s, "to": d, "symbols": sorted(v)} for (s, d), v in edges.items()],
        "truncated": cut,
    })

    print(f"依赖图 -> {out_dir / '依赖图.md'}（{len(nodes)} 个节点，{shown} 条边）")
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
        # 万一还是带引号的转义形式，拆掉引号再判断
        path = path.strip('"')
        if path.startswith(STATE_DIR + "/") or path.startswith(STATE_DIR + "\\"):
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
        (out_dir / "验证报告.md").write_text("\n".join(md), encoding="utf-8")
        print("NOT RUN：没有测试命令。用 --cmd 指定后重跑。")
        return 2

    if args.dry_run:
        md += ["## 结果", "", f"**DRY RUN** —— 将要执行：`{cmd}`", ""]
        (out_dir / "验证报告.md").write_text("\n".join(md), encoding="utf-8")
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

    (out_dir / "验证报告.md").write_text("\n".join(md), encoding="utf-8")
    print(f"验证 -> {out_dir / '验证报告.md'}（{verdict}，退出码 {code}，{elapsed}s）")
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
    # 不用 `git diff HEAD`：新文件在 `git add` 之前不出现、之后才出现，
    # 同一份内容会因为「暂存了没有」算出两个指纹，清单刚出就被判成过期。
    # 改成对「所有变更文件的内容」取指纹——和暂存状态无关。
    rc, listed = git(root, "ls-files", "-m", "-o", "--exclude-standard")
    files = {x.strip() for x in listed.splitlines() if x.strip()} if rc == 0 else set()
    # 自己的产物不算「改动」：没有 .gitignore 的仓库里 .devloop/ 是未跟踪的，
    # 清单文件本身还会在生成瞬间变一次，算进去等于让每份清单一出就过期
    files = {f for f in files if not f.startswith(STATE_DIR + "/")}
    files |= {e["file"] for e in entries}
    parts = []
    for f in sorted(files):
        p = root / f
        parts.append(f"{f}:{doc_sha(p) if p.is_file() else 'deleted'}")
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
           "- [ ] 验证已通过（`.devloop/验证报告.md`）",
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
        (out_dir / "S7-提交清单.md").write_text("\n".join(md), encoding="utf-8")
        write_json(out_dir / "S7-提交清单.json", {
            "generated_at": now(), "token": token, "message": args.message or "",
            "files": sorted(e["file"] for e in entries), "major": bool(major_reasons),
            "suggested_tag": tag, "head": git(root, "rev-parse", "HEAD")[1] or "",
            "worktree": worktree_signature(root, entries),
        })
    if not args.apply:
        print(f"提交清单 -> {out_dir / 'S7-提交清单.md'}（{len(entries)} 个文件，"
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

    plan_json = out_dir / "S7-提交清单.json"
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
    (out_dir / "敏感文件预检.md").write_text("\n".join(md), encoding="utf-8")

    print(f"敏感预检 -> {out_dir / '敏感文件预检.md'}（需授权 {len(hits)}：高危 {len(high)} / 中危 {len(mid)}）")
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

    state_path = out_dir / "状态.json"
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

    # 文件名叫「代码对账.md / 文档对账.md / 全部对账.md」，和 else 分支的 scope_name 对齐
    fname = f"{scope_name if scope != 'all' else '全部'}对账.md"
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
GATE_ORDER = ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]
PREV_STAGE = {GATE_ORDER[i]: GATE_ORDER[i - 1] for i in range(1, len(GATE_ORDER))}

# 旧版本（1.10 及以前）是 S1..S6，语义为「影响面/审计/文档/健康/定位/提交」。
# 升级后插入 S0 需求澄清与 S2 实现，旧键整体后移一位，见 migrate_gates。
GATE_SCHEMA = 2

DEFAULT_DOC = {
    "S0": "S0-需求澄清.md", "S1": "S1-影响面与方案.md", "S2": "S2-实现.md",
    "S3": "S3-变更审计.md", "S4": "S4-文档同步.md", "S5": "S5-健康检查.md",
    "S6": "S6-缺陷定位.md", "S7": "S7-提交清单.md",
}

STAGE_TITLE = {
    "S0": "需求澄清",
    "S1": "需求影响面与技术方案",
    "S2": "实现（TDD）",
    "S3": "变更审计",
    "S4": "文档同步",
    "S5": "健康检查",
    "S6": "缺陷定位与修复",
    "S7": "提交",
}

# 每个阶段交给用户过目的东西：标题在不算数，得有实质内容。
# (显示名, 可接受的标题关键词, 规则)
#   rule: {"min_items": n}      表格数据行 / 列表项至少 n 条
#         {"must_contain": str} 小节内必须出现该片段
#         {}                    只要小节非空即可（"无" 也算有效回答）
REQUIRED_SECTIONS = {
    "S0": [("需求原话", ("需求原话", "原始诉求", "用户原话"), {}),
           ("问答表", ("问答", "追问"), {"min_items": 4}),
           ("验收标准", ("验收标准", "验收"), {"min_items": 1}),
           ("待定与假设", ("待定", "假设", "未知"), {})],
    "S1": [("技术路线", ("技术路线", "方案选型", "选型对比"), {"min_items": 2}),
           ("实现流程", ("实现流程", "执行步骤", "实施步骤"), {"min_items": 1}),
           ("架构图", ("架构图",), {"must_contain": "```mermaid"}),
           ("改造清单", ("改造清单",), {"min_items": 1}),
           # 动态调用那一类静态查不到，必须在方案里交代怎么兜
           ("静态盲区", ("盲区", "静态分析"), {}),
           ("实现约束", ("实现约束", "编码约束", "质量要求"), {})],
    "S2": [("验收标准与测试映射", ("映射",), {"min_items": 1}),
           ("测试结果", ("测试结果", "全量测试", "验证结果"), {}),
           ("回访确认", ("回访", "满足"), {})],
    "S3": [("漏改判定", ("漏改",), {}),
           ("越界判定", ("越界",), {}),
           ("实现质量", ("实现质量", "质量",), {})],
    "S4": [("文档判定", ("文档", "新建", "更新", "不改"), {"min_items": 1})],
    "S5": [("问题清单", ("健康", "问题", "P0", "P1"), {})],
    "S6": [("根因", ("根因",), {}),
           ("修复方案", ("修复",), {})],
    "S7": [("提交清单", ("提交",), {})],
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
    return ensure_state_dir(root) / "确认记录.json"


def _cell_col(header: list[str], *keys: str) -> int | None:
    for i, h in enumerate(header):
        if any(k in h for k in keys):
            return i
    return None


def table_rows(body: str) -> tuple[list[str], list[list[str]]]:
    """把小节里第一张表解析成 (表头, 数据行)，单元格去空白。没有表就是 ([], [])。"""
    block: list[str] = []
    for line in body.splitlines():
        if line.strip().startswith("|"):
            block.append(line.strip())
        elif block:
            break
    if not block:
        return [], []
    rows = [[c.strip() for c in line.strip("|").split("|")] for line in block]
    if len(rows) >= 2 and TABLE_SEP_RE.match(block[1].strip()):
        return rows[0], rows[2:]
    return rows[0], rows[1:]


# S0 追问要覆盖的四类；缺任何一类都说明需求还没问清
CLARIFY_CATEGORIES = {
    "目标范围": ("目标", "范围", "目的", "场景", "给谁"),
    "边界异常": ("边界", "异常", "空值", "并发", "上限", "失败", "幂等", "重试"),
    "验收标准": ("验收", "标准", "怎么算", "如何判断"),
    "约束兼容": ("约束", "兼容", "性能", "安全", "规范", "不能", "存量", "迁移"),
}

# 这些填法都等于「没填」：空白、占位、以及声称「一次过」——最后这种其实是没走红阶段
_EMPTY_ANSWERS = {"", "-", "—", "待定", "不知道", "todo", "tbd", "待补", "待填",
                  "一次过", "直接通过", "直接绿", "不用", "n/a"}
_PLACEHOLDERS = ("待填", "待补", "todo", "tbd", "占位", "（待", "(待")


def _blank(cell: str) -> bool:
    """这一格是不是没填：空、占位符、「-」，或者还留着模板里的「（待填…）」。"""
    c = cell.strip().lower()
    if c in _EMPTY_ANSWERS:
        return True
    return any(k in c for k in _PLACEHOLDERS)


def check_clarify(text: str) -> list[str]:
    """S0 的收敛判据：四类都问到、每条都有用户答案。

    只要求「填过表」不够——空答案等于没问。所以这里逐行核答案列，
    并要求四类里至少覆盖三类（同一类问三次不算问清）。
    """
    problems = []
    body = section_body(text, ("问答", "追问"))
    header, rows = table_rows(body)
    if len(rows) < 4:
        problems.append(f"问答表只有 {len(rows)} 行，至少 4 行（四类各问一条）")
        return problems

    ans = _cell_col(header, "答案", "回复", "结论")
    cat = _cell_col(header, "类别", "维度", "类型")
    empty = [str(i + 1) for i, r in enumerate(rows)
             if ans is not None and ans < len(r) and _blank(r[ans])]
    if empty:
        problems.append(f"问答表第 {'、'.join(empty)} 行没有用户答案（问了没答不算问清）")

    if cat is not None:
        asked = set()
        for r in rows:
            cell = r[cat] if cat < len(r) else ""
            for name, keys in CLARIFY_CATEGORIES.items():
                if any(k in cell for k in keys):
                    asked.add(name)
        if len(asked) < 3:
            problems.append(f"四类问题只问到 {'、'.join(sorted(asked)) or '无'}，"
                            f"目标范围/边界异常/验收标准/约束兼容 至少覆盖三类")
    return problems


def check_tdd(text: str) -> list[str]:
    """S2 的 TDD 判据：每条验收标准都有测试，且留了先红后绿的证据。

    一张表同时承载映射与证据（验收标准 | 测试 | 先红 | 后绿）。
    只写「已实现」而不写失败输出，说明没走红阶段——那是先写代码再补测试。
    """
    problems = []
    body = section_body(text, ("映射",))
    header, rows = table_rows(body)
    if not rows:
        problems.append("验收标准与测试映射表是空的：每条验收标准都要落到一个测试上")
        return problems

    t_col = _cell_col(header, "测试")
    r_col = _cell_col(header, "红", "失败")
    g_col = _cell_col(header, "绿", "通过")
    a_col = _cell_col(header, "验收", "标准", "AC")

    for i, r in enumerate(rows, 1):
        def cell(c: int | None) -> str:
            return (r[c] if c is not None and c < len(r) else "").strip()

        ac = cell(a_col)
        tag = (ac[:18] + "…") if len(ac) > 18 else (ac or f"第 {i} 行")
        if t_col is not None and _blank(cell(t_col)):
            problems.append(f"{tag}：没有对应测试")
        if r_col is not None and _blank(cell(r_col)):
            problems.append(f"{tag}：缺红色证据（先写失败测试并留输出，再实现）")
        if g_col is not None and _blank(cell(g_col)):
            problems.append(f"{tag}：缺绿色证据（实现后跑通的输出）")

    # 回访确认要真的有一条记录：光把「去问用户」这句说明留着不算
    back = section_body(text, ("回访", "满足"))
    if count_items(back) < 1 or not any(k in back for k in
                                        ("确认", "满足", "同意", "打回", "「", "用户")):
        problems.append("回访确认：没有记录用户对「实现是否满足需求」的答复")
    return problems


# 「继续」「好的」「嗯」这类不是对方案的确认，只是让人往下走。
# 用户中断后回来说一句「继续」，agent 拿它当批准直接进下一阶段——这是本项目
# 真实踩过的坑（S1 方案没再看一眼就进了 S2，看板还停在待确认）。
# 只拦「让人往下走」这类话，不拦真正的回答：
# 「可以」是在回答「按 B 方案、改 3 个文件，可以吗？」，算确认；
# 「继续」不是——它只说明用户想往前走，没说明他看过报告。
LOW_INFO_NOTES = {
    "继续", "继续吧", "继续做", "继续执行", "继续推进", "接着", "接着做", "接着干",
    "往下", "往下走", "下一步", "下一步吧", "走下去", "go", "next", "continue", "resume",
    "嗯", "嗯嗯", "唔", "呃",
}


def note_is_thin(note: str) -> bool:
    """这条「用户原话」是不是只够表示「往下走」，不足以表示「我看过这份方案」。"""
    s = re.sub(r"[\s。，,！!~～.、]", "", note or "").lower()
    return (not s) or s in LOW_INFO_NOTES


def refresh_reports(root: Path, stage: str | None = None) -> dict:
    """看板 + 单阶段视图一起重渲染。

    阶段状态一变（登记/确认/跳过/退回/切阶段），页面必须跟着变——
    否则用户看到的是「待确认」，实际早就批准了，或者反过来。
    """
    out = {"full": None, "focused": None}
    if not (root / STATE_DIR).is_dir():
        return out
    try:
        out["full"] = report_mod.write_report(root, VERSION)["path"]
        if stage:
            out["focused"] = report_mod.write_report(root, VERSION, focus=stage)["path"]
    except OSError as exc:
        # 页面刷新失败不能静默：用户正看着旧状态，得让他知道
        print(f"  [警告] 看板刷新失败：{exc}")
        print("  页面可能还停在旧状态，手动跑一次 report --root . 重试")
    return out


def stage_view(root: Path, stage: str) -> Path:
    """单阶段视图的路径（还没生成也给出预期路径，方便先告诉用户）。"""
    return root / STATE_DIR / f"看板-{stage}.html"


def load_gates(root: Path) -> dict:
    """读确认记录；旧版本（S1..S6）的键自动后移一位，避免被当成新阶段的记录。"""
    p = gates_path(root)
    if not p.exists():
        return {}
    try:
        gates = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return migrate_gates(root, gates)


def write_gates(root: Path, gates: dict) -> None:
    """写确认记录，顺手打上 schema 标记——没有标记的都被当成 1.10 的旧格式。"""
    gates["_schema"] = GATE_SCHEMA
    write_json(gates_path(root), gates)


def stage_gates(gates: dict) -> dict:
    """只留阶段记录：确认记录.json 里还有 _schema 这类元数据键，不能当阶段读。"""
    return {k: v for k, v in gates.items() if not k.startswith("_")}


def migrate_gates(root: Path, gates: dict) -> dict:
    """1.10 及以前的 确认记录.json 用 S1..S6 表示「影响面/审计/文档/健康/定位/提交」。

    1.11 插入 S0 与 S2 后同一串键含义整体后移，若不迁移，旧的 S2（审计）
    会被当成新的 S2（实现），门禁直接放行。这里按键位移并写回。
    """
    if not gates or gates.get("_schema") == GATE_SCHEMA:
        return gates
    # 旧 S1（影响面与方案）对应新 S1，不用动；旧 S2..S6 整体后移一位
    shifted = {}
    for k, v in gates.items():
        m = re.fullmatch(r"S([2-6])", k)
        shifted[f"S{int(m.group(1)) + 1}" if m else k] = v
    shifted["_schema"] = GATE_SCHEMA
    write_json(gates_path(root), shifted)
    print("[migrate] 阶段编号已升级为 S0–S7，旧的 S1–S6 确认记录整体后移一位")
    return shifted


def gate_sig(files: list[str], doc: Path | None) -> str:
    """指纹只覆盖两样：清单文件集合 + 阶段报告内容。

    不覆盖整个工作区——S3 之后代码本来就在改，覆盖 worktree 会让每次批准都失效。
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
    do_revise = bool(getattr(args, "revise", False))
    if args.show or (not args.approve and not do_skip and not do_revise and not args.files):
        if not gates:
            print("还没有任何阶段被登记或确认。")
            return 0
        md = ["# 阶段确认状态", "",
              "| 阶段 | 内容 | 版本 | 状态 | 时间 | 用户原话 |", "|---|---|---|---|---|---|"]
        names = {"pending": "**待确认**", "approved": "已确认", "skipped": "不适用"}
        for st in GATE_ORDER:
            g = gates.get(st)
            if not g:
                md.append(f"| {st} | {STAGE_TITLE[st]} | — | 未登记 | — | — |")
                continue
            rev = f"v{g.get('revision', 1)}" if g.get("doc") else "—"
            md.append(f"| {st} | {STAGE_TITLE[st]} | {rev} | {names.get(g.get('status'), '?')} | "
                      f"{g.get('at', '')} | {g.get('note', '') or '—'} |")
        md += ["", "> 待确认的阶段，agent 必须停下等用户点头；不许自行推进。", ""]
        (out_dir / "确认状态.md").write_text("\n".join(md), encoding="utf-8")
        print("\n".join(md))
        return 0

    # 三种动作互斥：同时给等于没说清要干什么
    if sum([bool(args.approve), do_skip, do_revise]) > 1:
        print("--approve / --skip / --revise 是三选一的动作，一次只能给一个。")
        return 2

    # ---------------- 用户提了意见：退回待确认，等改完再确认
    if getattr(args, "revise", False):
        g = gates.get(stage)
        if not g:
            print(f"{stage} 还没有登记过，没有可改的方案。")
            print(f"  先登记：gate --stage {stage} --files a.py --doc <报告>")
            return 2
        if not args.note.strip():
            print("--revise 必须带 --note：原样记下用户的意见，不要自己概括。")
            return 2

        prev_rev = int(g.get("revision", 1) or 1)
        next_rev = prev_rev + 1
        g.setdefault("revisions", []).append(
            {"at": now(), "revision": next_rev, "note": args.note.strip(),
             "status_before": g.get("status")})
        g["status"] = "pending"
        g["note"] = ""
        gates[stage] = g
        write_gates(root, gates)

        print(f"{stage}（{STAGE_TITLE[stage]}）已退回待确认，接下来是第 {next_rev} 版。")
        print(f"  用户意见：{args.note.strip()}")
        print("  顺序：按意见改报告 → `gate` 重新登记 → 贴给用户 → 他再确认 → 才能进下一阶段。")
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
        write_gates(root, gates)
        rep = refresh_reports(root, stage)
        nxt = GATE_ORDER[GATE_ORDER.index(stage) + 1] if stage != GATE_ORDER[-1] else None
        print(f"{stage}（{STAGE_TITLE[stage]}）标记为不适用"
              + (f"——可以进入 {nxt}。" if nxt else "——流程结束。"))
        print(f"  用户原话：{args.note.strip()}")
        if rep["full"]:
            print(f"  看板已更新：{rep['full']}")
        return 0

    if args.approve or do_skip:
        g = gates.get(stage)
        if not g:
            print(f"{stage} 还没有登记过，没有可确认的清单。")
            print(f"  先登记：gate --stage {stage} --files a.py,b.py --doc <报告>")
            return 2
        # 已经出了方案就说明这个阶段适用，不能顺手标成"不适用"绕过去
        if do_skip and g.get("doc"):
            print(f"{stage} 已经出过产出（{g['doc']}），不能标成不适用。")
            print("  方案作废请走 `--revise --note \"为什么作废\"` 留下记录，再重新登记或确认。")
            return 2
        if g.get("status") == "approved":
            print(f"{stage} 已经确认过（{g.get('at')}），无需重复确认。")
            return 0
        if not args.note.strip():
            print(f"{'--approve' if args.approve else '--skip'} 必须带 --note：写用户的原话，"
                  f"例如 \"可以，按这个来\" 或 \"这次不涉及文档\"。")
            return 2
        # 「继续」不是确认。会话中断后回来说一句「继续」，如果直接记成批准，
        # 就等于用户没看报告也进了下一阶段——必须先重放报告、问到一句有内容的话。
        if note_is_thin(args.note):
            print(f"{stage}（{STAGE_TITLE[stage]}）没有真正确认：「{args.note.strip()}」"
                  f"只表示往下走，不表示看过这份报告。")
            print(f"  1) 重新打开 {stage_view(root, stage)}（或用 present_files 打开看板）")
            print(f"  2) 复述这份报告的关键结论 3 条，指出要用户拍板的地方")
            print(f"  3) 问一句具体的，例如「按 B 方案、改 3 个文件，可以吗？」")
            print(f"  4) 把用户对这句话的答复原样写进 --note，再确认")
            print(f"  阶段仍然是待确认状态，没有推进。")
            return 4

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
        write_gates(root, gates)
        rep = refresh_reports(root, stage)

        nxt = GATE_ORDER[GATE_ORDER.index(stage) + 1] if stage != GATE_ORDER[-1] else None
        rev = g.get("revision") or 1
        if args.approve:
            print(f"{stage}（{STAGE_TITLE[stage]}）已确认（第 {rev} 版报告）"
                  + (f"——可以进入 {nxt}。" if nxt else "——流程结束。"))
        else:
            print(f"{stage}（{STAGE_TITLE[stage]}）已标记为不适用"
                  + (f"——可以进入 {nxt}。" if nxt else "。"))
        print(f"  用户原话：{args.note.strip()}")
        if rep["full"]:
            print(f"  看板已更新：{rep['full']}")
        if nxt:
            print(f"  下一阶段给用户看的是：{stage_view(root, nxt)}（切过去时自动生成）")
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
    # S0 与 S2 光有小节不够：一个要真问过并拿到答案，一个要真走过红绿
    if stage == "S0":
        missing += check_clarify(text)
    elif stage == "S2":
        missing += check_tdd(text)
    if missing:
        print(f"{stage} 报告不达要求，不予登记：")
        for item in missing:
            print(f"  - {item}")
        print(f"  报告：{rel(root, doc_path)}")
        print(f"  用户要过目的是 {stage} 的完整产出，不是几个空标题。补齐后再登记。")
        return 2

    # ---- 版本与改动：报告每次变，就是新一版；没变就不虚增版本号
    prev = gates.get(stage) or {}
    cur_sha = doc_sha(doc_path)
    same_doc = bool(prev) and prev.get("doc_sha") == cur_sha
    prev_rev = int(prev.get("revision", 0) or 0)

    # 用户提过意见、却拿原封不动的报告重新登记 —— 意见没落实，不许糊弄过去
    last_note = (prev.get("revisions") or [{}])[-1]
    if same_doc and prev.get("status") == "pending" and last_note.get("revision", 0) > prev_rev:
        print(f"{stage} 的报告和上一版一模一样，用户提的意见还没落实到方案里。")
        print(f"  待落实的意见：{last_note.get('note', '')}")
        print("  改完报告再登记；如果意见是「不用改」，就用 --approve 时说明理由。")
        return 2

    rev = prev_rev if same_doc else prev_rev + 1
    if rev == 0:
        rev = 1

    snap_dir = out_dir / "历史版本"
    snap_dir.mkdir(exist_ok=True)
    diff_lines: list[str] = []
    if not same_doc and prev_rev:
        prev_snap = snap_dir / f"{stage}-v{prev_rev}.md"
        if prev_snap.exists():
            old_text = read_text(prev_snap, limit=1_000_000)
            diff_lines = list(difflib.unified_diff(
                old_text.splitlines(), text.splitlines(),
                fromfile=f"v{prev_rev}", tofile=f"v{rev}", lineterm="", n=1))
    (snap_dir / f"{stage}-v{rev}.md").write_text(text, encoding="utf-8")

    signature = gate_sig(files, doc_path)
    revisions = prev.get("revisions", []) if prev else []
    gates[stage] = {
        "at": now(), "status": "pending", "summary": args.summary or "",
        "files": sorted(files), "doc": rel(root, doc_path), "signature": signature,
        "note": "", "revision": rev, "doc_sha": cur_sha, "revisions": revisions,
    }
    write_gates(root, gates)

    md = [f"# {stage} · {STAGE_TITLE[stage]}（第 {rev} 版 · 待用户确认）", "",
          f"- 登记时间：{gates[stage]['at']}",
          f"- 说明：{gates[stage]['summary'] or '（未填）'}",
          f"- 报告：`{gates[stage]['doc']}`",
          f"- 文件数：{len(files)}",
          f"- 指纹：`{signature}`", ""]

    if revisions:
        md += ["## 你提过的意见", "", "| 第几版 | 时间 | 意见原话 |", "|---|---|---|"]
        for r in revisions:
            md.append(f"| v{r.get('revision', '?')} | {r.get('at', '')} | {r.get('note', '')} |")
        md.append("")

    if diff_lines:
        shown = [d for d in diff_lines if d.startswith(("+", "-")) and not d.startswith(("+++", "---"))]
        md += [f"## 本版改了什么（v{prev_rev} → v{rev}，共 {len(shown)} 行变化）", "", "```diff"]
        md += diff_lines[:60]
        if len(diff_lines) > 60:
            md.append(f"... 还有 {len(diff_lines) - 60} 行，见 .devloop/revisions/")
        md += ["```", ""]

    md += ["## 清单覆盖的文件", "", "| 文件 |", "|---|"]
    for f in sorted(files):
        md.append(f"| `{f}` |")
    md += ["", "## 确认流程", "",
           f"1. 把 `{gates[stage]['doc']}` 全文贴给用户（不是只贴文件名）",
           "2. **停下来等**，不许继续做下一阶段",
           f"3. 用户明确同意后：`gate --stage {stage} --approve --note \"用户原话\"`",
           f"4. 用户提了意见：`gate --stage {stage} --revise --note \"意见原话\"`，改完再登记",
           "5. 这个阶段不适用时：`gate --stage " + stage + " --skip --note \"为什么不需要\"`",
           f"6. 然后才能 `state --set {GATE_ORDER[min(GATE_ORDER.index(stage) + 1, 5)]}`", ""]
    (out_dir / f"{stage}-确认单.md").write_text("\n".join(md), encoding="utf-8")

    # 报告里的 mermaid 顺手画成一张独立 SVG：.md 在编辑器里只是一段代码，
    # 用户想要「一张图」的时候可以直接打开它
    svg = diagram_mod.from_markdown(text)
    if svg:
        (out_dir / f"{stage}-架构图.svg").write_text(svg, encoding="utf-8")

    tag = f"第 {rev} 版" + (f"，改动 {len([d for d in diff_lines if d.startswith(('+', '-'))])} 行" if diff_lines else "")
    print(f"{stage} 已登记待确认 -> {out_dir / f'{stage}-确认单.md'}（{len(files)} 个文件，{tag}）")

    # 登记即出页面：整本 + 本阶段单页，贴给用户的东西一定存在
    try:
        rep = report_mod.write_report(root, VERSION)
        view = report_mod.write_report(root, VERSION, focus=stage)
        print(f"  报告页面（第 {rep['seq']} 版）：{rep['path']}")
        print(f"  本阶段页（打开就定位到 {stage}）：{view['path']}")
        print("  用 present_files 打开**本阶段页**——用户看页面，不翻整本、不看 Markdown 源码。")
    except OSError as exc:
        print(f"  报告页面生成失败（{exc}），稍后手动跑 report")
    if revisions:
        print(f"  已记录你提过的 {len(revisions)} 条意见，报告里逐条对应。")
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
    path = out_dir / "文档基线.json"

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
        md += ["## 失效（需要回 S4）", ""]
        md += [f"- `{b['file']}`：{b['why']}" for b in broken] or ["无"]
        md += ["", "## 内容已变（确认是否与本次代码改动有关）", ""]
        md += [f"- `{f}`" for f in changed] or ["无"]
        md += ["", "> 锚点丢失比内容变化严重：说明文档已经不再描述当前的代码符号。", ""]
        (out_dir / "基线复核.md").write_text("\n".join(md), encoding="utf-8")

        print(f"基线检查 -> {out_dir / '基线复核.md'}（失效 {len(broken)} / 内容已变 {len(changed)}）")
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
    md += ["", "下一轮 S3 之后跑 `baseline --check` 复核；锚点丢失就回 S4。", ""]
    (out_dir / "文档基线.md").write_text("\n".join(md), encoding="utf-8")

    print(f"文档基线 -> {path}（{len(docs)} 个文档，{sum(len(d['anchors']) for d in docs)} 个锚点）")
    return 0


# ----------------------------------------------------------------------------- hook

def plan_fingerprint(root: Path, files: list[str], message: str) -> str:
    """与 commit 阶段完全一致的指纹算法，供 hook 复用。"""
    entries = [{"file": f, "status": "?"} for f in files]
    return hashlib.sha1(
        ("|".join(sorted(files)) + "\n" + message + "\n"
         + (git(root, "rev-parse", "HEAD")[1] or "")
         + "\n" + worktree_signature(root, entries)).encode("utf-8")
    ).hexdigest()[:16]


def clean_commit_message(text: str) -> str:
    """去掉 git 自动加的注释行与首尾空白。"""
    lines = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
    return "\n".join(lines).strip()


def cmd_check_commit(args) -> int:
    """commit-msg hook 调用：没走过 `commit` 清单的提交，一律拒绝。"""
    root = Path(args.root).resolve()
    out_dir = ensure_state_dir(root)
    plan_json = out_dir / "S7-提交清单.json"

    if not plan_json.exists():
        print("[dev-loop] 拒绝提交：没有提交清单。")
        print("  先跑：python <dev-loop>/scripts/devloop.py commit --root . --message \"...\"")
        print("  把清单给用户过目，用户同意后再提交。")
        return 1

    try:
        saved = json.loads(plan_json.read_text(encoding="utf-8"))
    except Exception:
        print("[dev-loop] 拒绝提交：S7-提交清单.json 解析失败，重新出一份清单。")
        return 1

    msg = ""
    if getattr(args, "message_file", ""):
        mf = Path(args.message_file)
        if not mf.is_absolute():
            mf = root / mf
        if mf.exists():
            msg = clean_commit_message(read_text(mf))
    if not msg:
        msg = saved.get("message", "")

    expect = plan_fingerprint(root, saved.get("files", []), msg)
    if expect != saved.get("token"):
        print("[dev-loop] 拒绝提交：这次提交和用户看过的清单不一致。")
        if msg != saved.get("message", ""):
            print(f"  清单里的信息：{saved.get('message', '')[:60]}")
            print(f"  你给的信息：  {msg[:60]}")
        else:
            print("  文件集合或内容在出清单之后变过，或者 HEAD 变了。")
        print("  重新出清单 → 给用户过目 → 再提交。")
        return 1

    print("[dev-loop] 提交清单校验通过。")
    return 0


HOOK_SH = """#!/bin/sh
# dev-loop: 没有经过用户确认的提交一律拒绝（由 devloop.py install-hook 生成）
exec "{python}" "{script}" check-commit --root "$(git rev-parse --show-toplevel)" --message-file "$1"
"""


def cmd_install_hook(args) -> int:
    """把 commit-msg hook 装进当前仓库，拦住绕过 tools 的直接提交。"""
    root = Path(args.root).resolve()
    rc, hooks_dir = git(root, "rev-parse", "--git-path", "hooks")
    if rc != 0:
        print("不是 git 仓库，装不了 hook。")
        return 2

    target = Path(hooks_dir)
    if not target.is_absolute():
        target = root / target
    target.mkdir(parents=True, exist_ok=True)
    hook = target / "commit-msg"

    if hook.exists() and "dev-loop" not in read_text(hook, limit=4000):
        print(f"{hook} 已存在且不是本技能生成的，不覆盖。")
        print("  要接管就把原文件挪走再装一次。")
        return 2

    # 用正斜杠：hook 是 sh 脚本，Windows 反斜杠在双引号里不可靠
    hook.write_text(
        HOOK_SH.format(python=Path(sys.executable).as_posix(),
                       script=Path(__file__).resolve().as_posix()),
        encoding="utf-8", newline="\n")
    try:
        hook.chmod(0o755)
    except Exception:
        pass

    # 顺手排除 .devloop/：否则 `git add -A` 会把分析产物一起提交
    rc2, exclude_path = git(root, "rev-parse", "--git-path", "info/exclude")
    if rc2 == 0:
        ex = Path(exclude_path)
        if not ex.is_absolute():
            ex = root / ex
        ex.parent.mkdir(parents=True, exist_ok=True)
        cur = read_text(ex)
        if ".devloop/" not in cur:
            with ex.open("a", encoding="utf-8", newline="\n") as f:
                if cur and not cur.endswith("\n"):
                    f.write("\n")
                f.write("# dev-loop 分析产物，不进版本库\n.devloop/\n")
            print("  已在 .git/info/exclude 排除 .devloop/，产物不会被误提交")

    print(f"已安装 commit-msg hook -> {hook}")
    print("  之后任何 `git commit` 都必须先有匹配的提交清单，否则被拒。")
    print("  注意：`git commit --no-verify` 能跳过它——那是显式违规，SKILL.md 里禁止。")
    return 0


# ----------------------------------------------------------------------------- state

def cmd_check(args) -> int:
    """只校验报告够不够格，不登记。

    改报告时反复用：不用每改一版就走一遍登记，也不会产生「看过但不是这版」的指纹。
    """
    root = Path(args.root).resolve()
    out_dir = ensure_state_dir(root)
    stage = (args.stage or "").upper()
    if stage not in DEFAULT_DOC:
        print(f"未知阶段 {stage}，可选：{'、'.join(GATE_ORDER)}")
        return 2
    doc = Path(args.doc) if args.doc else out_dir / DEFAULT_DOC[stage]
    if not doc.is_absolute():
        doc = root / doc
    if not doc.exists():
        print(f"找不到报告：{doc}")
        return 2

    text = read_text(doc, limit=1_000_000)
    problems = check_doc_sections(stage, text)
    if stage == "S0":
        problems += check_clarify(text)
    elif stage == "S2":
        problems += check_tdd(text)

    if not problems:
        print(f"{stage}（{STAGE_TITLE[stage]}）报告达标，可以 gate 登记。")
        return 0
    print(f"{stage}（{STAGE_TITLE[stage]}）报告还差 {len(problems)} 项：")
    for item in problems:
        print(f"  - {item}")
    print(f"  报告：{rel(root, doc)}")
    return 1


def cmd_state(args) -> int:
    root = Path(args.root).resolve()
    path = ensure_state_dir(root) / "状态.json"
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

    # 阶段名统一归一化：`--set s2` 曾经能绕过整道门（不匹配 S3，直接写进 state）
    if args.set:
        normalized = args.set.strip().upper()
        if normalized not in GATE_ORDER and normalized != "IDLE":
            print(f"未知阶段 {args.set}，可选：{'、'.join(GATE_ORDER)} 或 IDLE")
            return 2
        args.set = normalized

    # --round 是重置，--set 是跳阶段；一起给等于清空记录的同时跳过去
    if args.round and args.set:
        print("--round 和 --set 不能同时给。")
        print("  开新一轮：state --round <需求名>（会回到 S0）")
        print(f"  进入阶段：`state --set {args.set}`（需要上一阶段已确认）")
        return 2

    if args.set in PREV_STAGE:
        # 检查整条链，不只看上一级：S1 没确认就批 S3，进 S4 时也要拦住
        gates_all = load_gates(root)
        for st in GATE_ORDER[:GATE_ORDER.index(args.set)]:
            g = gates_all.get(st)
            if not g:
                print(f"不能进入 {args.set}：{st}（{STAGE_TITLE[st]}）还没登记过。")
                print(f"  1) 先把 {st} 的产出写成报告")
                print(f"  2) 登记：gate --stage {st} --files a.py,b.py --doc <报告>")
                print(f"  3) 把报告贴给用户看，等他明确同意")
                print(f"  4) 用户同意后：gate --stage {st} --approve --note \"用户原话\"")
                print(f"  这个阶段不适用：gate --stage {st} --skip --note \"为什么不需要\"")
                return 3
            if g.get("status") == "pending":
                print(f"不能进入 {args.set}：{st}（{STAGE_TITLE[st]}）的清单还没给用户确认过。")
                print(f"  清单在 {rel(root, root / g['doc'])}，先贴给用户，等他点头。")
                print(f"  用户不认同的处理：改清单 → 重新登记 → 再看一次。")
                return 3
            if g.get("status") not in ("approved", "skipped"):
                print(f"不能进入 {args.set}：{st} 的状态异常（{g.get('status')}）。")
                return 3

            # 用户确认之后报告又被改过——他看过的那版已经不是现在这版，必须重新确认
            if g.get("status") == "approved" and g.get("doc"):
                doc = root / g["doc"]
                cur = gate_sig(g.get("files", []), doc)
                if cur != g.get("signature"):
                    print(f"不能进入 {args.set}：{st}（{STAGE_TITLE[st]}）的报告在你确认之后又被改过。")
                    print(f"  报告：{g['doc']}")
                    print("  你确认的是上一版，现在这份不是同一份。重新登记并贴给用户，再确认一次。")
                    return 3

    if args.round:
        archived = state.get("open_items", [])
        state.setdefault("rounds", []).append(
            {"at": now(), "name": args.round, "items": archived})
        state["rounds"] = state["rounds"][-20:]
        state["open_items"] = []
        state["round"] = args.round
        state["stage"] = "S0"
        state["history"].append({"at": now(), "stage": "S0", "note": f"新一轮：{args.round}"})
        # 上一轮的报告不能留在原地——新一轮登记时会被误当成这一轮的产出
        out_dir = ensure_state_dir(root)
        arch_dir = out_dir / "归档" / f"第{len(state['rounds']):02d}轮"
        moved = 0
        for name in list(DEFAULT_DOC.values()) + [
                "S7-提交清单.json", "验证报告.md", "确认状态.md", "敏感文件预检.md",
                "代码对账.md", "文档对账.md", "依赖图.md", "影响面.md",
                "S1-架构图.svg", "S2-架构图.svg",
                "文档漂移.md", "文档基线.json", "基线复核.md"]:
            f = out_dir / name
            if f.exists():
                arch_dir.mkdir(parents=True, exist_ok=True)
                f.replace(arch_dir / name)
                moved += 1
        for stale in [gates_path(root)] + [out_dir / f"{st}-确认单.md" for st in GATE_ORDER]:
            if stale.exists():
                stale.unlink()
        extra = f"，归档上一轮产物 {moved} 个到 archive/round-{len(state['rounds']):02d}" if moved else ""
        print(f"新一轮「{args.round}」：已归档上一轮清单（{len(archived)} 项）{extra}，阶段重置为 S0（需求澄清）。")

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

    # 阶段一变，看板顶部的「当前阶段」和阶段卡片都得跟着变；
    # 断线重连后用户第一眼看的就是这页，不能停在上一个状态
    if args.set or args.round:
        refresh_reports(root, None)

    if args.show or not (args.set or args.add or args.close is not None
                         or args.note or args.round):
        gates = load_gates(root)
        print(json.dumps({
            "stage": state["stage"],
            "round": state.get("round", ""),
            "note": state.get("note", ""),
            "gates": {k: v.get("status") for k, v in stage_gates(gates).items()},
            "open_items": [i for i in state["open_items"] if not i["done"]],
            "history": state["history"][-6:],
        }, ensure_ascii=False, indent=2))
        return 0
    print(f"stage={state['stage']}  未闭环项 {len([i for i in state['open_items'] if not i['done']])} 个")
    return 0


# ----------------------------------------------------------------------------- blind

def changed_files_now(root: Path, base: str) -> list[str]:
    """当前改动涉及的代码文件：diff + 未跟踪。"""
    out: list[str] = []
    rc, diff = git(root, "diff", "--name-only", base)
    if rc == 0 and diff:
        out += [x.strip() for x in diff.splitlines() if x.strip()]
    out += [e["file"] for e in git_status_entries(root)]
    return list(dict.fromkeys(out))


def cmd_blind(args) -> int:
    root = Path(args.root).resolve()
    out_dir = ensure_state_dir(root)

    files = [x.strip() for x in args.files.split(",") if x.strip()] if args.files else []
    if not files:
        files = [f for f in changed_files_now(root, args.base)
                 if Path(f).suffix.lower() in CODE_EXTS]
    if not files:
        print("没有可检查的代码文件。用 --files 指定，或先产生改动。")
        return 0

    spots = astindex.blind_in_files(root, files)
    by_cat: dict[str, list[dict]] = {}
    for s in spots:
        by_cat.setdefault(s["category"], []).append(s)

    md = ["# 静态分析盲区", "",
          f"- 生成时间：{now()}",
          f"- 检查文件：{len(files)} 个",
          f"- 盲区：{len(spots)} 处" + (f"，涉及 {len(by_cat)} 类" if by_cat else ""), ""]
    if not spots:
        md += ["这些文件里没有发现动态调用、反射、DI、拼装路由等写法。",
               "",
               "> 只代表「没有命中已知模式」。跨服务的调用、配置驱动的行为，静态分析一律看不见。", ""]
    else:
        md += ["> 以下位置**静态分析看不见调用关系**，必须人工确认，别把脚本结果当完整影响面。", ""]
        for cat in sorted(by_cat):
            items = by_cat[cat]
            md += [f"## {cat}（{len(items)} 处）", "",
                   f"为什么看不见：{items[0]['why']}", "",
                   "| 位置 | 代码 | 要确认什么 |", "|---|---|---|"]
            for s in items:
                code = s["code"].replace("|", "\\|")[:110]
                md.append(f"| `{s['file']}:{s['line']}` | `{code}` | {s['action']} |")
            md.append("")

    (out_dir / "静态盲区.md").write_text("\n".join(md), encoding="utf-8")
    write_json(out_dir / "静态盲区.json", {"files": files, "count": len(spots),
                                              "by_category": {k: len(v) for k, v in by_cat.items()},
                                              "spots": spots})

    print(f"盲区 -> {out_dir / '静态盲区.md'}（{len(spots)} 处，{len(by_cat)} 类）")
    for cat in sorted(by_cat):
        print(f"  {cat} {len(by_cat[cat])} 处")
    if spots:
        print("  这些位置脚本看不见，必须人工确认后再写进改造清单。")
    return 0


# ----------------------------------------------------------------------------- quality

def cmd_quality(args) -> int:
    root = Path(args.root).resolve()
    out_dir = ensure_state_dir(root)

    files = [x.strip() for x in args.files.split(",") if x.strip()] if args.files else []
    if not files:
        files = changed_files_now(root, args.base)

    result = quality_mod.run_quality_scan(root, args.base, files)
    if "error" in result:
        print(f"质量扫描失败：{result['error']}")
        return 2

    md = quality_mod.render_markdown(result, args.base, now())
    (out_dir / "实现质量.md").write_text(md, encoding="utf-8")
    write_json(out_dir / "实现质量.json", {**result["stats"], "hits": result["hits"]})

    stats = result["stats"]
    print(f"实现质量 -> {out_dir / '实现质量.md'}"
          f"（P0 {stats['P0']} / P1 {stats['P1']} / P2 {stats['P2']}，"
          f"{stats['files']} 个文件 +{stats['added_lines']} 行）")
    for h in result["hits"][:8]:
        loc = f"{h['file']}:{h['line']}" if h["line"] else h["file"]
        print(f"  {h['severity']} {h['category']:<8} {loc}")
    if len(result["hits"]) > 8:
        print(f"  …… 另有 {len(result['hits']) - 8} 处，见报告")
    if stats["P0"]:
        print("  P0 必须改完才能继续；P1/P2 可在 S3 报告里写明豁免理由。")
    return quality_mod.exit_code(stats)


# ----------------------------------------------------------------------------- report

def cmd_report(args) -> int:
    root = Path(args.root).resolve()
    ensure_state_dir(root)

    if args.history:
        items = report_mod.list_history(root)
        if not items:
            print("还没有历史报告。跑一次 report 就会留下快照。")
            return 0
        print(f"最近 {len(items)} 份历史报告（目录 .devloop/{report_mod.HISTORY_DIR}/）：")
        for it in items:
            print(f"  {it['time']}  {it['size'] / 1024:6.1f} KB  {it['name']}")
        print("\n  用 present_files 打开任意一份即可对比当时的内容。")
        return 0

    focus = (args.focus or "").upper() or None
    if focus and focus not in GATE_ORDER:
        print(f"未知阶段 {focus}，可选：{'、'.join(GATE_ORDER)}")
        return 2
    res = report_mod.write_report(root, VERSION, args.name, focus=focus)
    if focus:
        print(f"{focus} 单阶段视图 -> {res['path']}（{res['count']} 份报告）")
        print("  这一步就是「定位到相关部分」：打开即是该阶段正文，不用翻整本。")
        return 0
    print(f"报告 -> {res['path']}（{res['count']} 份报告，第 {res['seq']} 版）")
    print(f"  历史 -> {res['snapshot']}（保留最近 {report_mod.KEEP_HISTORY} 份"
          + (f"，已清理 {res['pruned']} 份旧版" if res["pruned"] else "") + "）")
    print("  下一步：用 present_files 把这个 html 打开给用户看——")
    print("  用户要在页面里看阶段状态和报告，不是看一堆 Markdown 源码。")
    return 0


# ----------------------------------------------------------------------------- stage

def _section_of(text: str, *keys: str) -> str:
    """取某个标题下的正文（到下一个同级或更高级标题为止）。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s*(.+)$", line.strip())
        if not m or not any(k in m.group(2) for k in keys):
            continue
        level = len(m.group(1))
        body = []
        for nxt in lines[i + 1:]:
            m2 = re.match(r"^(#{1,6})\s", nxt.strip())
            if m2 and len(m2.group(1)) <= level:
                break
            body.append(nxt)
        return "\n".join(body).strip()
    return ""


def _demote_md(text: str, levels: int = 1) -> str:
    """把内联进来的 Markdown 标题整体降级，避免拼完后层级被压平。"""
    out, fence = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fence = not fence
        if not fence and re.match(r"^#{1,5}\s", line):
            line = "#" * levels + line
        out.append(line)
    return "\n".join(out)


def _compose_s2(root: Path, out_dir: Path, base: str) -> tuple[str, list[str]]:
    """把 audit / quality / blind 三份结果拼成 S3 的审计报告。"""
    audit_md = read_text(out_dir / "代码对账.md")
    quality_md = read_text(out_dir / "实现质量.md")
    blind_md = read_text(out_dir / "静态盲区.md")

    parts = ["# S3 变更审计", "", f"- 生成时间：{now()}", f"- 对比基线：{base}", ""]
    for title, key in (("漏改", "漏改"), ("越界", "越界"),
                       ("动作不符", "动作不符"), ("一致", "一致")):
        body = _section_of(audit_md, key) or "无"
        parts += [f"## {title}", "", _demote_md(body), ""]

    q_body = _section_of(quality_md, "实现质量") or quality_md.strip()
    parts += ["## 实现质量", "", _demote_md(q_body) or "无", ""]

    b_body = _section_of(blind_md, "静态分析盲区")
    if not b_body:
        b_body = "\n".join(l for l in blind_md.splitlines() if not l.startswith("# ")).strip()
    parts += ["## 静态盲区", "", _demote_md(b_body) or "无", ""]

    return "\n".join(parts), []


def _register_gate(root: Path, stage: str, files: list[str], doc: Path,
                   summary: str) -> int:
    """复用 gate 的登记逻辑，避免两套实现。"""
    ns = argparse.Namespace(root=str(root), stage=stage, files=",".join(files),
                            doc=str(doc), summary=summary, approve=False, skip=False,
                            note="", show=False, revise=False)
    return cmd_gate(ns)


CLARIFY_BANK = [
    ("目标范围", "这次要解决的具体问题是什么？（谁、什么场景、现在怎么难受）"),
    ("目标范围", "涉及哪些入口（接口/页面/定时任务/消息）？有没有明确不做的范围？"),
    ("边界异常", "空值、默认值、超大值、重复提交分别怎么处理？"),
    ("边界异常", "失败/超时/重试要不要幂等？事务边界卡在哪一层？"),
    ("验收标准", "怎么判断做完了？逐条给可测的标准（带数值或可观察结果）。"),
    ("验收标准", "谁来验、在哪个环境验、用什么数据验？"),
    ("约束兼容", "存量数据和老接口要不要兼容？能否加表、加字段、改签名？"),
    ("约束兼容", "性能、权限、审计日志有硬要求吗？能不能引入新依赖？"),
]


def state_round(root: Path) -> str:
    p = ensure_state_dir(root) / "状态.json"
    if not p.exists():
        return ""
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("round", "") or ""
    except Exception:
        return ""


def _extract_acs(text: str) -> list[str]:
    """把 S0 报告里「验收标准」一节的条目抽出来，给 S2 的映射表打底。"""
    body = section_body(text, ("验收标准", "验收"))
    out: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s or TABLE_SEP_RE.match(s):
            continue
        m = re.match(r"^(?:[-*+]|\d+[.)])\s+(.+)$", s)
        if m:
            out.append(re.sub(r"^AC\d+\s*[:：]?\s*", "", m.group(1)).strip())
            continue
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if any(re.match(r"(?i)^ac\d+", c) for c in cells[:2]):
                out.append(next((c for c in cells if c), s))
    return out


def profile_languages(prof: dict) -> str:
    """把画像里的语言读成一行人话。

    scan 写的是 ``[{"lang": "Python", "files": 3}]``；早先手写过的画像可能是
    ``{"Python": 3}``。两种都认，读不出来就直说未扫描——不能因为画像形状不对就崩。
    """
    raw = prof.get("languages")
    if isinstance(raw, list):
        parts = [f"{x.get('lang', '?')}({x.get('files', 0)})"
                 for x in raw if isinstance(x, dict) and x.get("lang")]
        return "、".join(parts) or "未扫描"
    if isinstance(raw, dict):
        parts = [f"{k}({v})" for k, v in raw.items() if k]
        return "、".join(parts) or "未扫描"
    return "未扫描"


def profile_test_cmd(prof: dict) -> str:
    """画像里的测试命令在 commands.test（verify 也从这里读），没有就提示自己指定。"""
    cmds = prof.get("commands")
    if isinstance(cmds, dict):
        cmd = cmds.get("test") or ""
        if isinstance(cmd, str) and cmd.strip():
            return cmd.strip()
    return "（verify 时指定 --cmd）"


def _clarify_draft(round_name: str, langs: str, test_cmd: str) -> str:
    rows = "\n".join(f"| {c} | {q} |  |  |" for c, q in CLARIFY_BANK)
    return (
        "# S0 需求澄清\n\n"
        f"- 生成时间：{now()}\n"
        f"- 轮次：{round_name or '（未命名，建议先 state --round \"<需求名>\"）'}\n"
        f"- 技术栈：{langs}\n"
        f"- 测试命令：{test_cmd}\n\n"
        "> 下面是要填的骨架。**问答表必须填用户的原话/明确答复**，不许自己编。\n"
        "> 用弹窗追问：每轮 3–4 个问题，一次问一类，问完把答案写回表格再问下一轮。\n\n"
        "## 需求原话\n\n"
        "> 原样粘贴用户这一轮的需求描述，不要改写、不要总结。\n\n"
        "## 问答表\n\n"
        "| 类别 | 问题 | 用户答案 | 对方案的影响 |\n|---|---|---|---|\n"
        f"{rows}\n\n"
        "## 验收标准\n\n"
        "> 逐条写成可测的句子（含数值或可观察结果），S2 会把每条映射到一个测试。\n"
        "> 一条都没有就是没问清——`gate` 会拒收。\n\n"
        "## 待定与假设\n\n"
        "> 还没定的、我先替你假设的，逐条列出来。没有就写「无」。\n\n"
        "## 弹窗记录\n\n"
        "| 轮次 | 时间 | 问了什么 | 用户怎么回 |\n|---|---|---|---|\n"
        "| 1 |  |  |  |\n"
    )


def _tdd_draft(acs: list[str], base: str, files: list[str]) -> str:
    if acs:
        rows = "\n".join(f"| AC{i} | {a} | （待填：tests/…::用例名） | （待填：先跑一次，把失败输出贴这里） | （待填） |"
                         for i, a in enumerate(acs, 1))
    else:
        rows = "| AC1 | （S0 里没有验收标准，回去补） |  |  |  |"
    file_list = "\n".join(f"- `{f}`" for f in files) or "- （还没有改动，实现后这里列出来）"
    return (
        "# S2 实现（TDD）\n\n"
        f"- 生成时间：{now()}\n- 对比基线：{base}\n\n"
        "## 验收标准与测试映射\n\n"
        "> 一张表管两件事：每条验收标准落到哪个测试、红绿两次的输出各是什么。\n"
        "> 红色证据写「先写测试、还没实现时」的输出；绿色证据写「实现完再跑」的输出。\n\n"
        "| # | 验收标准 | 测试（文件::用例） | 先红（失败输出摘要） | 后绿（通过输出摘要） |\n"
        "|---|---|---|---|---|\n"
        f"{rows}\n\n"
        "## 改动文件\n\n" + file_list + "\n\n"
        "## 测试结果\n\n"
        "> 全量命令与结果（不是只跑新加的那几个）。\n\n"
        "## 回访确认\n\n"
        "> 实现完逐条问用户「这条满足了吗」，把他的原话记这里；不满足的打回重做。\n\n"
        "## 豁免\n\n"
        "> 纯文档/配置/样式改动可以申请豁免，写清理由；S3 审计会复核这条豁免。\n"
    )


def cmd_stage(args) -> int:
    """一条命令跑完某阶段的机械部分：分析 → 拼报告 → 登记确认门。"""
    root = Path(args.root).resolve()
    out_dir = ensure_state_dir(root)
    stage = args.stage.upper()
    base = args.base

    if args.files:
        files = [x.strip() for x in args.files.split(",") if x.strip()]
    else:
        files = changed_files_now(root, base)

    print(f"[{stage}] 开始：{len(files)} 个文件，基线 {base}")

    if stage == "S0":
        doc = out_dir / "S0-需求澄清.md"
        if doc.exists():
            print(f"{doc} 已存在，不覆盖。要重开一轮请先 state --round \"<需求名>\"。")
            return 0
        prof = load_profile(root)
        langs = profile_languages(prof)
        test_cmd = profile_test_cmd(prof)
        doc.write_text(_clarify_draft(state_round(root), langs, test_cmd), encoding="utf-8")
        report_mod.write_report(root, VERSION)
        print(f"\n初稿 -> {doc}")
        print("下一步：用弹窗把四类问题问完（每轮 3–4 个），答案填进「问答表」")
        print("  四类：目标范围 / 边界异常 / 验收标准 / 约束兼容")
        print("  gate 会拒收：答案列为空、四类覆盖不到三类、验收标准少于 1 条")
        return 0

    if stage == "S2":
        acs = _extract_acs(read_text(out_dir / DEFAULT_DOC["S0"]))
        doc = out_dir / "S2-实现.md"
        doc.write_text(_tdd_draft(acs, base, files), encoding="utf-8")
        report_mod.write_report(root, VERSION)
        print(f"\n初稿 -> {doc}")
        print(f"从 S0 抄来 {len(acs)} 条验收标准；每条先写失败测试、留红色输出，再实现")
        print("  豁免：纯文档/配置改动可 gate --stage S2 --skip --note \"无行为变更\"")
        return 0

    if stage == "S3":
        # 对账
        rc_audit = cmd_audit(argparse.Namespace(root=str(root), base=base, scope="code"))
        # 实现质量
        cmd_quality(argparse.Namespace(root=str(root), base=base, files=",".join(files)))
        # 盲区
        cmd_blind(argparse.Namespace(root=str(root), base=base, files=",".join(files)))

        composed, _ = _compose_s2(root, out_dir, base)
        doc = out_dir / "S3-变更审计.md"
        doc.write_text(composed, encoding="utf-8")
        print(f"审计报告 -> {doc}")

        code_files = [f for f in files if Path(f).suffix.lower() in CODE_EXTS]
        rc = _register_gate(root, "S3", code_files or files, doc,
                            args.summary or f"S3 {len(files)} 个文件")
        # gate 登记时已经生成过页面，这里不再重复
        print(f"\n下一步：present_files 打开 {out_dir / '看板.html'} 给用户看，"
              f"等他确认后 gate --stage S3 --approve --note \"<原话>\"")
        return 0 if rc == 0 else rc
    if stage == "S1":
        # 机器能做的部分：影响面、依赖图、盲区、敏感预检
        cmd_impact(argparse.Namespace(root=str(root), base=base, files=",".join(files),
                                      staged=False))
        rc_graph = cmd_graph(argparse.Namespace(root=str(root), files=",".join(files),
                                                depth=2, max_nodes=40, format="both"))
        if rc_graph != 0:
            print("  依赖图没生成（清单里可能没有仓库内文件），S1 方案里的分层要手工补")
        cmd_blind(argparse.Namespace(root=str(root), base=base, files=",".join(files)))
        cmd_sensitive(argparse.Namespace(root=str(root), files=",".join(files)))

        manifest = read_text(out_dir / "影响面.md")
        graph_md = read_text(out_dir / "依赖图.md")
        blind_md = read_text(out_dir / "静态盲区.md")
        draft = out_dir / "S1-素材.md"
        draft.write_text(
            "# S1 素材（脚本产出，待补方案）\n\n"
            f"- 生成时间：{now()}\n- 锚点文件：{len(files)} 个\n\n"
            "> 下面只是**素材**。技术路线、实现流程、架构图、改造清单必须由你写，"
            "`gate` 会拒绝只有素材的报告。\n\n"
            "## 影响面\n\n" + (manifest or "（空）") + "\n\n"
            "## 依赖图\n\n" + (graph_md or "（空）") + "\n\n"
            "## 静态盲区\n\n" + (blind_md or "（空）") + "\n",
            encoding="utf-8")
        report_mod.write_report(root, VERSION)
        print(f"\n素材 -> {draft}")
        print("还需要你写：技术路线（≥2 条）、实现流程、架构图（mermaid）、改造清单（含理由）")
        print(f"写完存成 {out_dir / 'S1-影响面与方案.md'}，再 gate --stage S1 --files ... --doc ...")
        return 0

    if stage == "S4":
        cmd_drift(argparse.Namespace(root=str(root), docs="", changed=",".join(files)))
        cmd_audit(argparse.Namespace(root=str(root), base=base, scope="docs"))
        drift_md = read_text(out_dir / "文档漂移.md")
        doc = out_dir / "S4-文档同步.md"
        doc.write_text(
            "# S4 文档同步（脚本初稿）\n\n"
            f"- 生成时间：{now()}\n\n"
            "## 文档现状\n\n" + (drift_md or "（无漂移检测结果）") + "\n\n"
            "## 逐文档判定\n\n"
            "| 文档 | 新建/更新/不改 | 依据 |\n|---|---|---|\n"
            "| （待填） | | |\n",
            encoding="utf-8")
        report_mod.write_report(root, VERSION)
        print(f"\n初稿 -> {doc}")
        print("把「逐文档判定」表填完（每行必须给依据），再 gate --stage S4 ...")
        return 0

    if stage == "S5":
        # 健康检查的机器部分：客观指标 + 本次改动质量 + 盲区
        cmd_quality(argparse.Namespace(root=str(root), base=base, files=",".join(files)))
        cmd_blind(argparse.Namespace(root=str(root), base=base, files=",".join(files)))
        metrics_out = ""
        mpath = _HERE / "metrics.py"
        if mpath.exists():
            mproc = subprocess.run([sys.executable, str(mpath), "--root", str(root)],
                                   capture_output=True, encoding="utf-8",
                                   errors="replace", cwd=str(root))
            metrics_out = (mproc.stdout or "").strip()
        doc = out_dir / "S5-健康检查.md"
        doc.write_text(
            "# S5 健康检查（脚本初稿）\n\n"
            f"- 生成时间：{now()}\n- 范围：{'本次改动 ' + str(len(files)) + ' 个文件' if files else '全仓'}\n\n"
            "## 健康指标\n\n" + (_demote_md(metrics_out) or "（metrics.py 没跑起来）") + "\n\n"
            "## 问题\n\n"
            "| 等级 | 文件:行 | 问题 | 影响 | 修复建议 |\n|---|---|---|---|---|\n"
            "| （待填） | | | | |\n\n"
            "## P0\n\n（把会导致故障或数据错误的挑出来）\n\n"
            "## P1\n\n（其余值得修的）\n",
            encoding="utf-8")
        rep = report_mod.write_report(root, VERSION)
        print(f"\n初稿 -> {doc}")
        print("把「问题」表按 P0/P1/P2 填完（每条要带文件:行与修复建议），再 gate --stage S5 ...")
        print(f"报告页面：{rep['path']}")
        return 0

    if stage == "S6":
        trace = ""
        if getattr(args, "log", ""):
            spath = _HERE / "stackmap.py"
            if spath.exists():
                log_path = Path(args.log)
                if not log_path.is_absolute():
                    log_path = root / log_path
                subprocess.run([sys.executable, str(spath), "--root", str(root),
                                "--log", str(log_path)], capture_output=True,
                               encoding="utf-8", errors="replace", cwd=str(root))
                trace = read_text(out_dir / "调用链映射.md").strip()
        doc = out_dir / "S6-缺陷定位.md"
        doc.write_text(
            "# S6 缺陷定位与修复（脚本初稿）\n\n"
            f"- 生成时间：{now()}\n\n"
            "## 现象\n\n（报错原文、堆栈、时间、环境、触发操作、频率）\n\n"
            "## 栈帧映射\n\n" + (_demote_md(trace) or "（用 --log err.log 让脚本自动映射到文件:行）") + "\n\n"
            "## 根因\n\n（数据 / 逻辑 / 并发 / 环境 / 依赖，附证据链）\n\n"
            "## 修复\n\n（最小改动 + 影响半径 + 回归项）\n",
            encoding="utf-8")
        rep = report_mod.write_report(root, VERSION)
        print(f"\n初稿 -> {doc}")
        print("补完现象、根因、证据链、最小修复，再 gate --stage S6 ...")
        print(f"报告页面：{rep['path']}")
        return 0

    print(f"{stage} 没有可自动化的分析；按 SKILL.md 手工做，完成后 gate --stage {stage} ...")
    return 0


# ----------------------------------------------------------------------------- next

def cmd_next(args) -> int:
    """读状态，直接说下一步做什么，省得每次翻文档。"""
    root = Path(args.root).resolve()
    out_dir = ensure_state_dir(root)
    state = json.loads(read_text(out_dir / "状态.json") or "{}")
    gates = load_gates(root)
    seq = list(GATE_ORDER)

    stage = state.get("stage", "S0")
    if stage not in seq:
        stage = "S0"

    print(f"当前阶段：{stage}  轮次：{state.get('round') or '—'}")
    for st in seq:
        g = gates.get(st) or {}
        mark = {"approved": "✓", "pending": "…", "skipped": "-"}.get(g.get("status"), " ")
        print(f"  {mark} {st} {STAGE_TITLE.get(st, '')}")

    # 找第一个没被确认的阶段
    target = next((st for st in seq if (gates.get(st) or {}).get("status")
                   not in ("approved", "skipped")), None)
    if target is None:
        print("\n所有阶段都已确认。下一步：生成提交清单")
        print("  python scripts/devloop.py commit --root . --message \"...\" --bump patch")
        return 0

    g = gates.get(target) or {}
    nxt = seq[seq.index(target) + 1] if target != seq[-1] else None
    print()
    if not g:
        print(f"下一步：产出 {target}（{STAGE_TITLE[target]}）")
        if target == "S0":
            print("  python scripts/devloop.py stage --root . --stage S0")
            print("  然后用弹窗把四类问题问完，答案填进「问答表」，再 gate --stage S0")
        elif target == "S2":
            print("  python scripts/devloop.py stage --root . --stage S2 --base HEAD")
            print("  先写失败测试并留红色输出，再实现到绿；实现完逐条回访确认")
        elif target == "S3":
            print("  python scripts/devloop.py stage --root . --stage S3 --base HEAD")
        elif target == "S1":
            print("  python scripts/devloop.py stage --root . --stage S1 --files <锚点文件>")
        else:
            print(f"  按 SKILL.md 的 {target} 规程做完，写成 "
                  f".devloop/{report_mod.STAGE_ANCHOR.get(target, target + '.md')}")
    elif g.get("status") == "pending":
        view = stage_view(root, target)
        print(f"下一步：{target}（{STAGE_TITLE[target]}）在等用户确认——**不要往下走**。")
        print(f"  重新打开：{view}")
        print(f"    用 present_files 打开这一页（单阶段视图，直接定位到 {target}）")
        print(f"    再复述 3 条关键结论，问一句具体的话让他拍板")
        print(f"  同意后：gate --stage {target} --approve --note \"<用户对这句话的答复>\"")
        if nxt:
            print(f"  然后：state --set {nxt}")
        print("  注意：「继续」「好的」「嗯」不算确认，gate 会拒收（返回 4），"
              "阶段不会推进。")
        if target == "S1":
            print("  断线重连后尤其如此：先重放报告，再谈下一步。")
    else:
        print(f"{target} 状态异常（{g.get('status')}），重新登记一次。")
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
    g.add_argument("--files", default="", help="改造清单文件，逗号分隔；省略则读 影响面.json")
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
                    help="对账范围：code 只对代码项（S3），docs 只对文档项（S4），默认 code")
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
    g.add_argument("--stage", default="S1", help="阶段 S0-S7，默认 S1")
    g.add_argument("--files", default="", help="本阶段清单覆盖的文件，逗号分隔")
    g.add_argument("--doc", default="", help="本阶段报告文件，默认 .devloop/<阶段>-确认单.md")
    g.add_argument("--summary", default="", help="一句话说明")
    g.add_argument("--approve", action="store_true", help="用户确认后标记为已确认")
    g.add_argument("--revise", action="store_true",
                   help="用户提了意见：退回待确认，记下意见，改完重新登记")
    g.add_argument("--skip", action="store_true", help="本阶段不适用（也要给原话）")
    g.add_argument("--note", default="", help="用户原话，确认或跳过时必填")
    g.add_argument("--show", action="store_true", help="查看各阶段确认状态")
    g.set_defaults(func=cmd_gate)

    ckd = sub.add_parser("check", help="只校验阶段报告是否达标，不登记（改稿时反复用）")
    ckd.add_argument("--root", default=".")
    ckd.add_argument("--stage", required=True, help="阶段 S0-S7")
    ckd.add_argument("--doc", default="", help="报告路径，默认 .devloop/<阶段默认报告>")
    ckd.set_defaults(func=cmd_check)

    ck = sub.add_parser("check-commit", help="供 commit-msg hook 调用：校验提交清单")
    ck.add_argument("--root", default=".")
    ck.add_argument("--message-file", default="", help="commit-msg hook 传入的消息文件")
    ck.set_defaults(func=cmd_check_commit)

    ih = sub.add_parser("install-hook", help="安装 commit-msg hook，拦住绕过工具的提交")
    ih.add_argument("--root", default=".")
    ih.set_defaults(func=cmd_install_hook)

    pl = sub.add_parser("plan", help="兼容别名：等同 gate --stage S1")
    pl.add_argument("--root", default=".")
    pl.add_argument("--files", default="")
    pl.add_argument("--summary", default="")
    pl.add_argument("--approve", action="store_true")
    pl.add_argument("--note", default="")
    pl.set_defaults(func=cmd_gate, stage="S1")

    bl = sub.add_parser("blind", help="静态分析盲区：反射/DI/动态路由")
    bl.add_argument("--root", default=".")
    bl.add_argument("--files", default="", help="要检查的文件，逗号分隔；省略则取当前改动")
    bl.add_argument("--base", default="HEAD", help="对比基线，默认 HEAD")
    bl.set_defaults(func=cmd_blind)

    q = sub.add_parser("quality", help="实现质量：只看本次新增行的硬伤")
    q.add_argument("--root", default=".")
    q.add_argument("--base", default="HEAD", help="对比基线，默认 HEAD")
    q.add_argument("--files", default="", help="限定文件，逗号分隔")
    q.set_defaults(func=cmd_quality)

    rp = sub.add_parser("report", help="把各阶段报告渲染成单文件 HTML")
    rp.add_argument("--root", default=".")
    rp.add_argument("--name", default="",
                    help="输出文件名；默认 看板.html，--focus 时默认 看板-<阶段>.html")
    rp.add_argument("--history", action="store_true", help="列出历史报告，不重新生成")
    rp.add_argument("--focus", default="", help="只渲染某个阶段（S0-S7），生成单阶段视图")
    rp.set_defaults(func=cmd_report)

    sg = sub.add_parser("stage", help="一条命令跑完某阶段的分析、报告与登记")
    sg.add_argument("--root", default=".")
    sg.add_argument("--stage", default="S3", help="S0-S7，默认 S3")
    sg.add_argument("--base", default="HEAD", help="对比基线，默认 HEAD")
    sg.add_argument("--files", default="", help="限定文件，逗号分隔")
    sg.add_argument("--summary", default="", help="一句话说明")
    sg.add_argument("--log", default="", help="S6 用：报错日志文件，自动映射到文件:行")
    sg.set_defaults(func=cmd_stage)

    nx = sub.add_parser("next", help="看下一步该做什么")
    nx.add_argument("--root", default=".")
    nx.set_defaults(func=cmd_next)

    st = sub.add_parser("state", help="阶段状态")
    st.add_argument("--root", default=".")
    st.add_argument("--set", default="", help="设置当前阶段，如 S1/S3/S4/S5/S6")
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
