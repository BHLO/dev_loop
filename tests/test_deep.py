#!/usr/bin/env python3
"""深度测试：规则边界、异常输入、端到端闭环。

和 test_devloop.py 的分工：
- test_devloop.py：命令能不能用（黑盒）
- 本文件：每条规则的边界条件、退化路径、以及一次完整闭环（白盒 + 集成）

跑法：
    python tests/test_deep.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import devloop  # noqa: E402

SCRIPT = SCRIPTS / "devloop.py"


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, encoding="utf-8", errors="replace",
        cwd=str(cwd) if cwd else None,
    )


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)


def make_repo(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="devloop-deep-"))
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    if shutil.which("git"):
        git(root, "init", "-q", ".")
        git(root, "add", "-A")
        git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return root


S1_REPORT = """# S1 需求影响面与技术方案

## 一、需求理解与验收标准
部分退款：可退金额 ≤ 订单金额 - 已退金额。

## 二、技术路线
| 路线 | 做法 | 代价 | 结论 |
|---|---|---|---|
| A 接口层校验 | 改动小 | 多入口不一致 | 备选 |
| B 收敛到领域模型 | 加字段与迁移 | 口径唯一 | 采纳 |

## 三、实现流程
1. 迁移 → 2. 模型加校验 → 3. 接口透传 → 4. 状态机 → 5. 对账

## 四、架构图
```mermaid
flowchart TB
  R["/api/refund"] --> O[Order]
  O --> T[(orders)]
```

## 五、改造清单
| 编号 | 文件:行 | 动作 | 改造理由 | 依据 |
|---|---|---|---|---|
| C1 | src/api/refund.py:1 | 改 | 入参写死全额 | refund.py:1 |
"""

CHECKLIST_ONLY = """# S1 影响面

