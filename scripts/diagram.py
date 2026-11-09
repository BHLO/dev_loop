"""把 mermaid flowchart 的一个子集画成内联 SVG。

为什么不用现成的 mermaid：它要 CDN（`<script type="module">` 拉 jsdelivr），
离线、内网、或 CDN 被墙时页面就退化成一段代码——「生成出来的是图」这件事
不该取决于网络。这里用纯标准库算一次布局，直接吐 `<svg>`，粘到哪都能看。

支持的写法（够画依赖图和常规架构图）：

    flowchart LR / graph TD
      A["标签"] --> B(圆角) --> C{判断}
      A -->|"调用"| B
      A -.-> B        A ==> B        A --- B
      classDef xxx fill:#fff,stroke:#333,color:#111
      class A,B xxx

不支持的（sequenceDiagram、classDiagram、subgraph 嵌套语义等）返回 ``None``，
由调用方退回代码块——宁可显示源码，也不要画错。
"""

from __future__ import annotations

import html
import re

FONT_SIZE = 12.5
FONT_FAMILY = ("-apple-system,BlinkMacSystemFont,Segoe UI,Malgun Gothic,"
               "Microsoft YaHei,Hiragino Sans GB,sans-serif")
LINE_H = 17
PAD_X = 14
PAD_Y = 9
MAX_TEXT_W = 210
GAP_X = 56
GAP_Y = 26
MARGIN = 16

DEFAULT_NODE = {"fill": "#F8FAFC", "stroke": "#CBD5E1", "color": "#334155", "dash": ""}

# mermaid 节点形状：xxx["文本"] xxx("文本") xxx{文本} xxx[(库)] xxx[[子程序]] xxx((圆))
NODE_FORMS = [
    (re.compile(r'^([\w.-]+)\[\((?P<t>[^)\]]*)\)\]$'), "cyl"),
    (re.compile(r'^([\w.-]+)\[\[(?P<t>[^\]]*)\]\]$'), "sub"),
    (re.compile(r'^([\w.-]+)\[/(?P<t>[^/\]]*)/\]$'), "para"),
    (re.compile(r'^([\w.-]+)\(\((?P<t>[^)]*)\)\)$'), "circle"),
    (re.compile(r'^([\w.-]+)\[(?P<t>[^\]]*)\]$'), "rect"),
    (re.compile(r'^([\w.-]+)\((?P<t>[^)]*)\)$'), "round"),
    (re.compile(r'^([\w.-]+)\{(?P<t>[^}]*)\}$'), "diamond"),
    (re.compile(r'^([\w.-]+)>?(?P<t>[^\]]*)\]$'), "rect"),
    (re.compile(r'^([\w.-]+)$'), ""),
]

EDGE_RE = re.compile(r"\s*(-{2,3}>|={2,}>|-\.[-.]*->|-{2,3}>{2}|-{2,3}|={2,3}|-\.[-.]*)\s*")
LABEL_RE = re.compile(r'^\|(?P<t>[^|]*)\|')
HEADER_RE = re.compile(r"^\s*(?:flowchart|graph)\s*(?P<d>LR|RL|TB|BT|TD)?", re.I)


def _text_width(s: str) -> float:
    """粗略估宽：中文/全角按一个字号，西文按 0.56 个字号。"""
    return sum(FONT_SIZE if ord(c) > 0x2E80 else FONT_SIZE * 0.56 for c in s)


def _wrap(text: str, limit: float = MAX_TEXT_W) -> list[str]:
    """按估算宽度折行，最多两行；装不下的部分截断加省略号。"""
    text = text.strip()
    if not text:
        return [""]
    lines: list[str] = []
    cur, w, i = "", 0.0, 0
    while i < len(text):
        ch = text[i]
        cw = FONT_SIZE if ord(ch) > 0x2E80 else FONT_SIZE * 0.56
        if w + cw > limit and cur:
            lines.append(cur)
            if len(lines) == 2:
                # 第二行也满了：剩下的不要了，但必须让人看出来被截断
                lines[1] = lines[1][:-1] + "…" if len(lines[1]) > 1 else lines[1] + "…"
                return lines
            cur, w = "", 0.0
            continue                     # 这个字符留给下一行，不推进 i
        cur += ch
        w += cw
        i += 1
    if cur:
        lines.append(cur)
    return lines


