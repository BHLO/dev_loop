"""业务前端 UI 静态检查：抓「一眼能判定的丑与不可用」。

补的是 S2 之后的空档——代码能跑、测试也绿，但页面是「AI 拼出来的」：
颜色散落改不动主题、点了没焦点、状态只有成功一条路、窄屏横着跑、
紫蓝渐变 + 卡片套卡片。

只看这次改动的文件，不翻存量；存量 UI 债归 S5。

参考来源：本地 `hallmark` 的 Floor（反生成感硬线）与 `frontend-ui-engineering`
（设计系统遵循、WCAG AA）。这里挑的都是**能静态判定、误报低**的条目。
"""

from __future__ import annotations

import re
from pathlib import Path

CSS_EXTS = {".css", ".scss", ".sass", ".less", ".styl"}
MARKUP_EXTS = {".vue", ".tsx", ".jsx", ".svelte", ".html", ".htm"}
UI_EXTS = CSS_EXTS | MARKUP_EXTS

PURPLE = r"#(?:7c3aed|8b5cf6|a855f7|6366f1|818cf8|4f46e5|c084fc)\b"
COLOR = r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b|rgba?\([^)]*\)"

# (id, 级别, 标题, 正则, 为什么, 怎么改)  —— 逐条匹配，同类最多报 5 条
REGEX_RULES: list[tuple] = [
    ("UI-01", "P0", "transition: all",
     r"transition\s*:\s*all\b",
     "所有属性一起动，布局属性也会被动画，滚动时整页抖",
     "只写要动的属性：transition: background-color .15s, border-color .15s"),

    ("UI-02", "P0", "渐变用了紫蓝",
     r"linear-gradient\([^)]*(?:" + PURPLE + r"|purple|indigo)",
     "紫蓝渐变是「AI 生成」最强的信号，且和多数品牌色打架",
     "换单色或同色系深浅；要渐变就用品牌色的两个邻近色"),

    ("UI-03", "P0", "图片缺 alt",
     r"<img\b(?![^>]*\balt=)[^>]*>",
     "读屏软件读到的是空，图挂了也没有兜底文字",
     "补 alt（装饰图用 alt=\"\"）"),

    ("UI-04", "P1", "100vw 撑出横向滚动",
     r"\b100vw\b",
     "100vw 含滚动条宽度，垂直滚动条一出现就多一条横向滚动",
     "改用 width:100% 或 100dvw"),

    ("UI-05", "P1", "动画布局属性",
     r"(?:transition|animation)[^;{}]*\b(?:width|height|top|left|right|bottom)\b",
     "这四项触发重排，帧率掉得厉害",
     "改用 transform / opacity"),

    ("UI-06", "P1", "div 当按钮用",
     r"<div\b[^>]*\bonClick\b",
     "键盘 Tab 到不了、回车触发不了，读屏也认不出是按钮",
     "换 <button>；确实不能换就补 role=\"button\" tabindex=\"0\" 与键盘处理"),

    ("UI-07", "P1", "emoji 当图标",
     r">\s*[\U0001F300-\U0001FAFF\u2600-\u27BF]\s*<",
     "emoji 在不同系统上长得不一样，尺寸和对齐也不受控",
     "换图标库（lucide / heroicons）或内联 SVG"),

    ("UI-08", "P2", "内联 style 过多",
     r"\bstyle\s*=\s*[\"{]",
     "样式散在标签上，改主题时找不到、也没法复用",
     "收敛到 class 或组件样式；一次性的布局微调可以留"),
]

# 文件级判断：(id, 级别, 标题, 检查函数, 为什么, 怎么改)
FILE_RULES: list[tuple] = []


def _style_map(text: str, ext: str) -> tuple[str, list[tuple[int, int, int]]]:
    """取出样式文本，并记住每一段在原文里的位置。

    返回 (样式文本, [(段内起, 段内止, 原文起)])——报错要指回原文行号，
    否则 `transition: all` 会被标到模板第一行上去。
    """
    if ext in CSS_EXTS:
        return text, [(0, len(text), 0)]
    parts: list[str] = []
    spans: list[tuple[int, int, int]] = []
    cursor = 0
    for m in re.finditer(r"<style[^>]*>(.*?)</style>", text, re.S):
        seg = m.group(1)
        spans.append((cursor, cursor + len(seg), m.start(1)))
        parts.append(seg)
        cursor += len(seg) + 1          # join 用 "\n"
    style = "\n".join(parts)
    if not style.strip():
        return text, [(0, len(text), 0)]
    return style, spans


def _abs_offset(spans: list[tuple[int, int, int]], i: int) -> int | None:
    for a, b, orig in spans:
        if a <= i < b:
            return orig + (i - a)
    return None


def _colors_outside_root(style: str) -> int:
    """:root 之外出现的色值个数——token 化程度的最低判据。"""
    root_spans = [(m.start(), m.end()) for m in
                  re.finditer(r":root[^{]*\{(?:[^{}]|\{[^{}]*\})*\}", style)]
    count = 0
    for m in re.finditer(COLOR, style):
        if any(a <= m.start() < b for a, b in root_spans):
            continue
        count += 1
    return count


