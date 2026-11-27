"""真实运行实例验收的一次性身份约束；任何脚本都不得回退到固定管理员口令。"""
import os
import sys


def require_test_credentials(argv=None):
    # 口令只走环境变量：argv 会暴露在 ps / shell 历史 / CI 日志里。
    username = os.environ.get("QIANSHU_TEST_USERNAME", "")
    password = os.environ.get("PASSWORD", "")
    if not username or not password or username.lower() == "admin":
        print("必须使用非 admin 一次性测试用户：设置 QIANSHU_TEST_USERNAME 和 PASSWORD 环境变量；"
              "验收后由调用方清理该用户", file=sys.stderr)
        raise SystemExit(2)
    return username, password
