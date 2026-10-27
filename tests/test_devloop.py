#!/usr/bin/env python3
"""dev-loop 冒烟测试：不依赖第三方库，直接跑脚本验证四类产出。

运行：
    python tests/test_devloop.py
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
SCRIPT = ROOT / "scripts" / "devloop.py"


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, encoding="utf-8", errors="replace",
        cwd=str(cwd) if cwd else None,
    )


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)


class Fixture(unittest.TestCase):
    """建一个带 git 历史的小仓库，模拟真实改动。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="devloop-test-"))
        (self.root / "src" / "api").mkdir(parents=True)
        (self.root / "src" / "service").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "docs").mkdir()

        (self.root / "src" / "api" / "user.py").write_text(
            "def get_user(user_id: int):\n"
            "    return {'id': user_id}\n",
            encoding="utf-8",
        )
        (self.root / "src" / "service" / "order.py").write_text(
            "from src.api.user import get_user\n\n"
            "def build_order(uid):\n"
            "    return get_user(uid)\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_user.py").write_text(
            "from src.api.user import get_user\n\n"
            "def test_get_user():\n"
            "    assert get_user(1)['id'] == 1\n",
            encoding="utf-8",
        )
        (self.root / "docs" / "api.md").write_text(
            "# API\n\n"
            "调用 `get_user`，实现见 `src/api/user.py`。\n\n"
            "旧版在 `src/legacy/old_user.py`。\n",
            encoding="utf-8",
        )

        self.has_git = shutil.which("git") is not None
        if self.has_git:
            git(self.root, "init", "-q", ".")
            git(self.root, "add", "-A")
            git(self.root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------------------------------------------------------------- scan

    def test_scan_writes_profile(self) -> None:
        proc = run("scan", "--root", str(self.root))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        profile = json.loads((self.root / ".devloop" / "profile.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(profile["total_files"], 3)
        langs = {item["lang"] for item in profile["languages"]}
        self.assertIn("Python", langs)
        self.assertIn("docs/api.md", profile["docs"])

    # -------------------------------------------------------------- impact

    def test_impact_by_files_finds_callers(self) -> None:
        proc = run("impact", "--root", str(self.root), "--files", "src/api/user.py")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads((self.root / ".devloop" / "impact.json").read_text(encoding="utf-8"))
        entry = data["files"][0]
        callers = {ref["file"] for ref in entry["refs"]["code"]}
        self.assertIn("src/service/order.py", callers)
        tests = {ref["file"] for ref in entry["refs"]["tests"]}
        self.assertIn("tests/test_user.py", tests)

    def test_impact_ignores_own_artifacts(self) -> None:
        """同时提交代码改动与 .devloop/ 产物时，产物不应出现在变更列表里。"""
        run("scan", "--root", str(self.root))          # 生成 .devloop/profile.json
        (self.root / "src" / "api" / "user.py").write_text(
            "def get_user(user_id: int):\n"
            "    return {'id': user_id}\n\n"
            "def list_users():\n"
            "    return []\n",
            encoding="utf-8",
        )
        if not self.has_git:
            self.skipTest("need git")
        git(self.root, "add", "-A")
        git(self.root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "change+artifacts")

        proc = run("impact", "--root", str(self.root), "--base", "HEAD~1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads((self.root / ".devloop" / "impact.json").read_text(encoding="utf-8"))
        paths = [item["file"] for item in data["files"]]
        self.assertIn("src/api/user.py", paths)
        self.assertFalse([p for p in paths if p.startswith(".devloop/")], paths)

    def test_impact_detects_deleted_file(self) -> None:
        if not self.has_git:
            self.skipTest("need git")
        (self.root / "src" / "api" / "user.py").unlink()
        git(self.root, "add", "-A")
        git(self.root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "del")
        proc = run("impact", "--root", str(self.root), "--base", "HEAD~1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads((self.root / ".devloop" / "impact.json").read_text(encoding="utf-8"))
        self.assertEqual(data["files"][0]["status"], "D")
        self.assertGreaterEqual(data["files"][0]["risk"], 60)

    # --------------------------------------------------------------- drift

    def test_drift_finds_stale_path(self) -> None:
        proc = run("drift", "--root", str(self.root), "--changed", "src/api/user.py")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads((self.root / ".devloop" / "drift.json").read_text(encoding="utf-8"))
        values = {item["value"] for item in data["findings"]}
        self.assertIn("src/legacy/old_user.py", values)
        touched = {item["doc"] for item in data["docs_touching_changes"]}
        self.assertIn("docs/api.md", touched)

    # --------------------------------------------------------------- state

    def test_state_roundtrip(self) -> None:
        run("state", "--root", str(self.root), "--set", "S2", "--note", "改完")
        run("state", "--root", str(self.root), "--add", "补测试")
        proc = run("state", "--root", str(self.root), "--show")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = json.loads(proc.stdout)
        self.assertEqual(state["stage"], "S2")
        self.assertEqual(len(state["open_items"]), 1)
        self.assertFalse(state["open_items"][0]["done"])

        run("state", "--root", str(self.root), "--close", "0")
        state = json.loads(run("state", "--root", str(self.root), "--show").stdout)
        self.assertEqual(state["open_items"], [])

    def test_no_changes_exits_cleanly(self) -> None:
        proc = run("impact", "--root", str(self.root), "--base", "HEAD")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("没有检测到变更", proc.stdout)


class Sanity(unittest.TestCase):
    def test_version(self) -> None:
        proc = run("--version")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("devloop", proc.stdout)

    def test_skill_md_frontmatter(self) -> None:
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---"))
        head = text.split("---")[1]
        self.assertIn("name: dev-loop", head)
        self.assertIn("description:", head)
        self.assertIn("agent_created: true", head)
        self.assertIn("references/", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