def _check_colors(style: str) -> str:
    n = _colors_outside_root(style)
    if n > 12:
        return f":root 之外有 {n} 个裸色值——主题改不动，暗色模式也没法加"
    return ""


def _check_focus(style: str) -> str:
    if ":focus-visible" in style or ":focus" in style:
        return ""
    if re.search(r":hover|button|a\s*\{|input", style):
        return "有交互样式但没有任何 :focus-visible——键盘用户看不见自己在哪"
    return ""


def _check_motion(style: str) -> str:
    if "prefers-reduced-motion" in style:
        return ""
    if re.search(r"transition\s*:|animation\s*:|@keyframes", style):
        return "有动效但没有 prefers-reduced-motion 兜底——晕动症用户关不掉"
    return ""


def _check_states(text: str) -> str:
    """有请求就要有加载/空/错误三条退路。"""
    if not re.search(r"\b(fetch|axios|useQuery|useSWR|request)\s*[.(]", text):
        return ""
    has = [k for k in ("loading", "isLoading", "pending", "skeleton",
                       "error", "catch", "empty", "NoData", "placeholder")
           if re.search(k, text, re.I)]
    if len(has) < 2:
        return "发了请求，但没看到加载态与错误态——网慢或接口挂了页面就是一片空白"
    return ""


def _check_tokens(style: str) -> str:
    if "--" in style and re.search(r":root|@theme|--[\w-]+\s*:", style):
        return ""
    if _colors_outside_root(style) > 6:
        return "没有任何 token 定义，颜色全是散落的字面量"
    return ""


def _px_values(style: str, prop: str) -> list[int]:
    return [int(m.group(1)) for m in re.finditer(
        rf"\b{prop}(?:-[a-z]+)?\s*:\s*(\d+)px", style)]


def _check_spacing(style: str) -> str:
    """间距不在 4 的倍数上——最常见的「看着别扭但说不出哪别扭」。"""
    vals = []
    for prop in ("margin", "padding", "gap"):
        vals += _px_values(style, prop)
    vals = [v for v in vals if v > 4]
    if len(vals) < 4:
        return ""
    odd = [v for v in vals if v % 4]
    if len(odd) / len(vals) < 0.5:
        return ""
    sample = "、".join(str(v) for v in sorted(set(odd))[:5])
    return f"{len(odd)}/{len(vals)} 处间距不在 4 的倍数上（{sample}）——节奏会散"


def _check_type_scale(style: str) -> str:
    """字号要么太多要么太少，两头都会显得不专业。"""
    sizes = {m.group(1) for m in re.finditer(r"font-size\s*:\s*([\d.]+(?:px|rem|em))", style)}
    classes = set(re.findall(r"\.([a-z][\w-]*)\s*\{", style))
    if len(sizes) >= 7:
        return f"用了 {len(sizes)} 种字号——没有统一的字号阶梯"
    # 至少三个组件类才谈得上「层级」；单个组件的片段不该被判成扁平
    if len(classes) >= 3 and len(sizes) <= 1:
        return "所有文字同一个字号——层级只能靠加粗，读起来是平的"
    return ""


def _check_primary_buttons(text: str) -> str:
    """一屏一个主操作。三个主按钮等于没有主按钮。"""
    hits = re.findall(
        r'class="[^"]*\b(?:btn-primary|button--primary|ant-btn-primary|'
        r'el-button--primary|primary)\b', text)
    if len(hits) > 2:
        return f"同一屏出现 {len(hits)} 个主按钮——用户不知道该点哪个"
    return ""


def _check_card_nesting(text: str) -> str:
    """卡片里套卡片，边框和圆角叠三层，层次全乱。"""
    m = re.search(r'<(?:div|section|article)\b[^>]*class="[^"]*\bcard\b[^"]*"[^>]*>'
                  r'(?:(?!</(?:div|section|article)>).){0,400}?'
                  r'<(?:div|section|article)\b[^>]*class="[^"]*\bcard\b', text, re.S)
    if m:
        line = text[:m.start()].count("\n") + 1
        return f"第 {line} 行附近卡片套卡片——去掉内层边框，用间距分组"
    return ""


def _check_table_borders(style: str) -> str:
    """表格四边全框是电子表格的样子，不是产品界面。"""
    if not re.search(r"\btable\b", style):
        return ""
    full = re.search(r"table\s*[,{][^{}]*\bborder\s*:\s*1px", style) and \
        re.search(r"t[hd]\s*[,{][^{}]*\bborder\s*:\s*1px", style)
    if full:
        return "表格与单元格都画了四边边框——改成只留横线分隔"
    return ""


