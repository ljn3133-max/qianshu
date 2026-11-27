#!/usr/bin/env python3
"""公开发布安全边界：弱密钥、一次性身份、清库 URL 与白名单导出。"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings  # noqa: E402

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  [{'ok' if condition else 'FAIL'}] {name}" +
          (f" -- {detail}" if detail and not condition else ""))


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=60, **kwargs)


# 生产配置 fail-fast；开发默认值只允许隔离测试和本地开发。
valid = Settings(ENV="prod", SECRET_KEY="s" * 32, MASTER_KEY="ab" * 32, _env_file=None)
check("生产随机密钥配置可加载", valid.ENV == "prod")
for label, values in (
    ("占位签名密钥", {"SECRET_KEY": "change-me-in-env", "MASTER_KEY": "ab" * 32}),
    ("短签名密钥", {"SECRET_KEY": "short", "MASTER_KEY": "ab" * 32}),
    ("空主密钥", {"SECRET_KEY": "s" * 32, "MASTER_KEY": ""}),
    ("非十六进制主密钥", {"SECRET_KEY": "s" * 32, "MASTER_KEY": "z" * 64}),
):
    try:
        Settings(ENV="prod", _env_file=None, **values)
    except ValueError:
        check(f"生产拒绝{label}", True)
    else:
        check(f"生产拒绝{label}", False)
try:
    Settings(ENV="dev", _env_file=None)
except ValueError as exc:
    check("隔离开发配置不被生产校验误伤", False, str(exc))
else:
    check("隔离开发配置不被生产校验误伤", True)

# 未提供一次性身份时必须在登录、启动浏览器或写报告之前失败。
quality_env = os.environ.copy()
quality_env.pop("QIANSHU_TEST_USERNAME", None)
quality_env.pop("PASSWORD", None)
for name in ("run_all.py", "features_test.py", "api_smoke.py", "browser_sweep.py",
             "browser_k8s_mgmt.py", "visual_baseline.py"):
    proc = run([sys.executable, str(ROOT / "tests" / "quality" / name)], env=quality_env)
    check(f"{name} 无一次性身份 fail-fast",
          proc.returncode == 2 and "一次性测试用户" in proc.stdout + proc.stderr,
          f"exit={proc.returncode}")

# 清库测试的 URL 与显式确认均在建立数据库连接之前校验。
pg_test = ROOT / "backend" / "tests" / "test_pg_e2e_smoke.py"
pg_env = os.environ.copy()
pg_env.pop("QIANSHU_PG_URL", None)
pg_env.pop("QIANSHU_PG_E2E_CONFIRM", None)
proc = run([sys.executable, str(pg_test)], env=pg_env)
check("PG 清库测试默认跳过", proc.returncode == 0 and "SKIP" in proc.stdout)
for label, url, confirm in (
    ("拒绝非本机地址", "postgresql://u:p@192.0.2.1:5432/qianshu_e2e", "qianshu_e2e"),
    ("拒绝非专用库名", "postgresql://u:p@127.0.0.1:5432/qianshu", "qianshu"),
    ("拒绝缺少逐库确认", "postgresql://u:p@127.0.0.1:5432/qianshu_e2e", ""),
    ("拒绝错误逐库确认", "postgresql://u:p@127.0.0.1:5432/qianshu_e2e_a", "qianshu_e2e_b"),
):
    env = pg_env.copy()
    env["QIANSHU_PG_URL"] = url
    if confirm:
        env["QIANSHU_PG_E2E_CONFIRM"] = confirm
    proc = run([sys.executable, str(pg_test)], env=env)
    check(f"PG 清库测试{label}", proc.returncode != 0 and "拒绝执行" in proc.stdout + proc.stderr)

# 白名单导出真实执行一次，并验证不覆盖、私钥标记检测和排除范围。
spec = importlib.util.spec_from_file_location("export_public", ROOT / "deploy" / "export_public.py")
export_public = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export_public)
with tempfile.TemporaryDirectory(prefix="qianshu-export-test-") as tmp:
    output = Path(tmp) / "public"
    proc = run([sys.executable, str(ROOT / "deploy" / "export_public.py"),
                "--output", str(output)])
    check("公开候选导出成功", proc.returncode == 0, proc.stderr[-200:])
    manifest_path = output / "PUBLIC-EXPORT-MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    paths = {item["path"] for item in manifest.get("files", [])}
    check("导出清单非空且包含安装与源码入口", manifest.get("file_count", 0) > 100
          and {"README.md", "install.sh", "backend/app/main.py", "frontend/src/main.js"} <= paths)
    check("公开候选包含前端 Logo", "frontend/public/logo.png" in paths)
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    check("NOTICE 记录 Logo 非 Apache-2.0 授权",
          "frontend/public/logo.png" in notice
          and "not licensed under Apache-2.0" in notice)
    forbidden = [p for p in paths if p == "PLAN.md" or p.startswith(
        ("docs/archive/", "docs/tmp/", "agent/knowledge/", "tests/quality/baseline/"))
        or p in {
            "CLAUDE.md", "logo.png",
            "docs/active/gitea-upload.md", "docs/active/release-checklist.md",
            "backend/scripts/gen_license.py", "backend/scripts/gen_license_keypair.py",
        }
        or p == "frontend/vite.allowed-hosts.json" or p.endswith(
            (".env", ".pem", ".key", ".token", ".db", ".sqlite", ".pyc"))]
    check("公开候选不含私有目录、运行数据或密钥文件", not forbidden, str(forbidden))
    before = manifest_path.read_bytes()
    proc = run([sys.executable, str(ROOT / "deploy" / "export_public.py"),
                "--output", str(output)])
    check("导出拒绝覆盖非空目录", proc.returncode != 0 and manifest_path.read_bytes() == before)
    bad = Path(tmp) / "private-key-check"
    bad.mkdir()
    (bad / "example.txt").write_bytes(export_public.PRIVATE_KEY_MARKERS[0] + b"\nFAKE")
    try:
        export_public._check_output(bad)
    except RuntimeError:
        check("导出检测私钥内容标记", True)
    else:
        check("导出检测私钥内容标记", False)

for script in (ROOT / "install.sh", ROOT / "deploy" / "sysprep.sh",
               ROOT / "deploy" / "build_offline.sh", ROOT / "deploy" / "bin" / "qianshu-backend.sh"):
    proc = run(["bash", "-n", str(script)])
    check(f"Shell 语法：{script.relative_to(ROOT)}", proc.returncode == 0, proc.stderr[-200:])

# 安装下载器：TLS 失败时只有已固定 SHA256 才允许 -k 重试，且始终校验内容。
sysprep_text = (ROOT / "deploy" / "sysprep.sh").read_text(encoding="utf-8")
fn_match = re.search(r"(?ms)^download_verified\(\) \{.*?^\}\n", sysprep_text)
check("安装下载器函数存在", bool(fn_match))
if fn_match:
    with tempfile.TemporaryDirectory(prefix="qianshu-download-test-") as tmp:
        tmp = Path(tmp)
        wrapper = tmp / "fn.sh"
        wrapper.write_text(
            "LOG=${LOG:-/dev/null}\n"
            "warn() { printf '[warn] %s\\n' \"$*\" >&2; }\n"
            + fn_match.group(0)
            + "\ndownload_verified \"$@\"\n",
            encoding="utf-8",
        )
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        fake_curl = bin_dir / "curl"
        fake_curl.write_text(
            """#!/bin/sh
