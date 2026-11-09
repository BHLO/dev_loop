"""把 .devloop 下的 Markdown 报告渲染成单文件 HTML 看板。

为什么要有这个：Markdown 在对话框里是一堆竖线和对齐的表格，看不出层次，
也没法一眼看出「现在卡在哪一阶段、还有几个 P0」。这里把阶段状态、质量结论、
各阶段报告合成一页，交给 present_files 打开，用户看到的是排版好的页面。

不依赖任何第三方库；样式与脚本全部内联，单个 .html 可以随便拷走。
Mermaid 图会加载 CDN 渲染，没有网络时退化成代码块，不会白屏。
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path

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
# 文件名必须和 devloop.DEFAULT_DOC 一致：大小写错了在 Linux 上会直接找不到，
# 页面就只剩一句「还没有报告」（Windows 因为不分大小写，一直没暴露）。
STAGE_ANCHOR = {
    "S0": "S0-需求澄清.md", "S1": "S1-影响面与方案.md", "S2": "S2-实现.md",
    "S3": "S3-变更审计.md", "S4": "S4-文档同步.md", "S5": "S5-健康检查.md",
    "S6": "S6-缺陷定位.md", "S7": "S7-提交清单.md",
}
STATUS_TEXT = {"approved": "已确认", "pending": "待确认", "skipped": "不适用"}

# 展示顺序：先阶段报告，再支撑材料（S7-提交清单.md 已经在 S7 里，不重复列）
EXTRA_FILES = [
    "实现质量.md", "静态盲区.md", "敏感文件预检.md", "代码对账.md",
    "文档对账.md", "验证报告.md", "基线复核.md", "文档漂移.md",
    "影响面.md", "依赖图.md", "文档基线.md",
]
REPORT_ORDER = [(st, name) for st, name in STAGE_ANCHOR.items()] + \
               [(None, name) for name in EXTRA_FILES]
FILE_TITLE = {
    "实现质量.md": "实现质量", "静态盲区.md": "静态分析盲区",
    "敏感文件预检.md": "敏感文件预检", "代码对账.md": "代码项对账",
    "文档对账.md": "文档项对账", "验证报告.md": "验证结果",
    "S7-提交清单.md": "提交清单", "基线复核.md": "文档基线复核",
    "文档漂移.md": "文档漂移", "影响面.md": "影响面", "依赖图.md": "依赖图",
    "文档基线.md": "文档基线",
}


# --------------------------------------------------------------------------- 极简 Markdown

def _inline(text: str) -> str:
    out = html.escape(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", r"<em>\1</em>", out)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                 r'<a href="\2" target="_blank" rel="noopener">\1</a>', out)
    return out


def _is_table_sep(line: str) -> bool:
    return bool(re.match(r"^\|[\s:\-|]+\|$", line.strip()))


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def md_to_html(text: str) -> str:
    """够用的子集：标题、表格、列表、代码块、引用、分隔线、行内格式。"""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # 代码块（含 mermaid）
        m = re.match(r"^```([\w+-]*)", line.strip())
        if m:
            lang = m.group(1)
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            code = "\n".join(buf)
            if lang == "mermaid":
                out.append(f'<pre class="mermaid">{html.escape(code)}</pre>')
            else:
                out.append(f'<pre class="code"><code>{html.escape(code)}</code></pre>')
            continue

        # 表格
        if line.strip().startswith("|") and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
            head = _cells(line)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            out.append('<div class="table-wrap"><table><thead><tr>'
                       + "".join(f"<th>{_inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>")
            for r in rows:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table></div>")
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = min(len(m.group(1)) + 1, 6)   # 页面已有 h1/h2，报告从 h3 起降
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # 分隔线
        if re.match(r"^\s*(?:---|\*\*\*|___)\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        # 引用（可能连续多行）
        if line.strip().startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].strip())
                i += 1
            out.append(f"<blockquote>{_inline(' '.join(buf))}</blockquote>")
            continue

        # 列表（有序/无序，支持一层缩进）
        if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+\S", line):
            ordered = bool(re.match(r"^\s*\d+[.)]\s+", line))
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>")
            while i < len(lines) and re.match(r"^\s*(?:[-*+]|\d+[.)])\s+\S", lines[i]):
                item = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", lines[i])
                out.append(f"<li>{_inline(item)}</li>")
                i += 1
            out.append(f"</{tag}>")
            continue

        if not line.strip():
            i += 1
            continue

        # 以 | 开头但没有分隔行：当作无表头的简易表，别让它退化成一行歪斜的文本
        if line.strip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            out.append('<div class="table-wrap"><table><tbody>')
            for r in rows:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table></div>")
            continue

        # 段落
        buf = []
        while i < len(lines) and lines[i].strip() and not re.match(
                r"^(#{1,6}\s|```|\||>|\s*(?:[-*+]|\d+[.)])\s|\s*(?:---|\*\*\*|___)\s*$)",
                lines[i]):
            buf.append(lines[i].strip())
            i += 1
        if not buf:
            # 兜底：这一行没被任何分支吃掉（比如以 | 开头却没有分隔行），
            # 直接当普通文本输出并前进，否则会原地打转把内存吃光
            out.append(f"<p>{_inline(line.strip())}</p>")
            i += 1
            continue
        out.append(f"<p>{_inline(' '.join(buf))}</p>")

    return "\n".join(out)


# --------------------------------------------------------------------------- 页面

CSS = """
:root{--bg:#f7f8fa;--card:#fff;--ink:#1f2937;--muted:#6b7280;--line:#e5e7eb;
--accent:#2563eb;--ok:#059669;--warn:#d97706;--bad:#dc2626;--code:#f3f4f6}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:32px 24px 64px}
header.top{border-bottom:1px solid var(--line);padding-bottom:20px;margin-bottom:24px}
h1{font-size:26px;margin:0 0 6px;letter-spacing:.5px}
.meta{color:var(--muted);font-size:13px}
.meta span{margin-right:16px}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;
font-weight:600;border:1px solid transparent}
.pill.ok{background:#ecfdf5;color:var(--ok);border-color:#a7f3d0}
.pill.warn{background:#fffbeb;color:var(--warn);border-color:#fde68a}
.pill.gray{background:#f3f4f6;color:var(--muted);border-color:var(--line)}
.pill.bad{background:#fef2f2;color:var(--bad);border-color:#fecaca}
.gates{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px;
margin:0 0 28px}
.gate{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.gate .st{font-size:12px;color:var(--muted);letter-spacing:1px}
.gate .nm{font-size:14px;font-weight:600;margin:4px 0 8px}
.gate .note{font-size:12px;color:var(--muted);margin-top:6px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin:0 0 24px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px 18px;min-width:110px}
.stat b{display:block;font-size:22px;line-height:1.2}
.stat span{font-size:12px;color:var(--muted)}
section.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:24px 28px;margin-bottom:20px}
section.card>h2{margin:0 0 4px;font-size:19px}
section.card>.src{color:var(--muted);font-size:12px;margin-bottom:16px}
h3{font-size:16px;margin:22px 0 8px;padding-left:10px;border-left:3px solid var(--accent)}
h4,h5,h6{font-size:15px;margin:18px 0 6px}
p{margin:8px 0}
code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:13px;
font-family:"Cascadia Mono",Consolas,monospace}
pre.code{background:#0f172a;color:#e2e8f0;padding:14px 16px;border-radius:8px;
overflow:auto;font-size:13px;line-height:1.6}
pre.code code{background:none;color:inherit;padding:0}
pre.mermaid{background:var(--code);padding:14px;border-radius:8px;overflow:auto;
font-size:13px;color:var(--muted)}
.table-wrap{overflow-x:auto;margin:12px 0}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border:1px solid var(--line);padding:8px 12px;text-align:left;vertical-align:top}
th{background:#f9fafb;font-weight:600;white-space:nowrap}
tbody tr:nth-child(even){background:#fcfcfd}
blockquote{margin:12px 0;padding:10px 16px;background:#f9fafb;
border-left:3px solid var(--accent);color:#374151;border-radius:0 6px 6px 0}
hr{border:0;border-top:1px solid var(--line);margin:20px 0}
ul,ol{margin:8px 0;padding-left:22px}
li{margin:4px 0}
a{color:var(--accent)}
.empty{color:var(--muted);font-style:italic}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:32px}
"""

MERMAID_SCRIPT = """
<script type="module">
try {
  const m = await import('https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs');
  m.default.initialize({startOnLoad:true, theme:'neutral'});
} catch (e) { /* 离线时保留源码，不白屏 */ }
</script>
"""


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _gate_cards(gates: dict) -> str:
    cards = []
    for st in STAGE_ANCHOR:
        g = gates.get(st) or {}
        status = g.get("status", "")
        cls = {"approved": "ok", "pending": "warn", "skipped": "gray"}.get(status, "gray")
        text = STATUS_TEXT.get(status, "未登记")
        rev = f" · v{g.get('revision')}" if g.get("revision") else ""
        note = g.get("note") or g.get("summary") or ""
        cards.append(
            f'<div class="gate"><div class="st">{st}{rev}</div>'
            f'<div class="nm">{STAGE_TITLE[st]}</div>'
            f'<span class="pill {cls}">{text}</span>'
            + (f'<div class="note" title="{html.escape(note)}">{html.escape(note)}</div>' if note else "")
            + "</div>")
    return '<div class="gates">' + "".join(cards) + "</div>"


def _stats_strip(quality_data: dict, gates: dict, state: dict) -> str:
    approved = sum(1 for k, g in gates.items()
                   if not k.startswith("_") and isinstance(g, dict)
                   and g.get("status") in ("approved", "skipped"))
    p0 = p1 = p2 = 0
    q = quality_data.get("stats") or {}
    p0, p1, p2 = q.get("P0", 0), q.get("P1", 0), q.get("P2", 0)
    items = [
        (str(approved), "阶段已确认", "ok" if approved else "gray"),
        (str(p0), "P0 实现问题", "bad" if p0 else "ok"),
        (str(p1), "P1 实现问题", "warn" if p1 else "ok"),
        (str(p2), "P2 建议", "gray"),
        (str(len(state.get("open_items", []))), "未闭环项", "warn" if state.get("open_items") else "gray"),
    ]
    return '<div class="stats">' + "".join(
        f'<div class="stat"><b>{v}</b><span>{label}</span></div>' for v, label, _ in items) + "</div>"


def build_html(root: Path, version: str, title: str = "研发闭环 · 报告看板",
               seq: int = 0, prev_href: str = "") -> tuple[str, int]:
    """返回 (html, 渲染了几份报告)。"""
    out_dir = root / ".devloop"
    gates = _load_json(out_dir / "确认记录.json")
    state = _load_json(out_dir / "状态.json")
    quality_data = _load_json(out_dir / "实现质量.json")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    round_name = state.get("round") or "—"
    stage = state.get("stage") or "—"

    sections, count = [], 0
    for st, name in REPORT_ORDER:
        f = out_dir / name
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        count += 1
        heading = f"{st} · {STAGE_TITLE[st]}" if st else FILE_TITLE.get(name, name)
        sections.append(
            f'<section class="card"><h2>{html.escape(heading)}</h2>'
            f'<div class="src">.devloop/{html.escape(name)}</div>'
            f"{md_to_html(text)}</section>")

    if not sections:
        sections.append('<section class="card"><h2>还没有报告</h2>'
                        '<p class="empty">先跑任意一个分析命令，产物会出现在这里。</p></section>')

    seq_html = f"<span>第 {seq} 版</span>" if seq else ""
    prev_html = (f'<span><a href="{html.escape(prev_href)}">← 上一版</a></span>'
                 if prev_href else "")

    body = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<header class="top">
  <h1>{html.escape(title)}</h1>
  <div class="meta">
    <span>dev-loop {html.escape(version)}</span>
    <span>阶段 {html.escape(stage)}</span>
    <span>轮次 {html.escape(round_name)}</span>
    {seq_html}
    <span>生成于 {now}</span>
    {prev_html}
  </div>
</header>
{_gate_cards(gates)}
{_stats_strip(quality_data, gates, state)}
{''.join(sections)}
<footer>由 dev-loop 生成 · 报告只读，改动请在 .devloop/ 下的 Markdown 里做</footer>
</div>{MERMAID_SCRIPT}</body></html>"""
    return body, count


HISTORY_DIR = "看板历史"
KEEP_HISTORY = 10


def _history_files(out_dir: Path) -> list[Path]:
    return sorted((out_dir / HISTORY_DIR).glob("看板-*.html"))


def write_report(root: Path, version: str, name: str = "看板.html",
                 keep: int = KEEP_HISTORY) -> dict:
    """渲染看板，同时留一份历史快照。

    覆盖式的 看板.html 只回答「现在什么样」，回答不了「上周批的是什么」。
    历史按时间戳命名，保留最近 keep 份。
    """
    out_dir = root / ".devloop"
    out_dir.mkdir(parents=True, exist_ok=True)
    hist_dir = out_dir / HISTORY_DIR
    hist_dir.mkdir(parents=True, exist_ok=True)

    existing = _history_files(out_dir)
    # 版本号取历史最大值 +1：裁剪之后 len() 会回退，导致编号重复
    seq = 1
    for f in existing:
        m = re.search(r"-(\d{3})\.html$", f.name)
        if m:
            seq = max(seq, int(m.group(1)) + 1)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snap_name = f"看板-{stamp}-{seq:03d}.html"
    prev_href = f"{HISTORY_DIR}/{existing[-1].name}" if existing else ""

    html_text, count = build_html(root, version, seq=seq, prev_href=prev_href)
    target = out_dir / name
    target.write_text(html_text, encoding="utf-8")
    snapshot = hist_dir / snap_name
    snapshot.write_text(html_text, encoding="utf-8")

    pruned = 0
    files = _history_files(out_dir)
    for old_file in files[:-keep] if len(files) > keep else []:
        try:
            old_file.unlink()
            pruned += 1
        except OSError:
            continue

    return {"path": target, "count": count, "snapshot": snapshot,
            "seq": seq, "pruned": pruned, "history": _history_files(out_dir)}


def list_history(root: Path) -> list[dict]:
    out_dir = root / ".devloop"
    items = []
    for f in _history_files(out_dir):
        items.append({"name": f.name, "size": f.stat().st_size,
                      "time": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
    return items