FILE_RULES.extend([
    ("UI-09", "P1", "裸色值过多", lambda style, text: _check_colors(style),
     "颜色写死在各处，换主题要全局搜替换，暗色模式更无从下手",
     "把主色/背景/文字/边框提到 :root 的 token，组件只引用 var(--x)"),
    ("UI-10", "P1", "缺焦点态", lambda style, text: _check_focus(style),
     "键盘用户 Tab 过去没有任何视觉反馈，等于不能用",
     "加 :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }"),
    ("UI-11", "P2", "动效不可关", lambda style, text: _check_motion(style),
     "晕动症用户没有退出通道",
     "加 @media (prefers-reduced-motion: reduce) { * { transition: none; animation: none } }"),
    ("UI-12", "P1", "请求无状态处理", lambda style, text: _check_states(text),
     "只有成功路径，慢网和报错时用户看到的是空白或旧数据",
     "补齐 loading / empty / error 三态，错误要能重试"),
    ("UI-13", "P2", "没有设计 token", lambda style, text: _check_tokens(style),
     "没有 token 就没有统一的设计语言，页面会越改越花",
     "先定 :root 的色板/字号/间距，再写组件"),
    ("UI-14", "P2", "间距没有节奏", lambda style, text: _check_spacing(style),
     "13/17/21 这种间距混着来，肉眼会觉得「说不上哪不对但就是别扭」",
     "统一到 4 的倍数：4/8/12/16/24/32/48"),
    ("UI-15", "P2", "字号阶梯混乱", lambda style, text: _check_type_scale(style),
     "字号太多没有主次，太少则是平的——两种情况都不像设计过",
     "收成 3–4 级：页面标题 / 区块标题 / 正文 / 辅助"),
    ("UI-16", "P1", "一屏多个主按钮", lambda style, text: _check_primary_buttons(text),
     "三个主按钮等于没有主操作，用户的视线无处落",
     "只留一个主按钮，其余用描边或纯文字按钮"),
    ("UI-17", "P1", "卡片套卡片", lambda style, text: _check_card_nesting(text),
     "边框和圆角叠两层，层次全糊在一起",
     "去掉内层边框，用间距或浅背景分组"),
    ("UI-18", "P2", "表格四边框", lambda style, text: _check_table_borders(style),
     "四边全框像 Excel，视觉噪音压过内容",
     "只留横线分隔 + 浅底表头"),
])


MARKUP_RULES = {"UI-03", "UI-04", "UI-06", "UI-07", "UI-08"}


def scan_text(path: str, text: str) -> list[dict]:
    ext = Path(path).suffix.lower()
    if ext not in UI_EXTS:
        return []
    style, spans = _style_map(text, ext)
    out: list[dict] = []

    for rid, level, title, pat, why, fix in REGEX_RULES:
        in_markup = rid in MARKUP_RULES
        target = text if in_markup else style
        for m in list(re.finditer(pat, target))[:5]:
            pos = m.start() if in_markup else _abs_offset(spans, m.start())
            if pos is None:
                continue
            line = text[:pos].count("\n") + 1
            out.append({"id": rid, "level": level, "title": title, "file": path,
                        "line": line, "why": why, "fix": fix,
                        "snippet": text.splitlines()[line - 1].strip()[:90]})

    for rid, level, title, fn, why, fix in FILE_RULES:
        msg = fn(style, text)
        if msg:
            out.append({"id": rid, "level": level, "title": title, "file": path,
                        "line": 0, "why": why, "fix": fix, "snippet": msg})
    return out


def scan(root: Path, files: list[str]) -> list[dict]:
    issues: list[dict] = []
    for rel in files:
        p = root / rel
        if not p.is_file() or p.suffix.lower() not in UI_EXTS:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        issues.extend(scan_text(rel, text))
    order = {"P0": 0, "P1": 1, "P2": 2}
    issues.sort(key=lambda x: (order.get(x["level"], 9), x["file"], x["line"]))
    return issues


def render_md(issues: list[dict], files: list[str]) -> str:
    md = ["# UI 质量", "", f"- 检查文件：{len(files)} 个前端文件",
          f"- 结果：**P0 {sum(1 for i in issues if i['level'] == 'P0')} / "
          f"P1 {sum(1 for i in issues if i['level'] == 'P1')} / "
          f"P2 {sum(1 for i in issues if i['level'] == 'P2')}**", ""]
    if not issues:
        md += ["没有命中规则。这只是「没有常见硬伤」，版面是否好看仍要人看：",
               "", "- 三级层级是否拉开（页标题 / 区块标题 / 正文）",
               "- 空态、加载态、错误态是否都想清楚", ""]
        return "\n".join(md)

    md += ["| 级别 | 规则 | 位置 | 问题 | 怎么改 |", "|---|---|---|---|---|"]
    for i in issues:
        where = f"`{i['file']}`" + (f":{i['line']}" if i["line"] else "")
        md.append(f"| {i['level']} | {i['id']} {i['title']} | {where} "
                  f"| {i['snippet'] or i['why']} | {i['fix']} |")
    md += ["", "## 为什么这些算问题", ""]
    seen = set()
    for i in issues:
        if i["id"] in seen:
            continue
        seen.add(i["id"])
        md.append(f"- **{i['id']} {i['title']}**：{i['why']}")
    return "\n".join(md)
