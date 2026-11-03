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
                    "doc-baseline.json", "baseline-check.md", "commit-plan.md"}
        missing = expected - produced
        self.assertFalse(missing, f"缺少产物：{missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