def _strip_quotes(text: str) -> str:
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    return t


def parse(src: str) -> dict | None:
    """解析 mermaid 子集，返回 {direction, nodes, edges, classes}；看不懂就是 None。"""
    if not src or not src.strip():
        return None
    lines = [ln.rstrip() for ln in src.splitlines()]
    head = next((ln for ln in lines if ln.strip() and not ln.strip().startswith("%%")), "")
    m = HEADER_RE.match(head)
    if not m:
        return None                      # 不是 flowchart（时序图/类图等）
    direction = (m.group("d") or "TB").upper()
    if direction == "TD":
        direction = "TB"

    order: list[str] = []
    labels: dict[str, str] = {}
    shape: dict[str, str] = {}
    edges: list[dict] = []
    class_def: dict[str, dict] = {}
    class_of: dict[str, str] = {}
    declared_class: list[tuple[str, str]] = []

    def touch(node_id: str, label: str | None = None, kind: str = "") -> None:
        if node_id not in labels:
            order.append(node_id)
            labels[node_id] = label if label is not None else node_id
            shape[node_id] = kind or "rect"
        elif label:
            labels[node_id] = label

    def take_node(chunk: str) -> str | None:
        chunk = chunk.strip()
        if not chunk:
            return None
        for rx, kind in NODE_FORMS:
            mm = rx.match(chunk)
            if mm:
                node_id = mm.group(1)
                label = _strip_quotes(mm.group("t")) if kind else None
                touch(node_id, label, kind)
                return node_id
        return None

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("%%") or HEADER_RE.match(line):
            continue
        if line.startswith(("subgraph", "end", "direction")):
            continue
        if line.startswith("classDef"):
            body = line[len("classDef"):].strip()
            name, _, style = body.partition(" ")
            props = {}
            for part in style.split(","):
                k, _, v = part.partition(":")
                if k.strip() in ("fill", "stroke", "color", "stroke-dasharray"):
                    props[k.strip()] = v.strip()
            class_def[name.strip()] = props
            continue
        if line.startswith("class "):
            body = line[len("class"):].strip()
            ids, _, name = body.partition(" ")
            for node_id in ids.split(","):
                if node_id.strip():
                    declared_class.append((node_id.strip(), name.strip()))
            continue
        if line.startswith(("style ", "click ", "linkStyle", ":::")):
            continue

        # 节点声明（不成边）
        if "--" not in line and "==" not in line and "-." not in line:
            take_node(line)
            continue

        # 边：可能连成一条链 A --> B --> C
        parts = EDGE_RE.split(line)
        if len(parts) < 3:
            continue
        prev = take_node(parts[0])
        idx = 1
        while idx + 1 < len(parts):
            arrow = parts[idx]
            rest = parts[idx + 1] if idx + 1 < len(parts) else ""
            label = ""
            lm = LABEL_RE.match(rest.strip())
            if lm:
                label = _strip_quotes(lm.group("t"))
                rest = rest.strip()[lm.end():]
            nxt = take_node(rest)
            dashed = "." in arrow
            if prev and nxt:
                edges.append({"src": prev, "dst": nxt, "label": label, "dashed": dashed})
            prev = nxt
            idx += 2

    for node_id, name in declared_class:
        if node_id in labels:
            class_of[node_id] = name

    if not order:
        return None
    return {"direction": direction, "nodes": order, "labels": labels,
            "shape": shape, "edges": edges, "class_def": class_def, "class_of": class_of}


