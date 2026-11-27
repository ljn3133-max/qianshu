#!/usr/bin/env python3
"""按白名单导出公开候选源码，不读取或删除本机私有运行材料。

输出目录必须不存在或为空，且不能位于源码树内。导出只代表候选范围检查通过，
不代表第三方素材授权、历史泄露或产品验收已经完成。
"""
import argparse
import fnmatch
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent

ROOT_FILES = (
    ".env.example", ".gitignore", "build-images.sh", "CHANGELOG.md",
    "CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "LICENSE",
    "NOTICE", "README.en.md", "README.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md",
    "docker-compose.yml", "install.sh", "upgrade.sh",
)
TREES = (
    ".github/workflows", "agent", "backend/app", "backend/config",
    "backend/scripts", "backend/tests", "config", "deploy", "docs/active",
    "frontend/public", "frontend/src", "tests/quality", "update-data",
)
PLAIN_FILES = (
    "backend/.dockerignore", "backend/Dockerfile", "backend/constraints.txt",
    "backend/requirements.txt", "frontend/.dockerignore", "frontend/Dockerfile",
    "frontend/index.html", "frontend/nginx.conf",
    "frontend/package-lock.json", "frontend/package.json", "frontend/vite.config.js",
)
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
EXCLUDED_NAMES = {".env", ".DS_Store"}
EXCLUDED_PATTERNS = (
    ".env.*", "*.bak", "*.bak.*", "*.db", "*.db-*", "*.db.*", "*.key",
    "*.lock", "*.log", "*.pem", "*.pid", "*.pyc", "*.secret", "*.sock",
    "*.sqlite", "*.sqlite3", "*.token",
)
EXCLUDED_RELATIVE = {
    "agent/knowledge", "docs/archive", "docs/tmp", "frontend/vite.allowed-hosts.json",
    "tests/quality/baseline",
    # 内部发布/运维资料，不进入公开候选。
    "docs/active/gitea-upload.md", "docs/active/release-checklist.md",
    # 厂商侧 License 签发工具，不进入公开仓库。
    "backend/scripts/gen_license.py", "backend/scripts/gen_license_keypair.py",
}
PRIVATE_KEY_MARKERS = tuple(
    b"-----BEGIN " + kind + b"PRIVATE KEY-----"
    for kind in (b"", b"RSA ", b"EC ", b"OPENSSH ")
)


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _is_excluded(path: Path) -> bool:
    rel = _relative(path)
    if any(rel == item or rel.startswith(item + "/") for item in EXCLUDED_RELATIVE):
        return True
    if any(part in EXCLUDED_PARTS for part in path.parts):
        return True
    name = path.name
    if name in EXCLUDED_NAMES and name != ".env.example":
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDED_PATTERNS)


def _copy_selected(output: Path) -> None:
    for rel in ROOT_FILES + PLAIN_FILES:
        src = ROOT / rel
        if not src.is_file() or src.is_symlink():
            raise RuntimeError(f"公开白名单文件缺失或为符号链接：{rel}")
        target = output / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)

    for rel in TREES:
        src = ROOT / rel
        if not src.is_dir() or src.is_symlink():
            raise RuntimeError(f"公开白名单目录缺失或为符号链接：{rel}")
        for current, dirs, files in __import__("os").walk(src, followlinks=False):
            current_path = Path(current)
            dirs[:] = sorted(
                name for name in dirs
                if not _is_excluded(current_path / name)
            )
            for name in sorted(files):
                item = current_path / name
                if _is_excluded(item):
                    continue
                if item.is_symlink() or not item.is_file():
                    raise RuntimeError(f"公开候选含符号链接或非常规文件：{_relative(item)}")
                target = output / _relative(item)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)


def _check_output(output: Path) -> list:
    files = []
    for path in sorted(output.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"导出结果含符号链接：{path.relative_to(output)}")
        if not path.is_file():
            continue
        rel = path.relative_to(output).as_posix()
        with path.open("rb") as stream:
            content = stream.read(4 * 1024 * 1024 + 1)
        if len(content) > 4 * 1024 * 1024:
            raise RuntimeError(f"公开候选含超过 4 MiB 的文件，需单独复核：{rel}")
        if any(marker in content for marker in PRIVATE_KEY_MARKERS):
            raise RuntimeError(f"公开候选疑似包含私钥：{rel}")
        files.append({"path": rel, "sha256": hashlib.sha256(content).hexdigest()})
    if not files:
        raise RuntimeError("公开导出为空")
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="不存在或为空的导出目录")
    parser.add_argument(
        "--manifest",
        help="清单输出路径；默认写入导出目录。可指向导出目录外，避免清单进入仓库。",
    )
    args = parser.parse_args()
    output = Path(args.output).expanduser().resolve()
    source = ROOT.resolve()
    if output == source or source in output.parents:
        parser.error("输出目录不能位于源码树内")
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            parser.error("输出目录必须不存在或为空；不会覆盖或清空已有内容")
    output.mkdir(parents=True, exist_ok=True)
    try:
        _copy_selected(output)
        files = _check_output(output)
    except Exception:
        print("导出失败：请检查上述路径；已有输出未被自动删除。", file=sys.stderr)
        raise
    manifest = {
        "scope": "public-candidate",
        "file_count": len(files),
        "files": files,
        "release_blockers": [
            "第三方依赖与素材授权仍需逐项核对",
            "视觉基线和具体开发域名未纳入导出",
            "本工具不证明原目录或任何 Git 历史无敏感信息",
        ],
    }
    manifest_path = (
        Path(args.manifest).expanduser().resolve()
        if args.manifest else output / "PUBLIC-EXPORT-MANIFEST.json"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"公开候选导出完成：{len(files)} 个文件；清单={manifest_path}")
    print("注意：候选范围检查通过不等于授权、历史泄露或产品验收完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
