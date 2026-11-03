# dev-loop · 研发闭环

> 给 AI 编程助手装一条流水线：需求影响面 → 变更审计 → 文档同步 → 健康检查 → 缺陷定位。
> 五个阶段串成一个闭环，不用在多个技能之间来回切。

[English](README.en.md) · [安装](#安装) · [用法](#用法) · [产出](#产出长什么样) · [已知限制](#已知限制)

---

## 它解决什么

真实项目里的问题很少是"写不出代码"，而是这几类：

| 痛点 | 表现 | 本技能怎么治 |
|---|---|---|
| 需求来了不知道影响面 | 改了三处，漏了第七处，上线才发现 | S1 先出改造清单，作为验收标准 |
| 复测不全面 | 只测了改动的接口，没测调用方 | S1 给回归范围，S2 给待补测试 |
| 改了代码没改文档 | 文档描述的还是半年前的接口 | S3 扫描文档里的路径/符号/接口是否还存在 |
| 代码健康度靠肉眼 | 隐患堆到出事才有人看 | S4 按五个维度分级输出，定位到行 |
| 报错只能靠猜 | 改了三版才碰对 | S5 强制先建证据链，再谈修复 |

## 为什么不是"几个 prompt 拼起来"

关键区别在于**阶段之间的因果**，而不是把五个提示词塞进一个文件：

```
需求 ──► S1 影响面 ──► 改造清单（= S2 的验收标准）
                          │
                          ├──► 编码 ──► S2 变更审计 ──► 漏改 / 越界 / 待补测试
                          │                                │
                          └──► 回归范围                     ▼
                                                    S3 文档同步（用 S2 的 diff 定位文档）
                                                           │
                                        S4 健康检查 ◄──────┤
                                                           │
                                        S5 缺陷定位 ◄──────┘
                                              └── 修完必须回到 S2 重新审计
```

没有 S1 的清单，S2 就无法判断"改全了没有"——这是最常被跳过、也最致命的一环。
S5 修完不重跑 S2，就会漏掉修复本身引入的新影响面。

## 安装

技能遵循通用的 `SKILL.md` 约定，放到客户端的 skills 目录即可。

**方式一：直接克隆（推荐，便于更新）**

```bash
git clone https://github.com/BHLO/dev_loop.git ~/.workbuddy/skills/dev-loop
```

**方式二：项目内共享**（团队用同一版，随代码库分发）

```bash
mkdir -p .workbuddy/skills
git clone https://github.com/BHLO/dev_loop.git .workbuddy/skills/dev-loop
```

**方式三：下载 zip 解压**

```bash
unzip dev-loop.zip -d ~/.workbuddy/skills/
```

> 目录名必须是 `dev-loop`，需与 `SKILL.md` 里的 `name` 字段一致。
> 安装后**重启客户端**，技能才会出现在列表里。

**环境要求**：Python 3.8+，无需安装任何第三方包。Git 可选——有 git 能分析提交差异，没有则用 `--files` 指定文件。

## 用法

不用记命令，直接说人话：

```
「订单要支持部分退款，帮我评估影响面」
  → 影响面矩阵 + 改造清单 + 回归范围 + 待确认项

「改完了，看下有没有改漏」
  → 漏改清单(P0) + 越界改动 + 调用方判定表 + 待补测试

「文档要同步一下」
  → 逐文档判定（必须改/建议改/不用改）+ 段落级补丁

「检查下这个模块的代码健康度」
  → P0/P1/P2 分级问题，每条带 文件:行 与修复建议

「线上报错：AttributeError: 'NoneType' object has no attribute 'name'」
  → 现象结构化 → 栈帧映射 → 根因分类 → 证据链 → 最小修复 → 回归项
```

也可以直接调脚本：

```bash
python scripts/devloop.py scan   --root .
python scripts/devloop.py impact --root . --base HEAD~1
python scripts/devloop.py impact --root . --files src/api/user.py   # 无 git 也能用
python scripts/devloop.py graph  --root . --files src/api/user.py,src/service/order.py
python scripts/devloop.py drift  --root . --changed src/api/user.py
python scripts/devloop.py state  --root . --show
```

## 产出长什么样

所有分析结果落在项目根目录 `.devloop/`，不污染源码目录：

```
.devloop/
├── profile.json      项目画像（语言/构建命令/文档清单/入口点）
├── state.json        当前阶段、历史、未闭环项（支持中断续做）
├── impact.md/json    变更影响面（调用方/测试/文档/风险分）
├── drift.md/json     文档漂移（失效的路径、符号、接口）
├── S1-impact.md      需求影响面 + 改造清单 + 回归范围
├── S2-audit.md       漏改/越界/调用方判定/待补测试
├── S3-docs.md        文档逐段判定与补丁
├── S4-health.md      分级问题清单
└── S5-fix.md         现象→根因→修复→验证
```

建议加进 `.gitignore`。工具会自动忽略该目录，不会把自己的产物算成变更。

一份真实的 `impact.md` 片段：

```markdown
## src/api/user.py  ·  M  ·  风险 36
风险来源：被 2 个文件引用 (+6)；触碰对外接口层 (+20)；被 1 个文档描述 (+10)
改动：+3 / -0，1 个 hunk
涉及符号：`list_users`, `User`, `get_user`, `delete_user`, `/api/users`
**调用方（1）**
- `src/service/order.py`:1 — get_user
**相关测试（1）**
- `tests/test_user.py`:1 — get_user
**相关文档（1）**
- `docs/api.md`:3 — get_user
```

## 原理

分析引擎是一个 Python 脚本（`scripts/devloop.py`），四件事：

1. **符号提取**：从 diff 的 hunk 上下文和文件顶层定义里取出受影响的函数/类/路由。
2. **引用扫描**：把这些符号编译成一个正则，全仓单次扫描，分类成「调用方 / 测试 / 文档」。600 文件规模约 1.5 秒。
3. **风险评分**：按被引用数、是否触碰接口层/数据层/配置、有无测试、改动量、文档引用，加总成 0–100。
4. **文档漂移**：提取文档里的行内代码与代码块，判断其中的路径是否存在、符号是否还在代码里。

判断类的工作（语义影响、根因、修复方案）由 `SKILL.md` 的规程和 `references/` 的检查清单驱动，不靠脚本猜。

## 已知限制

诚实说明，避免误用：

- **符号提取基于正则，不是 AST**。跨语言、生成代码、极端写法可能漏掉，重要改动仍建议人工过一遍调用链。
- **识别不到动态引用**：反射、DI 容器、字符串拼装的调用、配置驱动的路由，脚本看不见。
- **只分析单仓库**。跨服务的影响面需要人工补充。
- **风险分是启发式**，用于排序注意力，不是质量结论。
- **不会自动改代码**。S1–S4 只读，改动需要你明确要求。

## 贡献

欢迎补充 `references/impact-rules.md` 里你所在技术栈的检查项——这是最容易产生价值的地方。
提 PR 前请自己跑一遍 `scan` / `impact` / `gate` 确认脚本仍可用。

## License

MIT