def _back_edges(nodes: list[str], edges: list[dict]) -> set[tuple[str, str]]:
    """DFS 找回边（指回栈上节点的边）。回边照画，但不参与分层。"""
    adj: dict[str, list[str]] = {}
    for e in edges:
        adj.setdefault(e["src"], []).append(e["dst"])
    state: dict[str, int] = {}
    back: set[tuple[str, str]] = set()
    for root in nodes:
        if state.get(root):
            continue
        state[root] = 1
        stack = [(root, iter(adj.get(root, [])))]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[node] = 2
                stack.pop()
                continue
            if state.get(nxt) == 1:
                back.add((node, nxt))
                continue
            if state.get(nxt) == 2:
                continue
            state[nxt] = 1
            stack.append((nxt, iter(adj.get(nxt, []))))
    return back


def _layers(nodes: list[str], edges: list[dict]) -> dict[str, int]:
    """最长路径分层。先摘掉回边，否则「审计打回实现」这类反馈边会把层号越推越高。"""
    back = _back_edges(nodes, edges)
    layer = {n: 0 for n in nodes}
    for _ in range(len(nodes)):
        changed = False
        for e in edges:
            if (e["src"], e["dst"]) in back:
                continue
            if layer[e["dst"]] < layer[e["src"]] + 1:
                layer[e["dst"]] = layer[e["src"]] + 1
                changed = True
        if not changed:
            break
    return layer


def _style(node: str, data: dict) -> dict:
    base = dict(DEFAULT_NODE)
    name = data["class_of"].get(node, "")
    for k, v in (data["class_def"].get(name) or {}).items():
        if k == "stroke-dasharray":
            base["dash"] = v
        else:
            base[k] = v
    return base