insecure=0; out=""
for a in "$@"; do [ "$a" = "-k" ] && insecure=1; done
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "${FAKE_CURL_MODE:-cert}" in
  cert)
    if [ "$insecure" = "0" ]; then
      echo 'curl: (60) Peer certificate has expired.' >&2
      exit 60
    fi
    printf 'payload\\n' > "$out" ;;
  wrong) printf 'tampered\\n' > "$out" ;;
  *) printf 'payload\\n' > "$out" ;;
esac
""",
            encoding="utf-8",
        )
        fake_curl.chmod(0o755)
        payload_sha = hashlib.sha256(b"payload\n").hexdigest()
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["LOG"] = str(tmp / "download.log")

        def download(mode, expected):
            target = tmp / "target.bin"
            target.unlink(missing_ok=True)
            env["FAKE_CURL_MODE"] = mode
            proc = run(["bash", str(wrapper), "https://example.invalid/node.tgz",
                        str(target), expected], env=env)
            return proc, target

        proc, target = download("cert", payload_sha)
        check("TLS 证书失败时用固定 SHA256 兜底",
              proc.returncode == 0 and target.read_bytes() == b"payload\n"
              and "TLS 证书校验失败" in proc.stderr, proc.stderr[-200:])
        proc, target = download("wrong", payload_sha)
        check("TLS 兜底仍拒绝 SHA256 不匹配",
              proc.returncode != 0 and not target.exists(), proc.stderr[-200:])
        proc, target = download("ok", "")
        check("无 SHA256 时拒绝下载",
              proc.returncode != 0 and not target.exists(), proc.stderr[-200:])

# 镜像加速：daemon.json 写入不等于运行态生效；有容器时只做无损重载，不重启。
fn_text = ""
for fn_name in ("docker_mirrors_loaded", "ensure_docker_mirrors_effective"):
    match = re.search(r"(?ms)^%s\(\) \{.*?^\}\n" % fn_name, sysprep_text)
    fn_text += match.group(0) if match else ""
check("镜像加速生效校验函数存在", "ensure_docker_mirrors_effective()" in fn_text
      and "docker_mirrors_loaded()" in fn_text)
if "ensure_docker_mirrors_effective()" in fn_text:
    with tempfile.TemporaryDirectory(prefix="qianshu-mirror-test-") as tmp:
        tmp = Path(tmp)
        state = tmp / "state"
        state.mkdir()
        conf = tmp / "daemon.json"
        actions = tmp / "actions.log"
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        fake_docker = bin_dir / "docker"
        fake_docker.write_text(
            "#!/bin/sh\n"
            "printf 'docker %s\\n' \"$*\" >> '" + str(actions) + "'\n"
            "case \"$1\" in\n"
            "  info)\n"
            "    if [ \"${2:-}\" = \"--format\" ] && [ -f '" + str(state) + "/mirrors_on' ]; then\n"
            "      echo 'https://docker.m.daocloud.io'\n"
            "    fi\n"
            "    exit 0 ;;\n"
            "  ps)\n"
            "    [ -f '" + str(state) + "/has_running' ] && echo abc123\n"
            "    exit 0 ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)
        fake_systemctl = bin_dir / "systemctl"
        fake_systemctl.write_text(
            "#!/bin/sh\n"
            f"printf 'systemctl %s\\n' \"$*\" >> '{actions}'\n"
            "if [ \"$1\" = reload ] && [ \"${FAKE_RELOAD_FIX:-0}\" = 1 ]; then\n"
            f"  : > '{state}/mirrors_on'\n"
            "fi\n"
            "if [ \"$1\" = restart ] && [ \"${FAKE_RESTART_FIX:-0}\" = 1 ]; then\n"
            f"  : > '{state}/mirrors_on'\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_systemctl.chmod(0o755)
        wrapper = tmp / "fn.sh"
        wrapper.write_text(
            f"LOG='{tmp / 'fn.log'}'\n"
            f"ACTIONS='{actions}'\n"
            f"STATE='{state}'\n"
            'info() { printf "[info] %s\\n" "$*" >> "$ACTIONS"; }\n'
            'warn() { printf "[warn] %s\\n" "$*" >> "$ACTIONS"; }\n'
            'run_step() { printf "[run_step] %s\\n" "$*" >> "$ACTIONS"; shift; "$@" || true; }\n'
            "is_podman_docker_shim() { return 1; }\n"
            + fn_text
            + '\nensure_docker_mirrors_effective; echo "rc=$?" >> "$ACTIONS"\n',
            encoding="utf-8",
        )

        def mirror_case(*, has_mirrors=True, loaded=False, reload_fix=False,
                        restart_fix=False, running=False):
            actions.write_text("", encoding="utf-8")
            (state / "mirrors_on").unlink(missing_ok=True)
            (state / "has_running").unlink(missing_ok=True)
            if loaded:
                (state / "mirrors_on").write_text("x", encoding="utf-8")
            if running:
                (state / "has_running").write_text("x", encoding="utf-8")
            conf.write_text(
                '{"registry-mirrors": ["https://docker.m.daocloud.io"]}\n' if has_mirrors
                else '{"log-driver": "json-file"}\n', encoding="utf-8")
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["DOCKER_DAEMON_JSON"] = str(conf)
            env["FAKE_RELOAD_FIX"] = "1" if reload_fix else "0"
            env["FAKE_RESTART_FIX"] = "1" if restart_fix else "0"
            run(["bash", str(wrapper)], env=env)
            return actions.read_text(encoding="utf-8")

        out = mirror_case(loaded=True)
        check("镜像加速已生效时不重载不重启", "rc=0" in out and "systemctl" not in out)
        out = mirror_case(reload_fix=True)
        check("镜像加速未生效时优先无损 reload",
              "rc=0" in out and "systemctl reload docker" in out
              and "systemctl restart docker" not in out)
        out = mirror_case()
        check("reload 无效且无运行容器时重启并明确失败",
              "rc=1" in out and "[run_step] 重启 docker" in out
              and "systemctl restart docker" in out)
        out = mirror_case(running=True)
        check("有运行容器时只告警不重启 docker",
              "rc=1" in out and "[run_step]" not in out
              and "有运行中容器" in out)
        out = mirror_case(has_mirrors=False)
        check("未配置镜像加速时不触碰 docker", "rc=0" in out and "docker " not in out)

# 系统时间校准：HTTP Date 取参考时间，偏差大才改钟；无参考/禁用/干跑都不改。
time_fns = ""
for fn_name in ("time_sync_enabled", "time_reference_epoch", "ntp_step", "sync_system_time"):
    match = re.search(r"(?ms)^%s\(\) \{.*?^\}\n" % fn_name, sysprep_text)
    time_fns += match.group(0) if match else ""
check("时间校准函数存在", "sync_system_time()" in time_fns
      and "time_reference_epoch()" in time_fns)
if "sync_system_time()" in time_fns:
    with tempfile.TemporaryDirectory(prefix="qianshu-time-test-") as tmp:
        tmp = Path(tmp)
        state = tmp / "state"
        state.mkdir()
        actions = tmp / "actions.log"
        fake_bin = tmp / "bin"
        fake_bin.mkdir()
        scripts = {
            "id": "#!/bin/sh\necho 0\n",
            "timedatectl": "#!/bin/sh\necho 'NTP synchronized: no'\n",
            "hwclock": "#!/bin/sh\nexit 0\n",
            "systemctl": "#!/bin/sh\nexit 1\n",
            # 防止无参考时间用例落到宿主机真实 NTP 客户端
            "chronyd": "#!/bin/sh\nexit 1\n",
            "ntpdate": (
                "#!/bin/sh\n"
                "[ \"${FAKE_NTPDATE_OK:-0}\" = 1 ] && exit 0\n"
                "exit 1\n"
            ),
            "curl": (
                "#!/bin/sh\n"
                f"printf 'curl %s\\n' \"$*\" >> '{actions}'\n"
                "[ \"${FAKE_CURL_FAIL:-0}\" = 1 ] && exit 1\n"
                "printf 'HTTP/1.1 200 OK\\r\\nDate: Thu, 01 Jan 2026 00:00:00 GMT\\r\\n\\r\\n'\n"
            ),
            "date": (
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  '+%s') cat \"$STATE/now\" ;;\n"
                "  *'-s @'*) printf 'set %s\\n' \"$*\" >> \"$STATE/set.log\" ;;\n"
                "  *'-d'*'+%s'*) printf '%s\\n' \"${FAKE_REF:-0}\" ;;\n"
                "  *) printf '2026-01-01 00:00:00\\n' ;;\n"
                "esac\n"
            ),
        }
        for name, body in scripts.items():
            p = fake_bin / name
            p.write_text(body, encoding="utf-8")
            p.chmod(0o755)
        wrapper = tmp / "fn.sh"
        wrapper.write_text(
            f"LOG='{tmp / 'fn.log'}'\n"
            f"ACTIONS='{actions}'\n"
            f"STATE='{state}'\n"
            'export STATE\n'
            'DRY_RUN="${DRY_RUN:-0}"\n'
            'info() { printf "[info] %s\\n" "$*" >> "$ACTIONS"; }\n'
            'warn() { printf "[warn] %s\\n" "$*" >> "$ACTIONS"; }\n'
            + time_fns
            + '\nsync_system_time; echo "rc=$?" >> "$ACTIONS"\n',
            encoding="utf-8",
        )

        def time_case(now_offset, *, curl_fail=False, sync="auto", dry_run=False,
                      ntpdate_ok=False):
            actions.write_text("", encoding="utf-8")
            (state / "set.log").unlink(missing_ok=True)
            ref = 1767225600
            (state / "now").write_text(str(ref + now_offset), encoding="utf-8")
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
            env["FAKE_REF"] = str(ref)
            env["FAKE_CURL_FAIL"] = "1" if curl_fail else "0"
            env["FAKE_NTPDATE_OK"] = "1" if ntpdate_ok else "0"
            env["QIANSHU_TIME_HTTP_SOURCES"] = "http://time.invalid"
            env["QIANSHU_TIME_SYNC"] = sync
            env["DRY_RUN"] = "1" if dry_run else "0"
            run(["bash", str(wrapper)], env=env)
            return actions.read_text(encoding="utf-8"), (state / "set.log").exists()

        out, changed = time_case(60)
        check("时间偏差在容差内不改钟", "rc=0" in out and not changed and "无需校准" in out)
        out, changed = time_case(7200)
        set_log = (state / "set.log").read_text(encoding="utf-8") if changed else ""
        check("时间偏差超容差自动改钟", "rc=0" in out and changed
              and "set -u -s @1767225600" in set_log and "已校准" in out)
        out, changed = time_case(7200, curl_fail=True)
        check("取不到参考时间时尝试 NTP 并告警但不阻断", "rc=0" in out and not changed
              and "无法从 HTTP 时间源获取参考时间" in out
              and "NTP 校时也不可用" in out)
        out, changed = time_case(7200, curl_fail=True, ntpdate_ok=True)
        check("chronyd 失败后继续尝试 ntpdate", "rc=0" in out and not changed
              and "已通过 NTP 校准" in out)
        out, changed = time_case(7200, dry_run=True)
        check("干跑只报告不改钟", "rc=0" in out and not changed and "干跑" in out)
        out, changed = time_case(7200, sync="off")
        check("显式禁用时不访问时间源", "rc=0" in out and not changed
              and "已禁用" in out and "curl " not in out)

install_text = (ROOT / "install.sh").read_text(encoding="utf-8")
check("install.sh 在联网前调用时间校准", "TIME_ARGS=(--time-only)" in install_text
      and "bash \"$SYSPREP_SH\" \"${TIME_ARGS[@]}\"" in install_text)
check("只读体检不自动改时钟", '[ "$FIX" = "1" ] || [ "$BOOTSTRAP" = "1" ]' in sysprep_text
      and '"$MIRROR_ONLY" = "1" ] || [ "$POSTGRES_ONLY" = "1" ]' in sysprep_text)
# 回归：--time-only 分发必须早于只读体检的默认 exit，否则该模式永远走不到。
time_dispatch = sysprep_text.find('if [ "$TIME_ONLY" = "1" ]; then')
preflight_banner = sysprep_text.find('echo "===== 千枢环境预检/调优')
tips_exit = sysprep_text.find("以上问题可自动修复")
check("--time-only 分发在体检之前", 0 <= time_dispatch < preflight_banner
      and 0 <= time_dispatch < tips_exit)
# 端到端：真实脚本跑 --time-only --dry-run，确认只做对时、不进入体检、不写时钟。
with tempfile.TemporaryDirectory(prefix="qianshu-time-e2e-") as tmp:
    tmp = Path(tmp)
    state = tmp / "state"
    state.mkdir()
    fake_bin = tmp / "bin"
    fake_bin.mkdir()
    ref = 1767225600
    (state / "now").write_text(str(ref + 7200), encoding="utf-8")
    e2e_scripts = {
        "id": "#!/bin/sh\necho 0\n",
        "timedatectl": "#!/bin/sh\necho 'System clock synchronized: no'\n",
        "hwclock": "#!/bin/sh\nexit 0\n",
        "systemctl": "#!/bin/sh\nexit 1\n",
        "chronyd": "#!/bin/sh\nexit 1\n",
        "ntpdate": "#!/bin/sh\nexit 1\n",
        "curl": (
            "#!/bin/sh\n"
            "printf 'HTTP/1.1 200 OK\\r\\nDate: Thu, 01 Jan 2026 00:00:00 GMT\\r\\n\\r\\n'\n"
        ),
        "date": (
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  '+%s') cat \"$STATE/now\" ;;\n"
            "  *'-d'*'+%s'*) printf '%s\\n' \"$FAKE_REF\" ;;\n"
            "  *'-s @'*) printf 'set %s\\n' \"$*\" >> \"$STATE/set.log\" ;;\n"
            "  *) exec /bin/date \"$@\" ;;\n"
            "esac\n"
        ),
    }
    for name, body in e2e_scripts.items():
        p = fake_bin / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    logs_before = set(Path("/var/log").glob("qianshu-sysprep-*.log"))
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["STATE"] = str(state)
    env["FAKE_REF"] = str(ref)
    env["QIANSHU_TIME_HTTP_SOURCES"] = "http://time.invalid"
    r = run(["bash", str(ROOT / "deploy" / "sysprep.sh"), "--time-only", "--dry-run"], env=env)
    out = (r.stdout or "") + (r.stderr or "")
    check("端到端 --time-only 只做对时", r.returncode == 0
          and "将把系统时间校准到参考时间" in out
          and "环境体检报告" not in out
          and not (state / "set.log").exists())
    for p in set(Path("/var/log").glob("qianshu-sysprep-*.log")) - logs_before:
        p.unlink(missing_ok=True)

sysprep = (ROOT / "deploy" / "sysprep.sh").read_text(encoding="utf-8")
for target in ('cat > "$LIMITS_CONF.tmp"', "cat > /etc/systemd/system.conf.d/99-qianshu.conf",
               'cat > "$SYSCTL_CONF.tmp"'):
    position = sysprep.find(target)
    dry_if = sysprep.rfind('if [ "$DRY_RUN" = "1" ]; then', 0, position)
    dry_else = sysprep.rfind("  else", dry_if, position)
    check(f"dry-run 写入保护：{target.split('>')[-1].strip()}",
          position > 0 and dry_if >= 0 and dry_else > dry_if)
check("dry-run 不执行 daemon-reexec", 'run_step "systemd DefaultLimitNOFILE 生效" systemctl daemon-reexec'
      in sysprep and '[ "$DRY_RUN" = "1" ] || run_step "systemd DefaultLimitNOFILE 生效"' not in sysprep)
offline = (ROOT / "deploy" / "build_offline.sh").read_text(encoding="utf-8")
check("离线包只复制 active 文档", 'cp -a "$APP_DIR/docs/active" "$OUT_DIR/$PKG_NAME/docs/active"'
      in offline and 'cp -a "$APP_DIR/docs" "$OUT_DIR/$PKG_NAME/docs"' not in offline)
compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
check("Compose API 与 OTLP 默认回环绑定",
      '"${BACKEND_BIND_HOST:-127.0.0.1}:${BACKEND_PORT:-8810}:8810"' in compose
      and compose.count('"${OTLP_BIND_HOST:-127.0.0.1}:${OTLP_') == 2)

print(f"\nrelease guards: {len(PASS)} ok / {len(FAIL)} fail")
sys.exit(1 if FAIL else 0)
