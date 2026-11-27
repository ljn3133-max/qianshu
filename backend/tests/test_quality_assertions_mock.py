#!/usr/bin/env python3
"""质量门自身反例：不访问网络、数据库或浏览器。"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]

# run_all 在导入时校验一次性身份；本测试全部走 mock，不访问真实实例。
os.environ.setdefault("QIANSHU_TEST_USERNAME", "ci-once-quality")
os.environ.setdefault("PASSWORD", "synthetic-quality-test-value")


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


features = load_module("quality_features_under_test", "tests/quality/features_test.py")
run_all = load_module("quality_run_all_under_test", "tests/quality/run_all.py")


def completed_json(payload, status=200, returncode=0):
    return subprocess.CompletedProcess(
        [], returncode, stdout=f"{json.dumps(payload)}\n{status}", stderr=""
    )


class FeatureAssertionTests(unittest.TestCase):
    def setUp(self):
        features.results.clear()
        features.skipped.clear()

    def run_case(self, payload, expect, status=200, returncode=0):
        case = {"name": " isolated assertion ", "api": "GET /isolated", "expect": expect}
        with mock.patch.object(
            features.subprocess, "run",
            return_value=completed_json(payload, status, returncode),
        ):
            features.run_case("fake-token", case)
        return features.results[-1] if features.results else None

    def test_unrelated_object_fails_faces_exact_length(self):
        ok, _, why = self.run_case({"unrelated": True}, "faces 长度=5")
        self.assertFalse(ok)
        self.assertIn("faces", why)

    def test_unknown_assertion_fails_even_for_nonempty_response(self):
        ok, _, why = self.run_case({"anything": True}, "unknown assertion")
        self.assertFalse(ok)
        self.assertIn("未知断言", why)

    def test_exact_length_checks_actual_collection(self):
        ok, _, _ = self.run_case({"faces": [1, 2, 3, 4, 5]}, "faces 长度=5")
        self.assertTrue(ok)
        ok, _, _ = self.run_case({"faces": [1, 2]}, "faces 长度=5")
        self.assertFalse(ok)

    def test_greater_than_length_checks_actual_collection(self):
        ok, _, _ = self.run_case({"tools": list(range(21))}, "tools 长度>20")
        self.assertTrue(ok)
        ok, _, _ = self.run_case({"tools": []}, "tools 长度>20")
        self.assertFalse(ok)

    def test_http_error_fails_even_with_expected_field(self):
        ok, _, why = self.run_case({"token": "must-not-pass"}, "token 存在", status=500)
        self.assertFalse(ok)
        self.assertIn("HTTP 500", why)

    def test_curl_failure_fails_even_with_successful_output(self):
        ok, _, why = self.run_case({"token": "must-not-pass"}, "token 存在", returncode=7)
        self.assertFalse(ok)
        self.assertIn("退出码 7", why)

    def test_intercept_requires_4xx_and_detail(self):
        ok, _, _ = self.run_case({"detail": "明确拒绝该操作"}, "拦截", status=403)
        self.assertTrue(ok)
        ok, _, why = self.run_case({"detail": "错误状态却返回成功"}, "拦截", status=200)
        self.assertFalse(ok)
        self.assertIn("HTTP 200", why)

    def test_not_connected_is_skip_not_pass(self):
        self.run_case({"detail": "未接入集群"}, "列表")
        self.assertEqual(features.results, [])
        self.assertEqual(len(features.skipped), 1)
        self.assertIn("未接入", features.skipped[0][1])


class RunAllExitTests(unittest.TestCase):
    def setUp(self):
        self.old_readonly = run_all.READONLY
        run_all.READONLY = True

    def tearDown(self):
        run_all.READONLY = self.old_readonly

    def fake_sh(self, feature_returncode=0, visual_output="视觉基线对比: 0 页有变化\n"):
        def invoke(cmd, timeout=300):
            outputs = {
                "gate.py": "静态质量门全绿\n",
                "structure_fingerprint.py": "0 项问题\n",
                "api_smoke.py": "接口冒烟: 1/1 通过\n",
                "browser_sweep.py": "页面渲染: 1/1 正常\n页面质量: 0 项问题\nJS 错误: 零\n",
                "visual_baseline.py": visual_output,
            }
            for script, output in outputs.items():
                if script in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")
            raise AssertionError(f"unexpected command: {cmd}")

        feature_output = (
            "== 功能回归: 1/1 通过；跳过 1 项（不计通过） ==\n"
            "  - 写用例 SKIP（只读模式，未执行非 GET 用例）\n"
        )
        feature_result = subprocess.CompletedProcess(
            [], feature_returncode, stdout=feature_output, stderr=""
        )
        return invoke, feature_result

    def execute(self, feature_returncode=0, visual_output="视觉基线对比: 0 页有变化\n"):
        invoke, feature_result = self.fake_sh(feature_returncode, visual_output)
        with tempfile.TemporaryDirectory() as out_dir, \
                mock.patch.object(run_all, "OUT", out_dir), \
                mock.patch.object(run_all, "sh", side_effect=invoke), \
                mock.patch.object(run_all.subprocess, "run", return_value=feature_result), \
                contextlib.redirect_stdout(io.StringIO()):
            code = run_all.main()
            reports = list(Path(out_dir).glob("验收摘要-*.md"))
            self.assertEqual(len(reports), 1)
            return code, reports[0].read_text()

    def test_success_text_with_nonzero_child_exit_fails(self):
        code, report = self.execute(feature_returncode=1)
        self.assertEqual(code, 1)
        self.assertIn("功能回归", report)
        self.assertNotIn("✅", report)

    def test_visual_difference_is_pending_not_pass(self):
        code, report = self.execute(visual_output="视觉基线对比: 1 页有变化\n· /cost\n")
        self.assertEqual(code, 1)
        self.assertIn("待人工复核", report)
        self.assertNotIn("无需人工验收", report)

    def test_skips_are_not_added_to_passed_count(self):
        code, report = self.execute()
        self.assertEqual(code, 0)
        self.assertIn("功能回归: 1/1 通过", report)
        self.assertIn("跳过共 1 项", report)
        self.assertIn("不计为通过", report)
        self.assertIn("不替代产品任务与人工验收", report)


if __name__ == "__main__":
    unittest.main()
