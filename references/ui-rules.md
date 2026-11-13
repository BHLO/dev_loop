# 报告页样式规则（只约束本技能自己生成的页面）

**范围**：`.devloop/看板.html`、`看板-<阶段>.html` 这类由本技能渲染的报告页。
业务项目里你让 agent 写的前端页面，规则在 `references/ui-quality.md`，不要混。

生成的是给人看的页面，不是把 Markdown 倒进 `<div>`。下面八条是硬线，改 `scripts/report.py`
的 `CSS` 时必须全部满足。参考来源：本地技能库的 `hallmark`（反生成感的 Floor）与
`frontend-ui-engineering`（设计系统、WCAG AA）。

---

## 八条硬线

### 1. 颜色、字号、间距只准用 token

`:root` 里定义，正文里只写 `var(--x)`。判定：

```bash
python - <<'EOF'
import re
t = open("scripts/report.py", encoding="utf-8").read()
s = t.index('CSS = """'); e = t.index('"""', s + 10)
bad = [l for l in t[s:e].splitlines()
       if re.search(r"#[0-9a-fA-F]{3,6}\b", l) and "--" not in l.split(":")[0]]
print("token 外的硬编码颜色：", len(bad))
EOF
```

输出必须是 0。新增颜色先加 token（`--xxx`），再引用。

### 2. 每个可交互元素有 `:focus-visible`

`a`、`summary`、按钮、表单控件一律给 2px 实线描边 + 2px 偏移，别只靠 hover。
键盘用户看不到焦点，等于页面不能用。

### 3. 动效可关闭

有 `transition` 就必须有：

```css
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
```

禁止 `transition: all`；禁止动 `width/height/top/left`（只动颜色、边框、背景）。

### 4. 不横向滚动

`html,body{overflow-x:clip}`；网格轨道用 `minmax(0,1fr)`，不用 `1fr`；
标题和表格单元格加 `overflow-wrap:break-word`。长日志、长路径放在自己的滚动容器里
（`pre.code`、`.table-wrap` 各自 `overflow:auto`），不许把整页撑宽。

### 5. 至少三级字号层级

页标题 / 区块标题 / 正文，相邻两级差 ≥4px，字重一起变。

当前：`h1 27px/650` → `h2 19px/650` → 正文 `14.5px/400`；`h3` 是块内标签（14px + 左侧 3px 色条）。

### 6. 禁用的装饰

- 紫蓝渐变（`linear-gradient` 一律不用）
- 卡片套卡片
- emoji 当图标
- 纯装饰的状态小圆点
- `100vw`

### 7. 宽内容独立滚动

表格包 `.table-wrap`、代码包 `pre.code`、mermaid 图包 `figure.diagram`，三者各自滚动，
页面本身不动。

### 8. 对比度

正文 ≥ 4.5:1，12.5px 以下的次要文字 ≥ 4.5:1（小字不能放宽到 3:1）。深色代码块内
文字 ≥ 7:1。

---

## 怎么核

```bash
# 静态：token 外硬编码 / 渐变 / transition:all / 100vw / 100% 宽度
python - <<'EOF'
import re
t = open("scripts/report.py", encoding="utf-8").read()
s = t.index('CSS = """'); e = t.index('"""', s + 10)
css = t[s:e]
for name, pat in [("硬编码颜色", r"#[0-9a-fA-F]{3,6}"),
                  ("渐变", r"gradient\\("),
                  ("transition:all", r"transition:\\s*all"),
                  ("100vw", r"100vw")]:
    hits = [l.strip() for l in css.splitlines()
            if re.search(pat, l) and "--" not in l.split(":")[0]]
    print(f"{name}: {len(hits)}")
EOF
```

几何与对比度用无头浏览器量（Chrome/Edge 都行）：

```bash
chrome --headless=new --disable-gpu --window-size=1280,900 \
  --dump-dom file:///<页面路径>
```

在页面里临时注入一段脚本，输出 `documentElement.scrollWidth > clientWidth`（必须为 false）、
`getComputedStyle` 的字号与颜色比值。改样式后至少量 1280 / 768 两个宽度。

---

## 例外

- `figure.diagram` 里的 SVG 颜色由 `scripts/diagram.py` 的 `classDef` 生成，不归 CSS 管；
  改配色改那边。
- 单阶段视图与整本看板共用同一份 `CSS`，**不要**为某一页单独写样式，否则两页会漂移。
- 深色代码块是刻意的（对照度高、和正文拉开层次），不属于「卡片套卡片」。