## 改造清单
| C1 | src/api/refund.py | 改 | 加参数 |
"""


def write_s1_report(root: Path, text: str = S1_REPORT) -> None:
    d = root / ".devloop"
    d.mkdir(exist_ok=True)
    (d / "S1-impact.md").write_text(text, encoding="utf-8")


def write_report(root: Path, name: str, text: str) -> None:
    d = root / ".devloop"
    d.mkdir(exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


BASE_FILES = {
    "src/api/refund.py": "def create_refund(order_id):\n    return {'order_id': order_id}\n",
    "src/service/order.py": "from src.api.refund import create_refund\n\ndef refund(o):\n    return create_refund(o)\n",
    "src/common/money.py": "def to_yuan(fen):\n    return fen / 100\n",
    "tests/test_refund.py": "from src.api.refund import create_refund\n\ndef test_x():\n    assert create_refund(1)\n",
    "docs/api/order.md": "# 订单\n\n调用 `create_refund`，实现见 `src/api/refund.py`。\n\n接口：`/api/refund`\n",
}


# ============================================================ 敏感规则单一事实源

class SensitiveRules(unittest.TestCase):
    def setUp(self) -> None:
        devloop._RULES_CACHE = None
        self._orig = devloop.rules_source_path

    def tearDown(self) -> None:
        devloop.rules_source_path = self._orig
        devloop._RULES_CACHE = None

    def test_rules_loaded_from_reference_md(self) -> None:
        rules = devloop.load_sensitive_rules()
        self.assertGreaterEqual(len(rules), 10, "规则条数偏少，可能没读到 md")
        for level, label, pat in rules:
            self.assertIn(level, ("高危", "中危"), f"级别写错：{level}")
            self.assertTrue(label, "规则缺 label")
            self.assertIsNotNone(devloop.re.compile(pat), f"正则无法编译：{pat}")

    def test_rules_md_is_single_source(self) -> None:
        md = (ROOT / "references" / "sensitive-files.md").read_text(encoding="utf-8")
        self.assertIn("唯一的事实源", md, "md 里要写明它是唯一事实源")
        self.assertIn("```yaml", md, "缺机器可读规则块")
        # 规则块条数应与内置默认一致，防止两边漂移
        md_rules = devloop.load_sensitive_rules()
        self.assertEqual(len(md_rules), len(devloop.DEFAULT_SENSITIVE_RULES),
                         "md 规则数与脚本内置默认不一致，说明已经漂移")

    def test_custom_rules_take_effect(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="rules-"))
        custom = tmp / "sensitive-files.md"
        custom.write_text(
            "```yaml\nrules:\n"
            "  - level: 高危\n    label: 自定规则甲\n    pattern: '^custom/'\n"
            "  - level: 高危\n    label: 自定规则乙\n    pattern: '\\\\.secret$'\n"
            "  - level: 高危\n    label: 自定规则丙\n    pattern: '(^|/)aaa/'\n"
            "  - level: 高危\n    label: 自定规则丁\n    pattern: '(^|/)bbb/'\n"
            "  - level: 中危\n    label: 自定规则戊\n    pattern: '(^|/)ccc/'\n"
            "```\n", encoding="utf-8")
        devloop.rules_source_path = lambda: custom
        devloop._RULES_CACHE = None

        rules = devloop.load_sensitive_rules()
        self.assertEqual(len(rules), 5)
        self.assertIn("自定规则甲", [r[1] for r in rules])
        hits = devloop.classify_sensitive(["custom/x.py", "ok.py"])
        self.assertEqual(hits[0]["label"], "自定规则甲")
        self.assertEqual(len(hits), 1)
        shutil.rmtree(tmp, ignore_errors=True)

    def test_broken_block_falls_back(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="rules-bad-"))
        bad = tmp / "sensitive-files.md"
        bad.write_text("```yaml\nrules:\n  - level: 高危\n    label: X\n    pattern: '(['\n```\n",
                       encoding="utf-8")
        devloop.rules_source_path = lambda: bad
        devloop._RULES_CACHE = None
        rules = devloop.load_sensitive_rules()
        self.assertEqual(rules, devloop.DEFAULT_SENSITIVE_RULES, "解析失败应退回默认规则")
        shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_file_falls_back(self) -> None:
        devloop.rules_source_path = lambda: Path("Z:/not/exist/sensitive-files.md")
        devloop._RULES_CACHE = None
        self.assertEqual(devloop.load_sensitive_rules(), devloop.DEFAULT_SENSITIVE_RULES)


# ============================================================ 动作核对

class AuditActions(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_repo(BASE_FILES)
        run("state", "--root", str(self.root), "--set", "S1")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _audit(self) -> tuple[int, str]:
        proc = run("audit", "--root", str(self.root), "--base", "HEAD")
        md = (self.root / ".devloop" / "audit-code.md").read_text(encoding="utf-8")
        return proc.returncode, md

    def test_plan_modify_but_deleted(self) -> None:
        run("state", "--root", str(self.root), "--add", "C1 | src/common/money.py | 改 | 调精度")
        (self.root / "src" / "common" / "money.py").unlink()
        code, md = self._audit()
        self.assertEqual(code, 1)
        self.assertIn("动作不符", md)
        self.assertIn("计划「改」，实际「删除」", md)

    def test_plan_add_but_modified(self) -> None:
        run("state", "--root", str(self.root), "--add", "C1 | src/api/refund.py | 增 | 新方法")
        (self.root / "src" / "api" / "refund.py").write_text(
            "def create_refund(order_id):\n    return 1\n\ndef extra():\n    return 2\n",
            encoding="utf-8")
        code, md = self._audit()
        self.assertEqual(code, 1)
        self.assertIn("计划「增」，实际「改」", md)

    def test_plan_delete_and_deleted_is_ok(self) -> None:
        run("state", "--root", str(self.root), "--add", "C1 | src/common/money.py | 删 | 不再使用")
        (self.root / "src" / "common" / "money.py").unlink()
        code, md = self._audit()
        self.assertEqual(code, 0, md)
        self.assertIn("无", md.split("## 动作不符")[1].split("## 一致")[0])

    def test_plan_add_new_file_is_ok(self) -> None:
        run("state", "--root", str(self.root), "--add", "C1 | migrations/0001.sql | 增 | 加字段")
        (self.root / "migrations").mkdir()
        (self.root / "migrations" / "0001.sql").write_text("ALTER TABLE t ADD c INT;\n",
                                                           encoding="utf-8")
        code, _ = self._audit()
        self.assertEqual(code, 0)

    def test_items_without_path_are_ignored(self) -> None:
        run("state", "--root", str(self.root), "--add", "补一段说明，没有具体文件")
        code, md = self._audit()
        self.assertEqual(code, 0)
        self.assertIn("计划文件：0", md)


# ============================================================ 文档基线

class Baseline(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_repo(BASE_FILES)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_write_then_intact(self) -> None:
        proc = run("baseline", "--root", str(self.root), "--files", "docs/api/order.md")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads((self.root / ".devloop" / "doc-baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["docs"]), 1)
        anchors = data["docs"][0]["anchors"]
        self.assertIn("create_refund", anchors)
        self.assertIn("src/api/refund.py", anchors)
        self.assertIn("/api/refund", anchors)

        proc = run("baseline", "--root", str(self.root), "--check")
        self.assertEqual(proc.returncode, 0)

    def test_anchor_lost_detected(self) -> None:
        run("baseline", "--root", str(self.root), "--files", "docs/api/order.md")
        (self.root / "docs" / "api" / "order.md").write_text("# 订单\n\n改了写法。\n",
                                                             encoding="utf-8")
        proc = run("baseline", "--root", str(self.root), "--check")
        self.assertEqual(proc.returncode, 1)
        md = (self.root / ".devloop" / "baseline-check.md").read_text(encoding="utf-8")
        self.assertIn("锚点丢失", md)

    def test_doc_deleted_detected(self) -> None:
        run("baseline", "--root", str(self.root), "--files", "docs/api/order.md")
        (self.root / "docs" / "api" / "order.md").unlink()
        proc = run("baseline", "--root", str(self.root), "--check")
        self.assertEqual(proc.returncode, 1)
        md = (self.root / ".devloop" / "baseline-check.md").read_text(encoding="utf-8")
        self.assertIn("文档已不存在", md)

    def test_content_changed_is_not_failure(self) -> None:
        run("baseline", "--root", str(self.root), "--files", "docs/api/order.md")
        p = self.root / "docs" / "api" / "order.md"
        p.write_text(p.read_text(encoding="utf-8") + "\n补充一句说明，锚点不变。\n", encoding="utf-8")
        proc = run("baseline", "--root", str(self.root), "--check")
        self.assertEqual(proc.returncode, 0)
        md = (self.root / ".devloop" / "baseline-check.md").read_text(encoding="utf-8")
        self.assertIn("内容已变", md)

    def test_check_without_baseline(self) -> None:
        proc = run("baseline", "--root", str(self.root), "--check")
        self.assertEqual(proc.returncode, 2)

    def test_inline_single_char_does_not_break_pairing(self) -> None:
        """`a` 这种单字符行内代码不能把后面整行的反引号配对搞乱。"""
        text = "调用 `a`，实现见 `src/api/refund.py`，接口 `x`。"
        spans = devloop.CODE_SPAN_RE.findall(text)
        self.assertEqual(spans, ["a", "src/api/refund.py", "x"])


# ============================================================ 自测清单的 diff 特征

class SelfTestFeatures(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_repo(BASE_FILES)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _verify(self, files: str) -> str:
        proc = run("verify", "--root", str(self.root), "--cmd",
                   f'"{sys.executable}" -c "print(1)"', "--files", files, "--base", "HEAD")
        self.assertIn(proc.returncode, (0, 1, 2))
        return (self.root / ".devloop" / "verify.md").read_text(encoding="utf-8")

    def test_signature_change_detected(self) -> None:
        (self.root / "src" / "api" / "refund.py").write_text(
            "def create_refund(order_id, amount):\n    return amount\n", encoding="utf-8")
        md = self._verify("src/api/refund.py")
        self.assertIn("签名变化", md)

    def test_branch_added_detected(self) -> None:
        (self.root / "src" / "service" / "order.py").write_text(
            "def refund(o):\n    if o:\n        return 1\n    return 0\n", encoding="utf-8")
        md = self._verify("src/service/order.py")
        self.assertIn("分支条件", md)

    def test_sql_change_detected(self) -> None:
        (self.root / "migrations").mkdir()
        (self.root / "migrations" / "0002.sql").write_text("ALTER TABLE t ADD c INT;\n",
                                                           encoding="utf-8")
        md = self._verify("migrations/0002.sql")
        self.assertIn("表结构/字段", md)

    def test_checklist_is_capped_and_deduped(self) -> None:
        for name in ("src/api/refund.py", "src/service/order.py"):
            (self.root / name).write_text("def x(a):\n    if a:\n        return 1\n    return 0\n",
                                          encoding="utf-8")
        md = self._verify("src/api/refund.py,src/service/order.py")
        rows = [l for l in md.splitlines() if l.startswith("| □")]
        self.assertLessEqual(len(rows), 10, "自测清单不能无限长")
        self.assertEqual(len(rows), len(set(rows)), "自测清单有重复行")


# ============================================================ 图与格式

class GraphFormats(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_repo(BASE_FILES)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _md(self) -> str:
        return (self.root / ".devloop" / "graph.md").read_text(encoding="utf-8")

    def test_ascii_only(self) -> None:
        run("graph", "--root", str(self.root), "--files", "src/api/refund.py",
            "--format", "ascii")
        md = self._md()
        self.assertIn("## 终端视图", md)
        self.assertNotIn("```mermaid", md)

    def test_mermaid_only(self) -> None:
        run("graph", "--root", str(self.root), "--files", "src/api/refund.py",
            "--format", "mermaid")
        md = self._md()
        self.assertNotIn("## 终端视图", md)
        self.assertIn("```mermaid", md)

    def test_ascii_shows_dependents(self) -> None:
        run("graph", "--root", str(self.root), "--files", "src/api/refund.py",
            "--format", "ascii")
        md = self._md()
        block = md.split("## 终端视图")[1].split("```")[1]
        self.assertIn("src/service/order.py", block)
        self.assertIn("(L1", block)

    def test_missing_file_marked_in_ascii(self) -> None:
        run("graph", "--root", str(self.root),
            "--files", "src/api/refund.py,migrations/9999.sql", "--format", "ascii")
        md = self._md()
        self.assertIn("[待新建]", md)


# ============================================================ 阶段闸门

class StageGate(unittest.TestCase):
    """每个阶段的产出都要用户过目；S1 还必须先有技术方案，不能只给清单。"""

    def setUp(self) -> None:
        self.root = make_repo({"a.py": "a\n"})

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _gates(self) -> dict:
        f = self.root / ".devloop" / "gates.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

    # ---------------------------------------------------------- 拦截

    def test_cannot_enter_s2_without_record(self) -> None:
        proc = run("state", "--root", str(self.root), "--set", "S2")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("还没登记过", proc.stdout)

    def test_checklist_without_plan_is_refused(self) -> None:
        """S1 只给清单不给方案，不许登记——这是本轮改动的核心。"""
        write_s1_report(self.root, CHECKLIST_ONLY)
        proc = run("gate", "--root", str(self.root), "--stage", "S1",
                   "--files", "a.py", "--doc", ".devloop/S1-impact.md")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("缺少必需内容", proc.stdout)
        for part in ("技术路线", "实现流程", "架构图"):
            self.assertIn(part, proc.stdout)

    def test_missing_report_is_refused(self) -> None:
        proc = run("gate", "--root", str(self.root), "--stage", "S1", "--files", "a.py")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("找不到阶段报告", proc.stdout)

    def test_registered_but_unapproved_blocks_next_stage(self) -> None:
        write_s1_report(self.root)
        run("gate", "--root", str(self.root), "--stage", "S1",
            "--files", "a.py", "--doc", ".devloop/S1-impact.md")
        proc = run("state", "--root", str(self.root), "--set", "S2")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("还没给用户确认过", proc.stdout)

    def test_approve_requires_user_quote(self) -> None:
        write_s1_report(self.root)
        run("gate", "--root", str(self.root), "--stage", "S1",
            "--files", "a.py", "--doc", ".devloop/S1-impact.md")
        proc = run("gate", "--root", str(self.root), "--stage", "S1", "--approve")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("必须带 --note", proc.stdout)

    # ---------------------------------------------------------- 放行

    def test_approve_then_enter_s2(self) -> None:
        write_s1_report(self.root)
        run("gate", "--root", str(self.root), "--stage", "S1",
            "--files", "a.py", "--doc", ".devloop/S1-impact.md", "--summary", "需求")
        proc = run("gate", "--root", str(self.root), "--stage", "S1",
                   "--approve", "--note", "可以，按 B 方案来")
        self.assertEqual(proc.returncode, 0, proc.stdout)

        g = self._gates()["S1"]
        self.assertEqual(g["status"], "approved")
        self.assertEqual(g["note"], "可以，按 B 方案来")
        self.assertEqual(g["doc"], ".devloop/S1-impact.md")

        self.assertEqual(run("state", "--root", str(self.root), "--set", "S2").returncode, 0)

    def test_report_change_invalidates_approval(self) -> None:
        write_s1_report(self.root)
        run("gate", "--root", str(self.root), "--stage", "S1",
            "--files", "a.py", "--doc", ".devloop/S1-impact.md")
        # 用户看的是旧版，agent 偷偷改了报告再确认
        write_s1_report(self.root, S1_REPORT + "\n## 六、补充\n偷偷加一段\n")
        proc = run("gate", "--root", str(self.root), "--stage", "S1",
                   "--approve", "--note", "可以")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("已过期", proc.stdout)

    def test_skip_needs_reason_and_unblocks(self) -> None:
        """阶段不适用：不用先登记，但必须给理由。"""
        proc = run("gate", "--root", str(self.root), "--stage", "S3", "--skip")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("必须带 --note", proc.stdout)

        proc = run("gate", "--root", str(self.root), "--stage", "S3",
                   "--skip", "--note", "本次只改代码，无文档变更")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self._gates()["S3"]["status"], "skipped")

    def test_every_stage_is_gated(self) -> None:
        """不是只有 S1：S2 没确认也进不了 S3。"""
        write_s1_report(self.root)
        run("gate", "--root", str(self.root), "--stage", "S1",
            "--files", "a.py", "--doc", ".devloop/S1-impact.md")
        run("gate", "--root", str(self.root), "--stage", "S1", "--approve", "--note", "可以")
        run("state", "--root", str(self.root), "--set", "S2")

        proc = run("state", "--root", str(self.root), "--set", "S3")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("S2", proc.stdout)

        write_report(self.root, "S2-audit.md", "# S2\n## 漏改\n无\n## 越界\n无\n")
        run("gate", "--root", str(self.root), "--stage", "S2",
            "--files", "a.py", "--doc", ".devloop/S2-audit.md")
        self.assertEqual(run("state", "--root", str(self.root), "--set", "S3").returncode, 3,
                         "登记但未确认也不能进")
        run("gate", "--root", str(self.root), "--stage", "S2", "--approve", "--note", "可以")
        self.assertEqual(run("state", "--root", str(self.root), "--set", "S3").returncode, 0)

    def test_show_lists_all_stages(self) -> None:
        write_s1_report(self.root)
        run("gate", "--root", str(self.root), "--stage", "S1",
            "--files", "a.py", "--doc", ".devloop/S1-impact.md")
        proc = run("gate", "--root", str(self.root), "--show")
        self.assertEqual(proc.returncode, 0)
        for st in ("S1", "S2", "S3", "S4", "S5", "S6"):
            self.assertIn(st, proc.stdout)
        self.assertIn("待确认", proc.stdout)

    # ---------------------------------------------------------- 轮次

    def test_new_round_archives_reports_and_clears_gates(self) -> None:
        write_s1_report(self.root)
        run("gate", "--root", str(self.root), "--stage", "S1",
            "--files", "a.py", "--doc", ".devloop/S1-impact.md")
        run("gate", "--root", str(self.root), "--stage", "S1", "--approve", "--note", "可以")
        run("state", "--root", str(self.root), "--add", "C1 | a.py | 改 | 旧需求")

        proc = run("state", "--root", str(self.root), "--round", "第二轮需求")
        self.assertEqual(proc.returncode, 0, proc.stdout)

        state = json.loads((self.root / ".devloop" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stage"], "S1")
        self.assertEqual(state["round"], "第二轮需求")
        self.assertEqual(state["open_items"], [])
        self.assertEqual(len(state["rounds"]), 1)

        self.assertFalse((self.root / ".devloop" / "gates.json").exists(), "确认记录必须清空")
        self.assertFalse((self.root / ".devloop" / "S1-impact.md").exists(),
                         "上一轮报告必须归档，否则新一轮会误用")
        archived = list((self.root / ".devloop" / "archive").rglob("S1-impact.md"))
        self.assertEqual(len(archived), 1)

    def test_second_round_needs_fresh_approval(self) -> None:
        write_s1_report(self.root)
        run("gate", "--root", str(self.root), "--stage", "S1",
            "--files", "a.py", "--doc", ".devloop/S1-impact.md")
        run("gate", "--root", str(self.root), "--stage", "S1", "--approve", "--note", "可以")
        run("state", "--root", str(self.root), "--set", "S2")

        run("state", "--root", str(self.root), "--round", "第二轮")
        proc = run("state", "--root", str(self.root), "--set", "S2")
        self.assertEqual(proc.returncode, 3, "新一轮不能沿用上一轮的批准")
        self.assertIn("还没登记过", proc.stdout)


# ============================================================ 提交闸门

class CommitGate(unittest.TestCase):
    """提交必须先出清单、用户过目、确认后才能提交推送。"""

    def setUp(self) -> None:
        self.root = make_repo({"a.py": "a\n"})
        (self.root / "a.py").write_text("a\nb\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _head(self) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.root),
                              capture_output=True, encoding="utf-8").stdout.strip()

    def test_apply_without_plan_is_refused(self) -> None:
        before = self._head()
        proc = run("commit", "--root", str(self.root), "--message", "feat: x", "--apply")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("没有找到本轮的提交清单", proc.stdout)
        self.assertEqual(self._head(), before, "被拒时不许提交")

    def test_message_must_match_plan(self) -> None:
        run("commit", "--root", str(self.root), "--message", "feat: 清单里的信息")
        before = self._head()
        proc = run("commit", "--root", str(self.root), "--message", "feat: 偷改的信息",
                   "--apply")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("提交信息与用户看过的清单不一致", proc.stdout)
        self.assertEqual(self._head(), before)

    def test_content_change_after_plan_is_refused(self) -> None:
        run("commit", "--root", str(self.root), "--message", "feat: 加一行")
        (self.root / "a.py").write_text("a\nb\nc\n", encoding="utf-8")   # 同名不同内容
        before = self._head()
        proc = run("commit", "--root", str(self.root), "--message", "feat: 加一行", "--apply")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("文件内容在清单生成之后又被改过", proc.stdout)
        self.assertEqual(self._head(), before)

    def test_new_file_after_plan_is_refused(self) -> None:
        run("commit", "--root", str(self.root), "--message", "feat: 加一行")
        (self.root / "b.py").write_text("x = 1\n", encoding="utf-8")
        proc = run("commit", "--root", str(self.root), "--message", "feat: 加一行", "--apply")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("文件集合在清单生成之后变了", proc.stdout)

    def test_push_requires_remote(self) -> None:
        run("commit", "--root", str(self.root), "--message", "feat: 加一行")
        before = self._head()
        proc = run("commit", "--root", str(self.root), "--message", "feat: 加一行",
                   "--apply", "--push")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("没有配置远程地址", proc.stdout)
        self.assertEqual(self._head(), before, "无远程时不该留下本地提交")

    def test_confirmed_flow_commits(self) -> None:
        run("commit", "--root", str(self.root), "--message", "feat: 加一行")
        before = self._head()
        proc = run("commit", "--root", str(self.root), "--message", "feat: 加一行", "--apply")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertNotEqual(self._head(), before)

    def test_tag_follows_changelog_when_ahead(self) -> None:
        """CHANGELOG 已经写了更高版本时，标签应当就是那一版，而不是 tag+1。"""
        (self.root / "CHANGELOG.md").write_text(
            "## [1.9.0] - 2026-11-03\n\n- 未发布内容\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "cl")
        git(self.root, "-c", "user.email=t@t", "-c", "user.name=t",
            "tag", "-a", "v1.5.0", "-m", "old")
        (self.root / "a.py").write_text("a\nb\nc\n", encoding="utf-8")   # 相对已提交版本要有变化

        run("commit", "--root", str(self.root), "--message", "feat: 下一版", "--bump", "minor")
        data = json.loads((self.root / ".devloop" / "commit-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(data["suggested_tag"], "v1.9.0",
                         "CHANGELOG 已写到 1.9.0，不该再按 tag+1 得出 v1.6.0")

    def test_plan_file_carries_fingerprint(self) -> None:
        run("commit", "--root", str(self.root), "--message", "feat: 加一行")
        md = (self.root / ".devloop" / "commit-plan.md").read_text(encoding="utf-8")
        self.assertIn("清单指纹", md)
        data = json.loads((self.root / ".devloop" / "commit-plan.json").read_text(encoding="utf-8"))
        for key in ("token", "message", "files", "head", "worktree"):
            self.assertIn(key, data)


# ============================================================ 端到端闭环

class EndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_repo(BASE_FILES)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_full_loop_produces_every_artifact(self) -> None:
        r = str(self.root)
        plan = [
            "C1 | src/api/refund.py | 改 | 入参增加 amount，否则无法部分退款",
            "C2 | src/service/order.py | 改 | 透传 amount，否则下游拿不到",
            "C3 | migrations/0001_refund_amount.sql | 增 | 不建表字段则金额无处存",
            "C4 | docs/api/refund.md | 增 | 新建退款接口文档，否则无据可查",
        ]

        # S1：画像 → 影响面 → 图 → 敏感预检 → 清单入 state
        self.assertEqual(run("scan", "--root", r).returncode, 0)
        self.assertEqual(run("impact", "--root", r, "--files",
                             "src/api/refund.py,src/service/order.py").returncode, 0)
        self.assertEqual(run("graph", "--root", r, "--files",
                             "src/api/refund.py,src/service/order.py,migrations/0001_refund_amount.sql",
                             "--format", "both").returncode, 0)
        sens = run("sensitive", "--root", r, "--files",
                   "src/api/refund.py,src/common/money.py,migrations/0001_refund_amount.sql")
        self.assertEqual(sens.returncode, 3, "清单含迁移文件，应报高危")
        run("state", "--root", r, "--set", "S1")
        for item in plan:
            run("state", "--root", r, "--add", item)
        # 阶段门 S1：先写技术方案，登记，用户同意后才放行
        self.assertEqual(run("state", "--root", r, "--set", "S2").returncode, 3,
                         "没登记不该能进 S2")
        write_s1_report(self.root)
        listed = ("src/api/refund.py,src/service/order.py,"
                  "migrations/0001_refund_amount.sql,docs/api/refund.md")
        proc = run("gate", "--root", r, "--stage", "S1", "--files", listed,
                   "--doc", ".devloop/S1-impact.md", "--summary", "订单支持部分退款")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(run("state", "--root", r, "--set", "S2").returncode, 3,
                         "登记了但没确认，也不能进")
        run("gate", "--root", r, "--stage", "S1", "--approve", "--note", "可以，按 B 方案来")
        self.assertEqual(run("state", "--root", r, "--set", "S2").returncode, 0)

        # 实施：只改代码，文档留到 S3
        (self.root / "src" / "api" / "refund.py").write_text(
            "def create_refund(order_id, amount):\n    return {'amount': amount}\n",
            encoding="utf-8")
        (self.root / "src" / "service" / "order.py").write_text(
            "from src.api.refund import create_refund\n\n"
            "def refund(o, amount):\n    return create_refund(o, amount)\n", encoding="utf-8")
        (self.root / "migrations").mkdir()
        (self.root / "migrations" / "0001_refund_amount.sql").write_text(
            "ALTER TABLE refund ADD amount INT DEFAULT 0;\n", encoding="utf-8")

        # S2：对账只对代码项
        proc = run("audit", "--root", r, "--base", "HEAD")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        code_md = (self.root / ".devloop" / "audit-code.md").read_text(encoding="utf-8")
        self.assertIn("漏改", code_md)
        self.assertNotIn("docs/api/refund.md", code_md.split("## 越界")[0])

        # 阶段门 S2：对账单给用户过目
        self.assertEqual(run("state", "--root", r, "--set", "S3").returncode, 3,
                         "S2 没确认不该能进 S3")
        proc = run("gate", "--root", r, "--stage", "S2",
                   "--files", "src/api/refund.py,src/service/order.py",
                   "--doc", ".devloop/audit-code.md")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        run("gate", "--root", r, "--stage", "S2", "--approve", "--note", "审计没问题，继续")
        self.assertEqual(run("state", "--root", r, "--set", "S3").returncode, 0)

        # 验证门禁
        proc = run("verify", "--root", r, "--cmd", f'"{sys.executable}" -c "print(1)"')
        self.assertEqual(proc.returncode, 0)
        verify_md = (self.root / ".devloop" / "verify.md").read_text(encoding="utf-8")
        self.assertIn("自测清单", verify_md)

        # S3：写文档 → 文档对账 → 记基线
        (self.root / "docs" / "api" / "refund.md").write_text(
            "# 退款\n\n调用 `create_refund`，实现在 `src/api/refund.py`。\n\n接口：`/api/refund`\n",
            encoding="utf-8")
        proc = run("audit", "--root", r, "--base", "HEAD", "--scope", "docs")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        proc = run("baseline", "--root", r, "--files", "docs/api/refund.md")
        self.assertEqual(proc.returncode, 0)
        proc = run("baseline", "--root", r, "--check")
        self.assertEqual(proc.returncode, 0)

        # 阶段门 S3：文档清单给用户过目
        write_report(self.root, "S3-docs.md",
                     "# S3 文档同步\n\n| 文档 | 判定 |\n|---|---|\n"
                     "| docs/api/refund.md | 更新：补 amount 参数 |\n")
        proc = run("gate", "--root", r, "--stage", "S3",
                   "--files", "docs/api/refund.md", "--doc", ".devloop/S3-docs.md")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        run("gate", "--root", r, "--stage", "S3", "--approve", "--note", "文档可以")
        self.assertEqual(run("state", "--root", r, "--set", "S4").returncode, 0)

        # 提交门禁：只出清单，不落库
        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r,
                                     capture_output=True, encoding="utf-8").stdout.strip()
        proc = run("commit", "--root", r, "--message", "feat(refund): 支持部分退款",
                   "--bump", "minor")
        self.assertEqual(proc.returncode, 0)
        plan_md = (self.root / ".devloop" / "commit-plan.md").read_text(encoding="utf-8")
        self.assertIn("高危", plan_md)
        self.assertIn("重大改动", plan_md)
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r,
                                    capture_output=True, encoding="utf-8").stdout.strip()
        self.assertEqual(head_before, head_after, "未确认不应自动提交")

        # 产物齐备
        produced = {p.name for p in (self.root / ".devloop").iterdir()}
        expected = {"profile.json", "impact.md", "graph.md", "graph.mmd", "sensitive.md",
                    "state.json", "audit-code.md", "verify.md", "audit-docs.md",
                    "doc-baseline.json", "baseline-check.md", "commit-plan.md",
                    "gates.json", "S1-gate.md", "S2-gate.md", "S3-gate.md"}
        gates = json.loads((self.root / ".devloop" / "gates.json").read_text(encoding="utf-8"))
        for st in ("S1", "S2", "S3"):
            self.assertEqual(gates[st]["status"], "approved", f"{st} 应已确认")
            self.assertTrue(gates[st]["note"], f"{st} 应记录用户原话")
        missing = expected - produced
        self.assertFalse(missing, f"缺少产物：{missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
