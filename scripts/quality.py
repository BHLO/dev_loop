"""实现质量扫描：只看本次改动新增的行。

补的是 S1 之后的空档——方案定了「改哪几个文件」，但没人管「代码写成什么样」。
这里不评判设计，只抓那些**一眼能判定的实现缺陷**：调试残留、吞异常、硬编码密钥、
字符串拼 SQL、eval、注释掉的代码、以及改了代码却没补测试。

只扫 diff 的新增行，不翻存量代码——存量问题归 S4 健康检查，两者不要混。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

LANG_BY_EXT = {
    ".py": "py", ".pyi": "py", ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js",
    ".ts": "js", ".tsx": "js", ".vue": "js", ".svelte": "js", ".java": "jvm",
    ".kt": "jvm", ".kts": "jvm", ".scala": "jvm", ".groovy": "jvm", ".go": "go",
    ".rs": "rs", ".cs": "cs", ".php": "php", ".rb": "rb", ".swift": "other",
    ".c": "other", ".h": "other", ".cpp": "other", ".cc": "other", ".hpp": "other",
}

DOC_EXTS = {".md", ".markdown", ".rst", ".adoc", ".txt"}
TEST_HINTS = ("test", "tests", "__tests__", "spec", "specs", "e2e")

# (id, 级别, 类别, 适用语言集合(None=全部), 正则, 为什么是问题, 怎么改)
RULES: list[tuple] = [
    ("SEC-01", "P0", "硬编码密钥", None,
     r"(?:AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|"
     r"BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY|xox[baprs]-[A-Za-z0-9-]{10,})",
     "密钥进仓库就等于泄露，改历史也难彻底清掉", "改读环境变量或密钥管理服务，并轮换该密钥"),

    ("SEC-02", "P0", "硬编码口令", {"py", "js", "jvm", "go", "php", "rb", "cs"},
     r"\b(?:password|passwd|pwd|secret|token|api_key|apikey)\s*[:=]\s*[\"'][^\"'{}$]{6,}[\"']",
     "口令写在代码里，权限边界直接失效", "改为配置项注入，默认值不要填真实口令"),

    ("SEC-03", "P0", "SQL 拼接", {"py", "js", "jvm", "go", "php", "rb", "cs"},
     r"(?:(?:execute|executemany|query|raw)\s*\(\s*(?:f[\"']|[\"'][^\"']*[\"']\s*\+))|"
     r"(?:[\"'][^\"']*\b(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER)\b[^\"']*[\"']\s*\+)|"
     r"(?:f[\"'][^\"']*\b(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER)\b[^\"']*\{)",
     "拼接的 SQL 是注入入口，参数化才是唯一正解", "改用占位符：execute(sql, params)"),

    ("SEC-04", "P0", "动态执行", {"py", "js"},
     r"\b(?:eval|exec)\s*\(|new Function\s*\(",
     "字符串当代码跑，输入一旦可控就是 RCE", "改用显式分支或查表，不要拼接执行"),

    ("SEC-05", "P0", "关掉校验", None,
     r"--no-verify|verify\s*=\s*False|InsecureSkipVerify|rejectUnauthorized\s*:\s*false|"
     r"ssl\._create_unverified_context|chmod\s+777",
     "关掉的是安全边界，不是麻烦", "恢复校验；确需例外要在代码里写明原因与范围"),

    ("ERR-01", "P0", "吞异常", {"py"},
     r"except(?:\s+[\w.,() ]+)?\s*:\s*pass\s*$",
     "异常被吃掉，出错时无人知晓，排查全靠猜", "至少记日志并带上上下文；能处理就处理"),

    ("ERR-02", "P1", "空 catch", {"js", "jvm", "cs"},
     r"catch\s*(?:\([^)]*\))?\s*\{\s*\}",
     "空 catch 让失败静默通过", "补日志或向上抛，别让调用方以为成功"),

    ("DBG-01", "P1", "调试残留", {"py", "js", "go", "jvm", "php", "rb", "cs"},
     r"(?:^|[^.\w])print\s*\(|console\.(?:log|debug)\s*\(|debugger;|"
     r"fmt\.Print(?:ln|f)?\s*\(|System\.out\.print|var_dump\s*\(|"
     r"(?<![\w.])dd\s*\(",
     "调试输出会进生产日志，也可能带出敏感数据", "改用项目日志组件，或提交前删掉"),

    ("DBG-02", "P1", "新增 TODO", {"py", "js", "jvm", "go", "php", "rb", "cs"},
     r"(?:TODO|FIXME|XXX|HACK)\b",
     "TODO 进主干等于把问题留给下一个人", "能做完就做完；确实要留，写清 issue 号"),

    ("DBG-03", "P1", "硬编码地址", {"py", "js", "jvm", "go", "php", "rb", "cs"},
     r"[\"'](?:https?://)?(?:localhost|127\.0\.0\.1)(?::\d+)?",
     "本地地址进代码，换环境就连不上", "改为配置项，默认值在示例配置里给"),

    ("DBG-04", "P2", "超长行", None,
     r"^.{161,}$",
     "超长行多半是塞了太多逻辑，可读性差", "拆成多行或抽变量"),

    ("DBG-05", "P2", "类型忽略", {"py", "js"},
     r"#\s*type:\s*ignore|@ts-(?:ignore|nocheck)|eslint-disable",
     "忽略检查会掩盖真实类型错误", "修类型；确需忽略要写明原因"),
]

COMMENT_CODE_RE = re.compile(r"^\s*(?:#|//)\s*(?:def |class |function |const |let |var |if |for |"
                             r"while |return |import |from |public |private )")
SLEEP_RE = re.compile(r"\btime\.sleep\s*\(|setTimeout\s*\(|\bThread\.sleep\s*\(")

MAX_ADDED_LINES_PER_FILE = 400
MAX_FUNC_LINES = 80


def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                           encoding="utf-8", errors="replace", timeout=90)
        return p.returncode, (p.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        return 1, f"<git failed: {exc}>"


def parse_added_lines(diff_text: str) -> dict[str, list[tuple[int, str]]]:
    """从统一 diff 里取出每个文件新增的行，行号是新文件里的行号。"""
    out: dict[str, list[tuple[int, str]]] = {}
    cur, lineno = None, 0
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:].strip()
            out.setdefault(cur, [])
            continue
        if line.startswith("+++ /dev/null"):
            cur = None
            continue
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            lineno = int(m.group(1))
            continue
        if cur is None:
            continue
        if line.startswith("+"):
            out[cur].append((lineno, line[1:]))
            lineno += 1
        elif line.startswith("-"):
            continue
        elif line.startswith(" "):
            lineno += 1
    return {k: v for k, v in out.items() if v}


SELF_RULE_FILE = Path(__file__).name   # 规则表里就写着 --no-verify 之类的字样，
                                       # 扫自己必然自指命中，直接跳过本文件

CLI_DIRS = ("scripts/", "bin/", "cmd/", "tools/")


def _safe_read(path: Path, limit: int = 600_000) -> str:
    """读不到就当空：调用方只关心能否判定，读失败不该中断扫描。"""
    if not path.is_file():
        return ""
    if path.stat().st_size > limit:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def is_cli_entry(root: Path, path: str, added: dict[str, list[tuple[int, str]]]) -> bool:
    """命令行工具里 print 就是输出方式，按 P1 报会全是噪音。

    判定同时看路径与 `__main__` 保护，避免把业务代码的 print 放过。
    """
    low = "/" + path.replace("\\", "/").lower()
    text = "\n".join(x[1] for x in added.get(path, [])) + "\n" + _safe_read(root / path)
    if "__main__" not in text:
        return False
    in_cli_dir = any(d in low for d in CLI_DIRS)
    has_guard = "__name__ == \"__main__\"" in text or "__name__ == '__main__'" in text
    return in_cli_dir or has_guard


def _lang(path: str) -> str | None:
    return LANG_BY_EXT.get(Path(path).suffix.lower())


def is_test_file(path: str) -> bool:
    low = path.lower()
    if any(f"/{h}/" in f"/{low}/" or low.startswith(f"{h}/") for h in TEST_HINTS):
        return True
    name = Path(low).name
    return ("test_" in name or "_test." in name or name.startswith("test.")
            or ".spec." in name or ".test." in name)


def scan_rule_hits(files: dict[str, list[tuple[int, str]]],
                   root: Path | None = None) -> list[dict]:
    hits = []
    for path, lines in files.items():
        lang = _lang(path)
        if lang is None:
            continue
        if Path(path).name == SELF_RULE_FILE:
            continue
        cli = is_cli_entry(root or Path("."), path, files) if root else False
        for lineno, text in lines:
            for rid, sev, cat, langs, pat, why, fix in RULES:
                if langs is not None and lang not in langs:
                    continue
                if not re.search(pat, text):
                    continue
                # 命令行工具靠 print 输出，降为 P2 并改写建议
                if rid == "DBG-01" and cli:
                    hits.append({"id": rid, "severity": "P2", "category": "命令行输出",
                                 "file": path, "line": lineno,
                                 "code": text.strip()[:160],
                                 "why": "该文件是命令行入口，print 就是它的输出通道",
                                 "fix": "确认这是面向用户的输出；库函数里仍应走日志"})
                    continue
                hits.append({"id": rid, "severity": sev, "category": cat,
                             "file": path, "line": lineno,
                             "code": text.strip()[:160], "why": why, "fix": fix})
    return hits


def scan_swallowed_exception(files: dict[str, list[tuple[int, str]]]) -> list[dict]:
    """跨行的 `except:` / `except X:` 后面只有 pass——单行正则抓不到。"""
    hits = []
    for path, lines in files.items():
        lang = _lang(path)
        if lang not in ("py", "js"):
            continue
        for i, (lineno, text) in enumerate(lines):
            stripped = text.strip()
            if lang == "py" and re.match(r"^except\b.*:\s*$", stripped):
                nxt = lines[i + 1][1].strip() if i + 1 < len(lines) else ""
                # 只看紧邻的下一行；中间夹注释也算
                j = i + 1
                while j < len(lines) and lines[j][1].strip().startswith("#"):
                    j += 1
                nxt = lines[j][1].strip() if j < len(lines) else ""
                if nxt == "pass":
                    hits.append({"id": "ERR-01", "severity": "P0", "category": "吞异常",
                                 "file": path, "line": lineno,
                                 "code": f"{stripped} → pass",
                                 "why": "异常被吃掉，出错时无人知晓，排查全靠猜",
                                 "fix": "至少记日志并带上上下文；能处理就处理，不能处理就往上抛"})
            if lang == "js" and re.match(r"^(?:\}\s*)?catch\s*(?:\([^)]*\))?\s*\{\s*$", stripped):
                j = i + 1
                while j < len(lines) and lines[j][1].strip().startswith("//"):
                    j += 1
                nxt = lines[j][1].strip() if j < len(lines) else ""
                if nxt in ("}", "};"):
                    hits.append({"id": "ERR-02", "severity": "P1", "category": "空 catch",
                                 "file": path, "line": lineno, "code": f"{stripped} → {nxt}",
                                 "why": "空 catch 让失败静默通过",
                                 "fix": "补日志或向上抛，别让调用方以为成功"})
    return hits


def scan_commented_code(files: dict[str, list[tuple[int, str]]]) -> list[dict]:
    """连续 3 行以上的注释掉的代码——比单行 TODO 更值得清。"""
    hits = []
    for path, lines in files.items():
        if _lang(path) is None:
            continue
        run: list[tuple[int, str]] = []
        for lineno, text in lines + [(-1, "")]:
            if COMMENT_CODE_RE.match(text):
                run.append((lineno, text))
                continue
            if len(run) >= 3:
                hits.append({"id": "DBG-06", "severity": "P2", "category": "注释掉的代码",
                             "file": path, "line": run[0][0],
                             "code": f"{len(run)} 行，起于：{run[0][1].strip()[:100]}",
                             "why": "注释掉的代码没人敢删也没人看，只会越积越多",
                             "fix": "要留就留，不留就删——版本库里有历史"})
            run = []
    return hits


def scan_sleep(files: dict[str, list[tuple[int, str]]]) -> list[dict]:
    hits = []
    for path, lines in files.items():
        if is_test_file(path):
            continue
        for lineno, text in lines:
            if SLEEP_RE.search(text):
                hits.append({"id": "DBG-07", "severity": "P1", "category": "阻塞等待",
                             "file": path, "line": lineno, "code": text.strip()[:160],
                             "why": "靠 sleep 等结果，机器慢就失败、机器快就浪费",
                             "fix": "改轮询 + 超时，或直接同步调用"})
    return hits


def scan_test_gap(files: dict[str, list[tuple[int, str]]]) -> list[dict]:
    """改了实现代码，却没有对应测试改动。"""
    code = [f for f in files if _lang(f) and not is_test_file(f)
            and Path(f).suffix.lower() not in DOC_EXTS]
    tests = [f for f in files if is_test_file(f)]
    if not code or tests:
        return []
    return [{"id": "TST-01", "severity": "P1", "category": "测试缺口",
             "file": f, "line": 0, "code": "本次改动未触及任何测试文件",
             "why": "改完没补测试，回归只能靠手工，下次改动就是碰运气",
             "fix": "补对应用例；确实无需补，在 S2 报告里写明理由"} for f in code]


def scan_oversized(files: dict[str, list[tuple[int, str]]],
                   root: Path | None = None) -> list[dict]:
    hits = []
    for path, lines in files.items():
        if _lang(path) is None:
            continue
        # 新建文件本来就长，不该按「改动过大」报——只有往老文件里猛加才值得提醒
        if root is not None:
            total = len(_safe_read(root / path).splitlines())
            if total and len(lines) >= total:
                continue
        if len(lines) > MAX_ADDED_LINES_PER_FILE:
            hits.append({"id": "SIZ-01", "severity": "P2", "category": "改动过大",
                         "file": path, "line": 0,
                         "code": f"新增 {len(lines)} 行",
                         "why": f"单文件新增超过 {MAX_ADDED_LINES_PER_FILE} 行，评审会漏看",
                         "fix": "拆成多个提交，或先做结构改造再加功能"})
    return hits


def scan_long_functions(files: dict[str, list[tuple[int, str]]]) -> list[dict]:
    """粗判：新增行里从 def 起连续到结尾的行数。只对 Python 做，够了。"""
    hits = []
    for path, lines in files.items():
        if Path(path).suffix.lower() not in (".py", ".pyi"):
            continue
        start, count = None, 0
        for lineno, text in lines + [(-1, "")]:
            if re.match(r"^\s*(?:async\s+)?def\s+\w+", text):
                start, count = lineno, 1
                continue
            if start is not None:
                if text.strip() == "" or not text.startswith((" ", "\t")):
                    if count > MAX_FUNC_LINES:
                        hits.append({"id": "SIZ-02", "severity": "P2", "category": "函数过长",
                                     "file": path, "line": start,
                                     "code": f"新增函数约 {count} 行",
                                     "why": f"单函数超过 {MAX_FUNC_LINES} 行，分支难覆盖",
                                     "fix": "按职责拆分，或把内层逻辑抽成小函数"})
                    start = None
                else:
                    count += 1
    return hits


def untracked_lines(root: Path, files: list[str]) -> dict[str, list[tuple[int, str]]]:
    """未跟踪的新文件在 `git diff` 里看不到，但它们是这次新写的代码。

    整文件都当新增行，否则新建的处理逻辑完全逃过质量检查。
    """
    rc, out = _git(root, "status", "--porcelain", "-uall")
    if rc != 0:
        return {}
    untracked = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2].strip(), line[3:].strip()
        if "?" not in code:
            continue
        if " -> " in path:
            path = path.split(" -> ")[-1].strip()
        if files and path not in files:
            continue
        untracked.append(path)

    result = {}
    for path in untracked:
        if _lang(path) is None:
            continue
        f = root / path
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        result[path] = list(enumerate(text.splitlines(), 1))
    return result


def _collapse_cli_output(hits: list[dict]) -> list[dict]:
    """命令行入口的 print 每文件只留一条，否则一个工具几百行 print 会淹没报告。"""
    grouped: dict[str, list[dict]] = {}
    rest: list[dict] = []
    for h in hits:
        if h["id"] == "DBG-01" and h["category"] == "命令行输出":
            grouped.setdefault(h["file"], []).append(h)
        else:
            rest.append(h)
    for f, items in grouped.items():
        items.sort(key=lambda x: x["line"])
        first = dict(items[0])
        first["code"] = f"共 {len(items)} 处，起于：`{items[0]['code'][:100]}`"
        first["why"] = f"该文件是命令行入口，print 就是它的输出通道（{len(items)} 处）"
        rest.append(first)
    return rest


def run_quality_scan(root: Path, base: str, files: list[str],
                     diff_text: str | None = None) -> dict:
    """跑完整套扫描，返回 {hits, stats}。"""
    if diff_text is None:
        args = ["diff", base, "-U0", "--"]
        if files:
            args += files
        rc, diff_text = _git(root, *args)
        if rc != 0:
            return {"error": diff_text or "git diff 失败", "hits": [], "stats": {}}

    added = parse_added_lines(diff_text)
    if files:
        want = set(files)
        added = {k: v for k, v in added.items() if k in want}

    # 新建但还没 add 的文件，整份算新增
    for path, lines in untracked_lines(root, files).items():
        added.setdefault(path, lines)

    # 被删除的行也算「这次动过」，但新增为空时仍要看测试缺口
    changed_all = set(added) or set(files)

    hits = []
    hits += scan_rule_hits(added, root)
    hits += scan_swallowed_exception(added)
    hits += scan_commented_code(added)
    hits += scan_sleep(added)
    hits += scan_test_gap({f: [] for f in changed_all})
    hits += scan_oversized(added, root)
    hits += scan_long_functions(added)

    hits = _collapse_cli_output(hits)

    order = {"P0": 0, "P1": 1, "P2": 2}
    hits.sort(key=lambda h: (order.get(h["severity"], 9), h["file"], h["line"]))

    counts = {s: sum(1 for h in hits if h["severity"] == s) for s in ("P0", "P1", "P2")}
    return {"hits": hits, "stats": {
        "files": len(added), "added_lines": sum(len(v) for v in added.values()),
        "P0": counts["P0"], "P1": counts["P1"], "P2": counts["P2"],
    }, "added": added}


def exit_code(stats: dict) -> int:
    if stats.get("P0"):
        return 2
    if stats.get("P1"):
        return 1
    return 0


def render_markdown(result: dict, base: str, when: str) -> str:
    stats = result.get("stats", {})
    hits = result.get("hits", [])
    md = ["# 实现质量", "",
          f"- 生成时间：{when}",
          f"- 对比基线：{base}",
          f"- 改动文件：{stats.get('files', 0)} 个，新增 {stats.get('added_lines', 0)} 行",
          f"- 结果：**P0 {stats.get('P0', 0)} / P1 {stats.get('P1', 0)} / P2 {stats.get('P2', 0)}**", ""]

    if not hits:
        md += ["没有命中任何实现质量规则。", "",
               "> 这只说明没有常见硬伤，不等于实现没问题——设计、命名、边界条件仍要人看。", ""]
        return "\n".join(md)

    md += ["| 级别 | 类别 | 位置 | 问题 | 怎么改 |", "|---|---|---|---|---|"]
    for h in hits:
        loc = f"`{h['file']}:{h['line']}`" if h["line"] else f"`{h['file']}`"
        md.append(f"| {h['severity']} | {h['category']} | {loc} | {h['why']} | {h['fix']} |")
    md.append("")

    for sev, title in (("P0", "P0（阻断，必须改完才能继续）"),
                       ("P1", "P1（本次改完，或写明豁免理由）"),
                       ("P2", "P2（可排期）")):
        group = [h for h in hits if h["severity"] == sev]
        if not group:
            continue
        md += [f"## {title}", ""]
        for h in group:
            loc = f"{h['file']}:{h['line']}" if h["line"] else h["file"]
            md += [f"### {h['id']} {h['category']} — `{loc}`",
                   "", "```", h["code"], "```", "",
                   f"- 为什么：{h['why']}", f"- 怎么改：{h['fix']}", ""]

    md += ["> P0 不允许豁免。P1/P2 要豁免，就在 S2 报告里逐条写理由，别默默放过。", ""]
    return "\n".join(md)
