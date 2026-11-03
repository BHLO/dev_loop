#!/usr/bin/env python3
"""技能规则完整性检查：防止后续修改把关键规矩悄悄删掉。

跑法：
    python tests/test_skill_rules.py

它不测代码，测的是 SKILL.md 这份「作业指导书」还完整不完整——
每条规矩都对应一次真实的协作失败，删掉哪条都会退回到原来的坑里。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "SKILL.md"


def section(text: str, title_kw: str) -> str:
    """取出某个二级标题下的正文（兼容最后一节没有后续标题的情况）。"""
    m = re.search(rf"^## {title_kw}.*?$(.*?)(?=^## |\Z)", text, re.S | re.M)
    return m.group(1) if m else ""


class SkillRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SKILL.read_text(encoding="utf-8")

    # ---------------------------------------------------------------- 结构

    def test_required_sections_exist(self) -> None:
        required = [
            "五、S1 需求影响面",
            "六、S2 变更审计",
            "七、S3 文档同步",
            "八、S4 健康检查",
            "九、S5 缺陷定位",
            "十、敏感文件确认协议",
            "十一、偏离清单",
            "十二、验证门禁",
            "十三、提交确认与打标签",
            "十四、闭环回退规则",
            "十五、产物目录",
            "十六、脚本参考",
            "十七、反模式",
        ]
        for title in required:
            with self.subTest(section=title):
                self.assertIn(f"## {title}", self.text, f"缺少章节：{title}")

    def test_iron_rules_count(self) -> None:
        body = section(self.text, "二、铁律")
        rules = re.findall(r"^\d+\. \*\*", body, re.M)
        self.assertGreaterEqual(len(rules), 12, "铁律少于 12 条，可能被删")

    def test_anti_patterns_exist(self) -> None:
        body = section(self.text, "十七、反模式")
        items = [l for l in body.splitlines() if l.strip().startswith("- ")]
        self.assertGreaterEqual(len(items), 15, "反模式条目过少")

    # ------------------------------------------------------------ 关键规矩

    def test_key_rules_present(self) -> None:
        keys = {
            "改造要有理由": "改造理由",
            "清单规模规则": "按模块分组",
            "依赖图": "Mermaid",
            "待新建标记": "待新建",
            "敏感文件分级": "高危",
            "偏离要停": "偏离 S1 清单",
            "验证门禁": "改完必测",
            "环境问题区分": "环境问题",
            "自测清单": "自测清单",
            "提交确认": "清单先给用户看",
            "重大改动": "重大改动",
            "标签规范": "语义化版本",
        }
        for name, needle in keys.items():
            with self.subTest(rule=name):
                self.assertIn(needle, self.text, f"缺少关键规矩：{name}")

    def test_every_subcommand_documented(self) -> None:
        body = section(self.text, "十六、脚本参考")
        for cmd in ("scan", "impact", "graph", "drift", "sensitive", "audit", "verify", "commit"):
            with self.subTest(cmd=cmd):
                self.assertIn(f"`{cmd} ", body + self.text, f"脚本表缺 {cmd}")

    # ------------------------------------------------------------ references

    def test_references_exist_and_referenced(self) -> None:
        refs = sorted((ROOT / "references").glob("*.md"))
        self.assertGreaterEqual(len(refs), 4, "references 文件数量异常")
        for ref in refs:
            with self.subTest(ref=ref.name):
                self.assertIn(f"references/{ref.name}", self.text,
                              f"{ref.name} 没有被 SKILL.md 引用，等于不生效")

    def test_skill_md_not_bloated(self) -> None:
        zh = len(re.findall(r"[\u4e00-\u9fff]", self.text))
        self.assertLess(zh, 9000, f"SKILL.md 正文过长（{zh} 中文字），考虑拆到 references")


if __name__ == "__main__":
    unittest.main(verbosity=2)
