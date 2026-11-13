# 业务前端 UI 规范（S1 定方案、S2 落地、S3 核）

管的是**业务项目**里你让 agent 写的前端页面，不是本技能自己的看板。
目标很具体：别产出「功能对、但一看就是 AI 拼的」界面。

`devloop ui` 能静态查的只是下限（见文末）；上面这些它查不出来，得写进 S1、做到 S2。

---

## 一、先有底座，再画页面

页面难看的根本原因，通常是**没有决策**：字号随手写、间距看着给、颜色每次现挑。
所以 S1 的「界面方案」必须先定五件事，缺一件后面就会花：

| 要定什么 | 具体到 | 反例 |
|---|---|---|
| 色板 | 背景 / 表面 / 正文 / 次要 / 边框 / 主色 / 成功·警告·危险，各一个值 | 「主色用蓝」 |
| 字号 | 3 级：页面标题 / 区块标题 / 正文，每级给 px 和字重 | 全站 14px |
| 间距 | 只取 4 的倍数（4/8/12/16/24/32/48），不出现 13、17、21 | `margin:15px` |
| 圆角 | 2–3 档（小控件 / 卡片 / 胶囊），不混用 4、8、12、16 | 每个组件不一样 |
| 主操作 | 这一屏只允许**一个**主按钮，其余 ghost/文字按钮 | 三个蓝色主按钮并排 |

落地方式：全部写进 `:root` 的 token，组件里只写 `var(--x)`。这样换主题、加暗色
都只改一个地方。

## 二、版面要有主次

同一屏里，用户的眼睛应该知道先看哪。三个可执行的判断：

0. **一屏只做一件事**。用户进来要完成什么，就让它成为屏幕上最大、最显眼的那块；
   其余都是辅助。一屏想干三件事，结果一件都不突出。
1. **一块主区 + 若干次区**。主区给最大的那张图/那组数字，次区用紧凑列表，
   不要「8 个一样大的卡片」——那就是没有主次。
2. **字号拉开，不要只靠加粗**。页面标题与正文至少差 8px，区块标题差 4px。
3. **留白成组**。相关的东西挨近（8–12），不相关的拉开（24–32）；均匀铺开等于没分组。

## 三、状态四件套，缺一不可

只要页面要取数，就必须同时想清楚四条路，只写成功那条是最常见的「看着能跑、一用就空」：

| 状态 | 要求 | 别这么做 |
|---|---|---|
| 加载 | 骨架屏或占位块，保持版面不跳 | 全屏转圈，加载完布局抖一下 |
| 空 | 说明为什么空 + 下一步按钮（新建/去设置/清除筛选） | 只写「暂无数据」 |
| 错误 | 说清哪一步失败 + 重试按钮，保留用户已填内容 | 弹一个 `alert`，或白屏 |
| 成功 | 就地反馈（按钮态、行内提示、toast 其一） | 什么都不发生，用户不知道成没成 |

表单另加三条：label 与输入框关联（`for`/`id`）、错误提示贴在字段下方、必填有标记。

## 四、响应式与可访问性

- 320px 起可用；窄屏表单单列、表格与代码各自横向滚动，**整页不许横滚**。
- 触控目标 ≥ 40×40px，相邻可点元素之间留 8px。
- 焦点必须可见：`:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }`。
- 对比度：正文 ≥ 4.5:1，大字号 ≥ 3:1；别用 `#999` 写正文。
- 图片有 `alt`；纯图标按钮有 `aria-label`；不要用 `div` 冒充按钮。

## 五、动效

- 只动 `transform`、`opacity`、颜色；不动 `width/height/top/left`。
- 150–250ms，`ease-out` 入场、`ease-in` 出场；超过 400ms 会显得卡。
- 必须能被 `prefers-reduced-motion: reduce` 关掉。
- 关键操作不做「等动画放完才生效」。

## 六、禁用清单（命中就是返工）

| 禁用 | 为什么 |
|---|---|
| 紫蓝渐变（`#7c3aed → #2563eb` 这类） | 「AI 生成」最强信号，且和多数品牌色打架 |
| 卡片套卡片 | 三层圆角边框叠在一起，层次全乱 |
| emoji 当图标 | 各系统渲染不同，尺寸对齐不可控 |
| 纯装饰的状态小圆点 | 不携带信息，只增加噪声 |
| 编造的指标、评价、客户 logo | 一眼假，比空着更糟 |
| 全站同一个圆角 / 同一个字号 | 没有层级 |
| `!important` 堆叠 | 说明选择器已经失控 |

## 七、常见丑法 → 改法

| 看起来 | 改成 |
|---|---|
| 满屏一样大的白卡片 | 主区一块大的，次区一行行紧凑列表 |
| 5 个统计卡数字一样大 | 一个主指标 28–32px，其余 14px 并排小字 |
| 表格四边全框 | 只留横线分隔 + 浅底表头 |
| 每个按钮都是主色 | 一屏一个主按钮，其余描边或纯文字 |
| 标题只加粗不放大 | 字号拉开 + 字重 600/650 |
| 间距 13/17/21 混着来 | 统一到 4 的倍数 |
| 空态「暂无数据」 | 一句话解释 + 一个下一步按钮 |
| 灰字看不清 | 次要文字用 `--ink-3`（≥4.5:1），别用 `#9ca3af` |

## 八、`devloop ui` 能查什么

