#!/usr/bin/env python3
"""任务文档结构与路径契约；只读检查，不加载 archive/tmp。"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
ACTIVE = ROOT / "docs" / "active"
TASKS = {
    "FIRST-USE": ACTIVE / "tasks" / "first-use.md",
    "CONCISE-INTERACTION": ACTIVE / "tasks" / "concise-interaction.md",
    "COST-INSIGHTS": ACTIVE / "tasks" / "cost-insights.md",
}
# PLAN 中明确约定后续任务创建；其它缺失测试路径一律失败。
PLANNED_TESTS = {
    "backend/tests/test_onboarding_tasks_mock.py",
    "backend/tests/test_cost_insights_mock.py",
    "tests/quality/first_use_browser.py",
    "tests/quality/cost_insights_browser.py",
}


class TaskDocumentTests(unittest.TestCase):
    def test_readme_routes_each_id_to_one_contract(self):
        readme = (ACTIVE / "README.md").read_text()
        for task_id, path in TASKS.items():
            self.assertEqual(readme.count(f"`{task_id}`"), 1, task_id)
            relative = path.relative_to(ACTIVE).as_posix()
            self.assertEqual(readme.count(f"]({relative})"), 1, relative)

    def test_task_ids_are_unique_and_all_eight_sections_exist(self):
        seen = []
        for expected_id, path in TASKS.items():
            text = path.read_text()
            ids = re.findall(r"^- 任务 ID：`([A-Z-]+)`。", text, re.M)
            self.assertEqual(ids, [expected_id], path)
            seen.extend(ids)
            sections = re.findall(r"^## ([1-8])\. ", text, re.M)
            self.assertEqual(sections, list("12345678"), path)
        self.assertEqual(len(seen), len(set(seen)))

    def test_local_links_resolve(self):
        files = [ACTIVE / "README.md", ACTIVE / "status.md", ACTIVE / "engineering.md", *TASKS.values()]
        for path in files:
            text = path.read_text()
            for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
                if "://" in target or target.startswith("#"):
                    continue
                clean = target.split("#", 1)[0]
                if not clean:
                    continue
                resolved = (path.parent / clean).resolve()
                self.assertTrue(resolved.exists(), f"{path}: {target}")
                self.assertTrue(resolved.is_relative_to(ROOT), f"{path}: outside root")

    def test_referenced_test_paths_exist_or_are_explicit_plan_items(self):
        found = set()
        for path in TASKS.values():
            text = path.read_text()
            section_six = text.split("## 6. ", 1)[1].split("## 7. ", 1)[0]
            for candidate in re.findall(r"`((?:backend/tests|tests/quality)/[^`]+\.py)`", section_six):
                found.add(candidate)
                test_path = ROOT / candidate
                if not test_path.exists():
                    self.assertIn(candidate, PLANNED_TESTS, f"unplanned missing test: {candidate}")
                self.assertNotIn("..", Path(candidate).parts)
        self.assertTrue(PLANNED_TESTS.issubset(found))

    def test_new_mock_tests_enter_existing_ci_discovery(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("working-directory: backend", ci)
        self.assertIn("tests/test_*_mock.py", ci)
        for name in ("test_quality_assertions_mock.py", "test_task_docs_mock.py"):
            self.assertTrue((ROOT / "backend" / "tests" / name).exists())


if __name__ == "__main__":
    unittest.main()