def to_svg(src: str) -> str | None:
    """mermaid 源码 → 内联 SVG 字符串；看不懂返回 None。"""
    data = parse(src)
    if not data:
        return None

    nodes, labels, shape = data["nodes"], data["labels"], data["shape"]
    edges = data["edges"]
    layer = _layers(nodes, edges)
    direction = data["direction"]

    wrapped = {n: _wrap(labels[n]) for n in nodes}
    heights = {n: PAD_Y * 2 + LINE_H * len(wrapped[n]) for n in nodes}
    widths = {n: min(MAX_TEXT_W, max(70.0, max(_text_width(t) for t in wrapped[n]))) + PAD_X * 2
              for n in nodes}

    groups: dict[int, list[str]] = {}
    for n in nodes:
        groups.setdefault(layer[n], []).append(n)
    layers = sorted(groups)

    pos: dict[str, tuple[float, float, float, float]] = {}

    if direction in ("LR", "RL"):
        col_w = {k: max(widths[n] for n in groups[k]) for k in layers}
        col_h = {k: sum(heights[n] for n in groups[k]) + GAP_Y * (len(groups[k]) - 1)
                 for k in layers}
        total_h = max(col_h.values())
        y0, x = MARGIN, MARGIN
        cols = {}
        for k in layers:
            cols[k] = (x, col_w[k])
            x += col_w[k] + GAP_X
        total_w = x - GAP_X + MARGIN
        for k in layers:
            cy = MARGIN + (total_h - col_h[k]) / 2
            for n in groups[k]:
                pos[n] = (cols[k][0], cy, widths[n], heights[n])
                cy += heights[n] + GAP_Y
        if direction == "RL":
            for n, (px, py, w, h) in pos.items():
                pos[n] = (total_w - px - w, py, w, h)
    else:                                        # TB / BT
        row_h = {k: max(heights[n] for n in groups[k]) for k in layers}
        row_w = {k: sum(widths[n] for n in groups[k]) + GAP_X * (len(groups[k]) - 1)
                 for k in layers}
        total_w = max(row_w.values())
        y, rows = MARGIN, {}
        for k in layers:
            rows[k] = (y, row_h[k])
            y += row_h[k] + GAP_Y
        total_h = y - GAP_Y + MARGIN
        for k in layers:
            cx = MARGIN + (total_w - row_w[k]) / 2
            for n in groups[k]:
                pos[n] = (cx, rows[k][0], widths[n], heights[n])
                cx += widths[n] + GAP_X
        if direction == "BT":
            for n, (px, py, w, h) in pos.items():
                pos[n] = (px, total_h - py - h, w, h)

    total_w = max(px + w for px, _, w, _ in pos.values()) + MARGIN
    total_h = max(py + h for _, py, _, h in pos.values()) + MARGIN

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_w:.0f} {total_h:.0f}" '
           f'width="100%" style="max-width:{total_w:.0f}px;height:auto" role="img" '
           f'aria-label="架构图">',
           '<defs><marker id="dl-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
           'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
           '<path d="M0,0 L10,5 L0,10 z" fill="#64748B"/></marker></defs>']

    # 先画边，节点盖在上面
    for e in edges:
        if e["src"] not in pos or e["dst"] not in pos:
            continue
        sx, sy, sw, sh = pos[e["src"]]
        dx, dy, dw, dh = pos[e["dst"]]
        if direction in ("LR", "RL"):
            x1, y1 = sx + sw, sy + sh / 2
            x2, y2 = dx, dy + dh / 2
            if x2 <= x1:                       # 同层或回流：从上方绕
                mid = (y1 + y2) / 2 - 18
                d = f"M{x1:.0f},{y1:.0f} C{x1 + 26:.0f},{mid:.0f} {x2 - 26:.0f},{mid:.0f} {x2:.0f},{y2:.0f}"
            else:
                d = f"M{x1:.0f},{y1:.0f} L{x2:.0f},{y2:.0f}"
        else:
            x1, y1 = sx + sw / 2, sy + sh
            x2, y2 = dx + dw / 2, dy
            if y2 <= y1:
                mid = (x1 + x2) / 2 - 18
                d = f"M{x1:.0f},{y1:.0f} C{mid:.0f},{y1 + 26:.0f} {mid:.0f},{y2 - 26:.0f} {x2:.0f},{y2:.0f}"
            else:
                d = f"M{x1:.0f},{y1:.0f} L{x2:.0f},{y2:.0f}"
        dash = ' stroke-dasharray="5 4"' if e["dashed"] else ""
        out.append(f'<path d="{d}" fill="none" stroke="#64748B" stroke-width="1.4"'
                   f'{dash} marker-end="url(#dl-arrow)"/>')
        if e["label"]:
            lx, ly = (x1 + x2) / 2, (y1 + y2) / 2
            label = e["label"][:18]
            w = _text_width(label)
            out.append(f'<rect x="{lx - w / 2 - 4:.0f}" y="{ly - 9:.0f}" width="{w + 8:.0f}" '
                       f'height="17" rx="3" fill="#FFFFFF" opacity="0.92"/>')
            out.append(f'<text x="{lx:.0f}" y="{ly + 4:.0f}" text-anchor="middle" '
                       f'font-size="11" fill="#475569">{html.escape(label)}</text>')

    for n in nodes:
        x, y, w, h = pos[n]
        st = _style(n, data)
        dash = f' stroke-dasharray="{st["dash"]}"' if st["dash"] else ""
        rx = h / 2 if shape.get(n) == "round" else (6 if shape.get(n) != "circle" else h / 2)
        out.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" '
                   f'rx="{rx:.0f}" fill="{st["fill"]}" stroke="{st["stroke"]}" '
                   f'stroke-width="1.4"{dash}/>')
        lines = wrapped[n]
        base_y = y + h / 2 - (len(lines) - 1) * LINE_H / 2 + FONT_SIZE * 0.36
        for i, text in enumerate(lines):
            out.append(f'<text x="{x + w / 2:.0f}" y="{base_y + i * LINE_H:.0f}" '
                       f'text-anchor="middle" font-size="{FONT_SIZE}" '
                       f'font-family="{FONT_FAMILY}" fill="{st["color"]}">'
                       f'{html.escape(text)}</text>')

    out.append("</svg>")
    return "\n".join(out)


FENCE_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.S)


def from_markdown(md: str) -> str | None:
    """Markdown 里第一个能画出来的 mermaid 块 → SVG。"""
    for m in FENCE_RE.finditer(md or ""):
        svg = to_svg(m.group(1))
        if svg:
            return svg
    return None