```bash
python scripts/devloop.py ui --root . --files web/src/Refund.vue,web/src/refund.css
python scripts/devloop.py ui --root .            # 默认查本次改动的文件
```

它按文件扫，报 P0/P1/P2，P0 返回非零：

| 规则 | 级别 | 判据 |
|---|---|---|
| UI-01 `transition: all` | P0 | 布局属性也被动画 |
| UI-02 紫蓝渐变 | P0 | 渐变里出现紫/靛色 |
| UI-03 图片缺 alt | P0 | `<img>` 没有 alt |
| UI-04 `100vw` | P1 | 撑出横向滚动 |
| UI-05 动画布局属性 | P1 | transition/animation 里出现 width 等 |
| UI-06 `div` 当按钮 | P1 | `<div onClick>` |
| UI-07 emoji 当图标 | P1 | 标签里只有 emoji |
| UI-08 内联 style 过多 | P2 | 样式散在标签上 |
| UI-09 裸色值过多 | P1 | `:root` 外 > 12 个色值 |
| UI-10 缺焦点态 | P1 | 有 hover/按钮样式但无 `:focus-visible` |
| UI-11 动效不可关 | P2 | 有动效但无 `prefers-reduced-motion` |
| UI-12 请求无状态处理 | P1 | 有 fetch/axios 但 loading/error 不全 |
| UI-13 没有设计 token | P2 | 有颜色但没有 token 定义 |
| UI-14 间距没有节奏 | P2 | 间距不在 4 的倍数上（13/17/21 这类） |
| UI-15 字号阶梯混乱 | P2 | 字号 ≥7 种，或 ≥3 个组件类却只有 1 种 |
| UI-16 一屏多个主按钮 | P1 | 主按钮类名出现 >2 次 |
| UI-17 卡片套卡片 | P1 | `class="card"` 里又套了一个 `card` |
| UI-18 表格四边框 | P2 | `table` 与 `th,td` 都设了 1px 边框 |

**它查不出「好不好看」**。版面主次、留白节奏、文案语气，仍然要人看——S2 的
「界面自查」就是留这一眼的：贴 `ui` 结论 + 宽窄两张截图，说明你实际看到了什么。

## 九、起步模板（可直接改）

不确定从哪下手时，照这个结构起，再把内容换掉。它已经满足上面八条规则。

```vue
<template>
  <main class="page">
    <header class="head">
      <h1>订单退款</h1>
      <p class="sub">订单 #{{ id }} · 已付 {{ paid }}</p>
    </header>

    <section v-if="loading" class="card" aria-busy="true">正在加载…</section>
    <section v-else-if="error" class="card">
      <p class="err">拉取失败：{{ error }}</p>
      <button class="btn ghost" @click="retry">重试</button>
    </section>
    <section v-else-if="!data" class="card">
      <p>这笔订单没有可退金额。</p>
      <button class="btn ghost" @click="back">返回订单</button>
    </section>
    <section v-else class="card">
      <label class="label" for="amount">退款金额</label>
      <input id="amount" v-model="amount" class="input" inputmode="decimal" />
      <p v-if="amountError" class="err">{{ amountError }}</p>
      <div class="actions">
        <button class="btn primary" :disabled="!valid" @click="submit">提交退款</button>
        <button class="btn ghost" @click="back">取消</button>
      </div>
    </section>
  </main>
</template>

<style>
:root{
  --bg:#f7f8fa; --surface:#fff; --ink:#15181d; --ink-2:#3f4653; --ink-3:#5b6371;
  --line:#e2e5ea; --accent:#1d4ed8; --danger:#9f1b12;
  --sp-1:4px; --sp-2:8px; --sp-3:16px; --sp-4:24px; --r-2:10px
}
.page{max-width:640px;margin:0 auto;padding:var(--sp-4) var(--sp-3);background:var(--bg)}
.head h1{font-size:22px;line-height:1.3;margin:0 0 4px}
.sub{color:var(--ink-3);font-size:13px;margin:0 0 var(--sp-3)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-2);
  padding:var(--sp-3)}
.label{display:block;font-size:13px;color:var(--ink-2);margin-bottom:4px}
.input{width:100%;padding:10px 12px;font-size:14px;border:1px solid var(--line);border-radius:8px}
.input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.err{color:var(--danger);font-size:13px;margin:6px 0 0}
.actions{display:flex;gap:var(--sp-2);margin-top:var(--sp-3)}
.btn{padding:9px 16px;font-size:14px;border-radius:8px;border:1px solid transparent;
  cursor:pointer;transition:background-color .15s,border-color .15s,color .15s}
.btn.primary{background:var(--accent);color:#fff}
.btn.ghost{background:var(--surface);border-color:var(--line);color:var(--ink-2)}
.btn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.btn:disabled{opacity:.5;cursor:not-allowed}
@media (prefers-reduced-motion:reduce){*{transition:none;animation:none}}
</style>
```

三件事它已经替你做了：状态四件套齐全、只有一个主按钮、token 与焦点态就位。
换成你的字段时，**别把间距改回 13/17/21**，也别加第二个主按钮。

## 十、参考

本地技能库里两套可借：

- `~/.workbuddy/skills/hallmark`：从零设计新页面时整套用它（问素材/受众/用途/语气 →
  抽主题 → `sloplint` 自查）。营销页、落地页、品牌页优先。
- `~/.workbuddy/skills/frontend-ui-engineering`：组件结构、状态管理、设计系统遵循、
  WCAG 清单。中后台、工具型界面优先。
