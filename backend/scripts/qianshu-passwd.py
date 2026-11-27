#!/usr/bin/env python3
"""管理员密码找回兜底：服务器本地执行，无需登录。

用法（在千枢 backend 目录，或任意目录均可）：
  python3 scripts/qianshu-passwd.py reset <username> [--password 新密码]

行为：
  - 直接改库（bcrypt 与 app/core/security.py 同一口径），旧密码立即失效；
  - 未给 --password 时服务端生成一次性密码，**只打印这一次**（库内只存哈希）；
  - 一律强制首登改密（must_change_password=True）；
  - 留审计 action=user.password.reset（detail.source=cli，密码绝不入审计）。

安全边界：只能在服务器本机执行（有库文件/配置的机器）——这正是兜底能力只对
本机/私网提示（登录页）的原因；见 docs/active/deployment.md《管理员找回》。
"""
from __future__ import annotations

import argparse
import os
import sys

# 与服务同口径：以 backend 为工作目录加载 .env（DATABASE_URL/MASTER_KEY 等）
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_BACKEND)
sys.path.insert(0, _BACKEND)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="qianshu-passwd.py",
        description="千枢账号密码找回（服务器本地兜底；重置后强制首登改密）")
    sub = parser.add_subparsers(dest="cmd")
    p_reset = sub.add_parser("reset", help="重置指定用户密码")
    p_reset.add_argument("username", help="用户名（如 admin）")
    p_reset.add_argument("--password", default="",
                         help="指定新密码（缺省自动生成一次性密码并打印一次）")
    args = parser.parse_args()
    if args.cmd != "reset":
        parser.print_help()
        return 1

    from app.core.db import SessionLocal
    from app.core.security import (check_password_strength, generate_password,
                                   hash_password)
    from app.models.user import User
    from app.services import audit

    plain = (args.password or "").strip()
    if plain:
        weak = check_password_strength(plain)
        if weak:
            print(f"错误：指定密码不合格——{weak}", file=sys.stderr)
            return 2
        generated = False
    else:
        plain, generated = generate_password(), True

    db = SessionLocal()
    try:
        row = db.query(User).filter(User.username == args.username).first()
        if row is None:
            print(f"错误：用户不存在——{args.username}", file=sys.stderr)
            return 1
        row.password_hash = hash_password(plain)
        row.must_change_password = True   # 找回一律强制首登改密
        db.flush()
        audit.record(
            db, action="user.password.reset", username="cli",
            resource_type="user", resource_id=row.id,
            detail={"target_username": row.username, "must_change_password": True,
                    "source": "cli", "password_generated": generated},
        )
        db.commit()
    finally:
        db.close()

    print(f"用户 {args.username} 的密码已重置（旧密码已失效）。")
    if generated:
        print("一次性新密码（只打印这一次，请立即保存）：")
        print(f"  {plain}")
    else:
        print("已按指定密码重置（不回显）。")
    print("该用户下次登录将被强制修改密码。")
    print("提示：如该账号启用了动态验证码（TOTP），登录时仍需动态码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
