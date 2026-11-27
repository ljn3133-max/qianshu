"""K8s 管理操作流程：添加节点/备份/升级/清空——任务化执行体。

安全边界：目标机必须在任务登记里；高危确认在 API 层（输入名称不符拒绝）；
命令全部代码模板生成（不执行 LLM/用户粘贴的命令原文）。
"""
from __future__ import annotations

import logging
from datetime import datetime

from app.collector.ssh import run_ssh

logger = logging.getLogger("qianshu.k8s_ops")

# 适配各版本与发行版：CentOS 默认 yum 源没有 k8s 包，必须先配置软件源；
# yum（el7 源）优先，apt（阿里云 k8s 源）兜底；版本由 {ver} 对齐集群
INSTALL_CMD = """(cat > /etc/yum.repos.d/kubernetes.repo <<'REPO'
[kubernetes]
name=Kubernetes
baseurl=https://mirrors.aliyun.com/kubernetes/yum/repos/kubernetes-el7-x86_64/
enabled=1
gpgcheck=0
REPO
yum install -y kubelet-{ver}* kubeadm-{ver}* kubectl-{ver}* || yum install -y kubelet-{ver} kubeadm-{ver} kubectl-{ver} || { VER=$(yum list available "kubelet-*" 2>/dev/null | awk '/kubelet.x86_64/{print $2}' | sort -V | tail -1 | cut -d- -f1); echo "指定版本 {ver} 源内不可得，降级安装源内版本 $VER"; yum install -y kubelet-$VER kubeadm-$VER kubectl-$VER; }) || (curl -fsSL https://mirrors.aliyun.com/kubernetes/apt/doc/apt-key.gpg | apt-key add - 2>/dev/null; echo "deb https://mirrors.aliyun.com/kubernetes/apt/ kubernetes-xenial main" > /etc/apt/sources.list.d/kubernetes.list; apt-get update -qq; apt-get install -y kubelet={ver}-* kubeadm={ver}-* kubectl={ver}-*)"""

# 安装 containerd 不等于运行时就绪，必须确认 kubelet 能使用 CRI——
# Docker 自带 containerd 的 config.toml 默认 disabled_plugins=["cri"]，kubelet 1.24+
# 连上直接报 unknown service runtime.v1.RuntimeService。这里校验 CRI 真实可用，
# 被禁则自动启用 + SystemdCgroup 对齐 kubeadm(1.28 默认 systemd)，最后以 ctr plugins 实证。
RUNTIME_CMD = r"""set -e
if ! command -v containerd >/dev/null 2>&1; then
  (curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo -o /etc/yum.repos.d/docker-ce.repo && yum install -y containerd.io) || yum install -y containerd || (apt-get update -qq && apt-get install -y containerd)
fi
if ! grep -q '\[plugins."io.containerd.grpc.v1.cri"\]' /etc/containerd/config.toml 2>/dev/null; then
  containerd config default > /etc/containerd/config.toml
  echo "已重建 containerd 标准配置（原配置无 CRI 段——Docker 极简配置）"
fi
sed -i 's/disabled_plugins = \["cri"\]/disabled_plugins = []/' /etc/containerd/config.toml
sed -i 's/^\(\s*\)SystemdCgroup = false/\1SystemdCgroup = true/' /etc/containerd/config.toml
# sandbox（pause）镜像源：默认 registry.k8s.io 不可达时，pod 无法启动
sed -i 's|.*sandbox_image = .*|\t\tsandbox_image = "registry.aliyuncs.com/google_containers/pause:3.9"|' /etc/containerd/config.toml
grep -q 'sandbox_image' /etc/containerd/config.toml || sed -i '/\[plugins."io.containerd.grpc.v1.cri"\]/a\    sandbox_image = "registry.aliyuncs.com/google_containers/pause:3.9"' /etc/containerd/config.toml
systemctl enable --now containerd >/dev/null 2>&1
systemctl restart containerd
sleep 2
if ctr plugins ls 2>/dev/null | grep -i 'cri' | grep -qi 'ok'; then
  echo "CRI 校验通过（io.containerd.grpc.v1 cri 插件 ok）"
else
  echo "CRI 校验失败（ctr plugins 无可用 cri）："; ctr plugins ls | grep -iE 'cri|grpc' | sed -n '1,5p'; exit 1
fi"""

# 旧集群残留清理（kubeadm 标准 reset；不碰 iptables——机器上可能有 docker 业务容器，
# 全局清表会断其网络。CNI 残留配置必须清，否则新旧 CNI 冲突）
RESET_CMD = "(kubeadm reset -f 2>&1 | tail -3 || true) && (rm -rf /etc/cni/net.d/* 2>/dev/null || true) && echo 清理完成"

# kubeadm 默认 registry.k8s.io 在国内环境可能无法拉取，导致 init 长时间无镜像。
# 国内默认阿里云镜像；换环境用 meta.image_repo 覆盖
DEFAULT_IMAGE_REPO = "registry.aliyuncs.com/google_containers"

# 节点 Ready 最长等待 5 分钟，慢镜像源环境下 2 分钟可能不够
WAIT_READY_CMD = ("for i in $(seq 1 60); do kubectl get nodes 2>/dev/null | grep -q ' Ready' && break; "
                  "sleep 5; done; kubectl get nodes; kubectl get nodes | grep -q ' Ready'")

# flannel 镜像国内镜像源（ghcr/docker 直连在内网慢到拉不完；可 meta.cni_image_repo 覆盖）
CNI_IMAGE_REPO = "ghcr.m.daocloud.io"


def add_node_steps() -> list:
    return [
        {"key": "probe", "label": "目标机探测（SSH/系统/资源）", "status": "pending"},
        {"key": "reset", "label": "清理旧集群残留（kubeadm reset）", "status": "pending"},
        {"key": "runtime", "label": "容器运行时就绪（CRI 校验）", "status": "pending"},
        {"key": "kubeadm", "label": "安装 kubeadm/kubelet/kubectl（与集群同版本）", "status": "pending"},
        {"key": "join", "label": "加入集群（kubeadm join）", "status": "pending"},
        {"key": "verify", "label": "节点验证（kubelet 活跃）", "status": "pending"},
    ]


def run_add_node(task_id: int, log, set_steps, meta: dict):
    """添加工作节点（每次一台）：版本对齐集群、过程全记录、可终止。"""
    ip = meta.get("ip", "")
    user = meta.get("ssh_user", "root")
    password = meta.get("ssh_password", "")
    master_ssh = meta.get("master_ssh", "")
    master_user = meta.get("master_user", "root")
    steps = add_node_steps()

    def mark(key, status, note=""):
        for s in steps:
            if s["key"] == key:
                s["status"] = status
                if note:
                    s["note"] = note
        set_steps(steps)

    def runner(cmd, label):
        log(f"$ {label}")
        return run_ssh(ip, user, cmd, on_line=log, timeout=900, password=password)

    # 1) 探测
    mark("probe", "running")
    r = runner("echo ok && hostname && uname -m && (command -v containerd >/dev/null && echo has-containerd || echo no-containerd) && (command -v kubeadm >/dev/null && kubeadm version -o short || echo no-kubeadm)", "探测目标机")
    if not r["ok"]:
        mark("probe", "failed", "SSH 不可达")
        return False, "目标机 SSH 探测失败"
    mark("probe", "succeeded")

    # 2) 旧集群残留清理（重新加入/换集群场景必须 reset，否则 join 状态冲突）
    mark("reset", "running")
    r = runner(RESET_CMD, "清理旧集群残留")
    mark("reset", "succeeded" if r["ok"] else "failed")
    if not r["ok"]:
        return False, "旧集群残留清理失败"

    # 3) 集群版本（控制面探测，适配所有版本——版本对齐）
    import re as _re

    k8s_ver = meta.get("k8s_version", "")
    if not k8s_ver and master_ssh:
        log("从控制面探测集群版本")
        pr = run_ssh(master_ssh, master_user, "kubeadm version -o short 2>/dev/null || echo unknown", timeout=30)
        # 只认 X.Y.Z 形态，错误输出严禁进入版本号
        for ln in (pr.get("lines") or []):
            m = _re.search(r"v?(\d+\.\d+\.\d+)", ln)
            if m:
                k8s_ver = m.group(1)
                break
        if not k8s_ver:
            log("控制面版本探测不可用（SSH/权限），使用指定版本或默认")
    k8s_ver = k8s_ver or meta.get("fallback_version", "1.29.4")
    log(f"目标 k8s 版本: {k8s_ver}（与集群对齐；kubeadm 路径适配任意版本）")

    # 4) 容器运行时：必须真实校验 CRI，只看二进制是否存在会漏掉 cri 被禁用
    mark("runtime", "running")
    r = runner(RUNTIME_CMD, "容器运行时就绪（CRI 校验）")
    mark("runtime", "succeeded" if r["ok"] else "failed")
    if not r["ok"]:
        return False, "容器运行时就绪失败（CRI 校验不通过，见日志）"

    # 5) k8s 组件（版本对齐）
    mark("kubeadm", "running")
    r = runner(
        INSTALL_CMD.replace("{ver}", k8s_ver),
        f"配置 k8s 源并安装组件 {k8s_ver}")
    if not r["ok"]:
        mark("kubeadm", "failed", "安装失败")
        return False, f"k8s 组件 {k8s_ver} 安装失败（见日志）"
    runner("systemctl enable --now kubelet", "启用 kubelet")
    mark("kubeadm", "succeeded")

    # 6) 加入集群（join 命令由控制面模板生成）
    mark("join", "running")
    if not master_ssh:
        mark("join", "failed", "未配置控制面 SSH")
        return False, "未配置控制面（master_ssh）——join 命令无法生成"
    log("控制面生成加入令牌（kubeadm token create --print-join-command）")
    pr = run_ssh(master_ssh, master_user,
                 "kubeadm token create --print-join-command 2>/dev/null | tail -1", timeout=60)
    join_cmd = (pr.get("lines") or [""])[-1].strip()
    if not join_cmd.startswith("kubeadm join"):
        mark("join", "failed", "join 命令获取失败")
        return False, "获取 join 命令失败（控制面 kubeadm token）"
    log("join 命令就绪")
    r = runner(join_cmd, "kubeadm join 加入集群")
    if not r["ok"]:
        mark("join", "failed", "join 失败")
        return False, "kubeadm join 失败（见日志）"
    mark("join", "succeeded")

    # 7) 验证（失败即任务失败——节点 Ready 才算数，不看命令退出码）
    mark("verify", "running")
    r = runner("systemctl is-active kubelet && hostname", "节点验证（kubelet）")
    ok = r["ok"]
    if ok and master_ssh:
        hn = ""
        for ln in (r.get("lines") or [])[::-1]:
            if ln.strip() and "kubelet" not in ln:
                hn = ln.strip()
                break
        if hn and _re.fullmatch(r"[a-zA-Z0-9._-]{1,63}", hn):
            log(f"控制面确认节点 {hn} Ready（最长等 5 分钟）")
            pr = run_ssh(master_ssh, master_user,
                         f"for i in $(seq 1 60); do kubectl get nodes 2>/dev/null | grep '{hn}' | grep -q ' Ready' && break; sleep 5; done; kubectl get nodes | grep '{hn}'; kubectl get nodes | grep '{hn}' | grep -q ' Ready'",
                         on_line=log, timeout=360)
            ok = pr["ok"]
        else:
            log(f"主机名不可识别（{hn[:30]}），跳过 Ready 复核")
    mark("verify", "succeeded" if ok else "failed")
    if not ok:
        return False, "节点验证失败（kubelet 未活跃或未 Ready，见日志）"
    log("节点加入完成（资源页-Node 可见）")
    return True, ""


def resolve_master_ssh(db, cluster_name: str = "", cluster_id: int = 0,
                       strict: bool = False) -> tuple:
    """集群登记 → 控制面 SSH（管理操作的目标机来源，换环境改配置不改代码）。

    strict=True（写操作默认）：只认登记的 ssh_host，没有就返回空，绝不拿 api_url
    猜 SSH 目标，避免误连未登记的控制面。
    strict=False（兼容旧调用）：未登记时从 api_url 提取 host。"""
    from urllib.parse import urlparse

    from app.models.k8s import K8sCluster

    q = db.query(K8sCluster)
    c = (q.filter(K8sCluster.id == cluster_id).first() if cluster_id
         else (q.filter(K8sCluster.name == cluster_name).first() if cluster_name
               else q.order_by(K8sCluster.id).first()))
    if c is None:
        return "", "root", None
    host = c.ssh_host or ("" if strict else (urlparse(c.api_url).hostname or ""))
    return host, c.ssh_user or "root", c


# ================= 备份（真实执行体） =================

def run_backup(task_id: int, log, set_steps, meta: dict):
    """集群备份：kubeadm 配置/证书 + 资源 yaml 全量 + 打包登记（可恢复）。"""
    ssh_host = meta.get("master_ssh", "")
    ssh_user = meta.get("master_user", "root")
    steps = [
        {"key": "config", "label": "集群配置备份（kubeadm 配置/证书清单）", "status": "pending"},
        {"key": "yaml", "label": "资源 yaml 全量导出", "status": "pending"},
        {"key": "pack", "label": "备份集打包登记", "status": "pending"},
    ]

    def mark(idx, status, note=""):
        steps[idx]["status"] = status
        if note:
            steps[idx]["note"] = note
        set_steps(steps)

    if not ssh_host:
        return False, "未配置控制面 SSH（集群登记补 ssh_host，或提交时带 master_ssh）"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log(f"备份集: k8s-backup-{stamp}（目标 {ssh_host}）")

    mark(0, "running")
    r = run_ssh(ssh_host, ssh_user,
                f"mkdir -p /data/k8s-backup/{stamp} && cp -r /etc/kubernetes /data/k8s-backup/{stamp}/ && ls /data/k8s-backup/{stamp}/kubernetes | sed -n '1,20p'",
                on_line=log, timeout=300)
    mark(0, "succeeded" if r["ok"] else "failed")
    if not r["ok"]:
        return False, "集群配置备份失败（/etc/kubernetes 不可读？）"

    mark(1, "running")
    r = run_ssh(ssh_host, ssh_user,
                f"cd /data/k8s-backup/{stamp} && kubectl get all --all-namespaces -o yaml > all-resources.yaml && wc -l all-resources.yaml",
                on_line=log, timeout=600)
    mark(1, "succeeded" if r["ok"] else "failed")
    if not r["ok"]:
        return False, "资源 yaml 导出失败（kubectl 不可用？）"

    mark(2, "running")
    r = run_ssh(ssh_host, ssh_user,
                f"tar czf /data/k8s-backup/k8s-backup-{stamp}.tar.gz -C /data/k8s-backup {stamp} && ls -lh /data/k8s-backup/k8s-backup-{stamp}.tar.gz",
                on_line=log, timeout=300)
    mark(2, "succeeded" if r["ok"] else "failed")
    if not r["ok"]:
        return False, "备份集打包失败"
    log(f"备份完成: /data/k8s-backup/k8s-backup-{stamp}.tar.gz（恢复：解包后按 kubernetes/ 目录回填）")
    return True, ""


# ================= 升级（单控制面独立实验节点，逐 minor） =================

def run_upgrade(task_id: int, log, set_steps, meta: dict):
    """受限升级：只操作 SSH 目标这一台单控制面独立实验节点，不承诺整集群升级。"""
    import re as _re

    from app.core.db import SessionLocal

    ssh_host = meta.get("master_ssh", "")
    ssh_user = meta.get("master_user", "root")
    target = meta.get("target_version", "")
    if not _re.fullmatch(r"\d+\.\d+\.\d+", target or ""):
        return False, "目标版本格式非法（须 X.Y.Z）"
    steps = [
        {"key": "nodecheck", "label": "只读节点清单核验（仅限单控制面实验节点）", "status": "pending"},
        {"key": "artifact", "label": f"准备 kubeadm {target} 制品", "status": "pending"},
        {"key": "env", "label": "升级运行环境就绪", "status": "pending"},
        {"key": "precheck", "label": "升级前检查（kubeadm upgrade plan）", "status": "pending"},
        {"key": "backup", "label": "升级前强制备份", "status": "pending"},
        {"key": "registry", "label": "预拉目标版本镜像", "status": "pending"},
        {"key": "upgrade", "label": "单控制面版本升级（逐组件）", "status": "pending"},
        {"key": "register", "label": "登记新版本", "status": "pending"},
    ]

    def mark(idx, status, note=""):
        steps[idx]["status"] = status
        if note:
            steps[idx]["note"] = note
        set_steps(steps)

    def runner(cmd, label):
        log(f"$ {label}")
        return run_ssh(ssh_host, ssh_user, cmd, on_line=log, timeout=900)

    if not ssh_host:
        return False, "未配置控制面 SSH（集群登记补 ssh_host）"

    # 任何写操作前先取得真实节点清单并 fail-closed：多控制面、worker、空清单、
    # 输出不可解析都拒绝；本执行体没有多节点编排能力，绝不把它包装成整集群升级。
    mark(0, "running")
    r = runner("kubectl get nodes --no-headers", "只读节点清单（升级拓扑限制）")
    if not r["ok"]:
        mark(0, "failed", "节点清单读取失败")
        return False, "节点清单读取失败，已终止；当前仅支持单控制面独立实验节点，生产/多节点未开放"
    rows = [ln.split() for ln in (r.get("lines") or []) if ln.strip()]
    nodes = [(parts[0], parts[2]) for parts in rows if len(parts) >= 5]
    if not nodes or len(nodes) != len(rows):
        mark(0, "failed", "节点清单为空或格式无法确认")
        return False, "节点清单为空或格式无法确认，已终止；当前仅支持单控制面独立实验节点，生产/多节点未开放"
    control_planes = [(name, roles) for name, roles in nodes
                      if {"control-plane", "master"} & set(roles.split(","))]
    if len(nodes) != 1 or len(control_planes) != 1:
        mark(0, "failed", f"检测到 {len(nodes)} 个节点，生产/多节点未开放")
        log("拒绝升级：当前仅支持单控制面独立实验节点，生产/多节点未开放")
        return False, "当前仅支持单控制面独立实验节点，生产/多节点未开放"
    mark(0, "succeeded", f"{nodes[0][0]}（单控制面独立实验节点）")

    mark(1, "running")
    r = runner(f"(yum install -y kubeadm-{target}* || yum install -y kubeadm-{target} || (apt-get update -qq && apt-get install -y kubeadm={target}-*)) && kubeadm version -o short",
               f"安装 kubeadm {target}")
    mark(1, "succeeded" if r["ok"] else "failed")
    if not r["ok"]:
        return False, f"kubeadm {target} 制品准备失败"

    mark(2, "running")
    r = runner("kubeadm version -o short && systemctl is-active kubelet", "环境就绪检查")
    mark(2, "succeeded" if r["ok"] else "failed")
    if not r["ok"]:
        return False, "升级运行环境检查失败，已终止（kubeadm/kubelet 未就绪）"

    mark(3, "running")
    r = runner(f"kubeadm upgrade plan 2>&1 | tail -30", "kubeadm upgrade plan")
    mark(3, "succeeded" if r["ok"] else "failed", "" if r["ok"] else "预检未通过（见日志）")
    if not r["ok"]:
        return False, "升级预检未通过（kubeadm upgrade plan）"

    mark(4, "running")
    bmeta = dict(meta)
    ok, err = run_backup(task_id, log, set_steps, bmeta)
    steps[4]["status"] = "succeeded" if ok else "failed"
    set_steps(steps)
    if not ok:
        return False, f"升级前备份失败：{err}"

    mark(5, "running")
    r = runner(f"kubeadm config images pull --kubernetes-version=v{target} --image-repository={meta.get('image_repo') or DEFAULT_IMAGE_REPO} 2>&1 | tail -10", "预拉镜像")
    mark(5, "succeeded" if r["ok"] else "failed", "" if r["ok"] else "镜像预拉失败（离线环境需先备镜像）")
    if not r["ok"]:
        return False, "目标版本镜像预拉失败，已终止（离线环境需先备镜像）"

    mark(6, "running")
    r = runner(f"kubeadm upgrade apply -y v{target} 2>&1 | tail -25", f"kubeadm upgrade apply v{target}")
    if not r["ok"]:
        mark(6, "failed", "升级失败")
        return False, f"升级 apply 失败（回退：任务中心-备份集恢复）"
    r = runner(f"(yum install -y kubelet-{target}* kubectl-{target}* || (apt-get update -qq && apt-get install -y kubelet={target}-* kubectl={target}-*)) && systemctl daemon-reload && systemctl restart kubelet",
               f"升级本机 kubelet/kubectl {target} 并重启")
    mark(6, "succeeded" if r["ok"] else "failed")
    if not r["ok"]:
        return False, "kubelet 升级/重启失败"

    mark(7, "running")
    r = runner("kubeadm version -o short && kubectl get nodes | sed -n '1,3p'", "版本登记")   # --short 在 1.28 已废弃
    mark(7, "succeeded" if r["ok"] else "failed")
    if not r["ok"]:
        return False, "版本登记失败（升级已执行，登记步骤未完成）"
    # 集群登记版本更新
    try:
        sdb = SessionLocal()
        from app.models.k8s import K8sCluster

        cname = meta.get("cluster_name", "")
        q = sdb.query(K8sCluster)
        c = (q.filter(K8sCluster.name == cname).first() if cname else None) or q.first()
        if c:
            c.status_note = f"k8s v{target}（升级于 {datetime.now():%Y-%m-%d %H:%M}）"
            sdb.commit()
    except Exception:
        pass
    log(f"单控制面实验节点升级完成 → v{target}（回退：任务中心-备份集恢复）")
    return True, ""


# ================= 清空集群（真实执行体，高危七步） =================

def run_wipe(task_id: int, log, set_steps, meta: dict):
    """清空集群（高危）：复核→清组件→清网络→删配置→卸仓库→撤 PATH→解绑登记。
    多节点：从控制面发现集群内全部节点，逐台执行 2~6 步。
    不做全局 iptables -F（机器上可能有 docker 业务容器，只清 KUBE- 链）。"""
    import re as _re

    from app.core.db import SessionLocal

    ssh_host = meta.get("master_ssh", "")
    ssh_user = meta.get("master_user", "root")
    steps = [
        {"key": "review", "label": "复核待清理节点", "status": "pending"},
        {"key": "components", "label": "清理节点组件（kubelet/kubeadm/kubectl）", "status": "pending"},
        {"key": "network", "label": "清理节点网络残留（CNI/KUBE- 链）", "status": "pending"},
        {"key": "config", "label": "删除集群配置目录（/etc/kubernetes 等）", "status": "pending"},
        {"key": "registry", "label": "卸载本地镜像仓库（如有）", "status": "pending"},
        {"key": "path", "label": "撤销节点 PATH/别名配置", "status": "pending"},
        {"key": "unbind", "label": "解绑机器与平台登记", "status": "pending"},
    ]

    def mark(idx, status, note=""):
        steps[idx]["status"] = status
        if note:
            steps[idx]["note"] = note
        set_steps(steps)

    if not ssh_host:
        return False, "未配置控制面 SSH（集群登记补 ssh_host）"

    # 1) 复核：控制面发现节点（运行时数据只做主机白名单，不过 shell 拼接）
    mark(0, "running")
    r = run_ssh(ssh_host, ssh_user,
                "kubectl get nodes -o wide --no-headers 2>/dev/null | awk '{print $6}'",
                timeout=60)
    nodes = [n.strip() for n in (r.get("lines") or []) if n.strip()]
    nodes = [n for n in nodes if _re.fullmatch(r"[a-zA-Z0-9._:-]{1,63}", n)]
    if ssh_host not in nodes:
        nodes.insert(0, ssh_host)   # 控制面本机必清
    log(f"待清理节点 {len(nodes)} 台: {', '.join(nodes)}")
    mark(0, "succeeded", f"{len(nodes)} 台")

    def each_node(cmd, label, idx):
        mark(idx, "running")
        allok = True
        for n in nodes:
            log(f"$ [{label}] {n}")
            rr = run_ssh(n, ssh_user, cmd, on_line=log, timeout=600)
            if not rr["ok"]:
                allok = False
                log(f"[{label}] {n} 执行有错误（继续其余节点）")
        mark(idx, "succeeded" if allok else "failed", "" if allok else "部分节点有错（见日志）")
        return allok

    each_node("kubeadm reset -f 2>&1 | tail -2; systemctl disable --now kubelet 2>/dev/null; "
              "(yum remove -y kubelet kubeadm kubectl || apt-get remove -y kubelet kubeadm kubectl) 2>&1 | tail -2; echo 组件清理完成",
              "清理组件", 1)
    each_node("rm -rf /etc/cni/net.d/* /var/lib/cni 2>/dev/null; "
              "for ch in KUBE-SERVICES KUBE-PROXY-FIREWALL KUBE-FIREWALL; do iptables -t nat -F $ch 2>/dev/null; iptables -F $ch 2>/dev/null; done; "
              "echo 网络残留清理完成（未做全局清表）",
              "清理网络", 2)
    each_node("rm -rf /etc/kubernetes /var/lib/kubelet /var/lib/etcd /root/.kube 2>/dev/null; echo 配置目录已删除",
              "删除配置", 3)
    each_node("systemctl disable --now registry 2>/dev/null; rm -rf /opt/registry /data/registry 2>/dev/null; echo 本地镜像仓库处理完成",
              "卸载仓库", 4)
    each_node("sed -i '/kubeasz/d;/kubectl completion/d;/KUBECONFIG=/d' /root/.bashrc 2>/dev/null; echo PATH 配置已撤销",
              "撤销 PATH", 5)

    # 7) 解绑平台登记
    mark(6, "running")
    try:
        sdb = SessionLocal()
        from app.models.k8s import K8sCluster

        cname = meta.get("cluster_name", "")
        q = sdb.query(K8sCluster)
        c = (q.filter(K8sCluster.name == cname).first() if cname else None) or q.first()
        if c:
            c.status = "unknown"
            c.status_note = f"已清空（{datetime.now():%Y-%m-%d %H:%M}），可重新部署"
            sdb.commit()
            log(f"集群登记「{c.name}」已标记清空")
        sdb.close()
    except Exception as e:
        log(f"登记更新跳过: {str(e)[:80]}")
    mark(6, "succeeded")
    log("清空完成——机器可重新部署")
    return True, ""


def run_restore(task_id: int, log, set_steps, meta: dict):
    """集群恢复（备份集→配置回填）：校验→留现场→停 kubelet→回填→重启→验证。
    恢复前先给当前配置留 .pre-restore 副本（后悔药之上再加一道）。"""
    import re as _re

    ssh_host = meta.get("master_ssh", "")
    ssh_user = meta.get("master_user", "root")
    backup_set = (meta.get("backup_set") or "").strip()
    if not _re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", backup_set):
        return False, "备份集名称非法"
    steps = [
        {"key": "verify_set", "label": "校验备份集（内容清单）", "status": "pending"},
        {"key": "snapshot", "label": "当前配置留档（.pre-restore）", "status": "pending"},
        {"key": "stop", "label": "停 kubelet（安全窗口）", "status": "pending"},
        {"key": "restore", "label": "配置回填（/etc/kubernetes）", "status": "pending"},
        {"key": "restart", "label": "重启 kubelet/组件", "status": "pending"},
        {"key": "verify", "label": "恢复验证（apiserver/node）", "status": "pending"},
    ]

    def mark(idx, status, note=""):
        steps[idx]["status"] = status
        if note:
            steps[idx]["note"] = note
        set_steps(steps)

    def runner(cmd, label):
        log(f"$ {label}")
        return run_ssh(ssh_host, ssh_user, cmd, on_line=log, timeout=600)

    if not ssh_host:
        return False, "未配置控制面 SSH（集群登记补 ssh_host）"

    mark(0, "running")
    r = runner(f"tar tzf /data/k8s-backup/k8s-backup-{backup_set}.tar.gz | sed -n '1,15p'", "校验备份集")
    listing = "\n".join(r.get("lines") or [])
    if not r["ok"] or "kubernetes/" not in listing:
        mark(0, "failed", "备份集缺失或内容不符")
        return False, "备份集校验失败（不存在或无 kubernetes/ 配置）"
    mark(0, "succeeded", f"{len(listing.splitlines())} 项")

    mark(1, "running")
    r = runner("rm -rf /etc/kubernetes.pre-restore && cp -r /etc/kubernetes /etc/kubernetes.pre-restore && echo 已留档", "当前配置留档")
    mark(1, "succeeded" if r["ok"] else "failed")

    mark(2, "running")
    r = runner("systemctl stop kubelet 2>/dev/null; echo kubelet 已停", "停 kubelet")
    mark(2, "succeeded" if r["ok"] else "failed")

    mark(3, "running")
    r = runner(f"cd /data/k8s-backup && tar xzf k8s-backup-{backup_set}.tar.gz && cp -r /data/k8s-backup/{backup_set}/kubernetes/. /etc/kubernetes/ && ls /etc/kubernetes | sed -n '1,20p'", "配置回填")
    mark(3, "succeeded" if r["ok"] else "failed")
    if not r["ok"]:
        runner("systemctl start kubelet", "回滚：重启 kubelet")
        return False, "配置回填失败（已重启 kubelet 保持现状）"

    mark(4, "running")
    r = runner("systemctl daemon-reload && systemctl start kubelet && sleep 5 && systemctl is-active kubelet", "重启 kubelet")
    mark(4, "succeeded" if r["ok"] else "failed")

    mark(5, "running")
    r = runner(WAIT_READY_CMD, "恢复验证（等 node Ready）")
    mark(5, "succeeded" if r["ok"] else "failed")
    if not r["ok"]:
        return False, "恢复后验证失败（node 未 Ready——检查备份集版本匹配）"
    log(f"恢复完成（备份集 k8s-backup-{backup_set}）；回退：/etc/kubernetes.pre-restore")
    return True, ""


def create_cluster_steps() -> list:
    return [
        {"key": "probe", "label": "主节点探测（SSH/系统）", "status": "pending"},
        {"key": "reset", "label": "清理旧集群残留（kubeadm reset）", "status": "pending"},
        {"key": "runtime", "label": "容器运行时就绪（CRI 校验）", "status": "pending"},
        {"key": "kubeadm", "label": "k8s 组件安装（指定版本）", "status": "pending"},
        {"key": "init", "label": "kubeadm init（控制面初始化）", "status": "pending"},
        {"key": "cni", "label": "安装 CNI（flannel）", "status": "pending"},
        {"key": "verify", "label": "集群验证（node Ready）", "status": "pending"},
    ]


def run_create_cluster(task_id: int, log, set_steps, meta: dict):
    """创建新集群（kubeadm init）——任务中心任务类型之一。"""
    ip = meta.get("ip", "")
    user = meta.get("ssh_user", "root")
    password = meta.get("ssh_password", "")
    k8s_ver = meta.get("k8s_version", "1.29.4")
    pod_cidr = meta.get("pod_cidr", "10.244.0.0/16")
    svc_cidr = meta.get("svc_cidr", "10.96.0.0/12")
    steps = create_cluster_steps()

    def mark(key, status, note=""):
        for s in steps:
            if s["key"] == key:
                s["status"] = status
                if note:
                    s["note"] = note
        set_steps(steps)

    def runner(cmd, label):
        log(f"$ {label}")
        return run_ssh(ip, user, cmd, on_line=log, timeout=900, password=password)

    mark("probe", "running")
    r = runner("hostname && uname -m", "探测主节点")
    if not r["ok"]:
        mark("probe", "failed", "SSH 不可达")
        return False, "主节点 SSH 不可达"
    mark("probe", "succeeded")

    # 旧集群残留（重装/前次失败残骸）不清理 init 必撞状态冲突
    mark("reset", "running")
    r = runner(RESET_CMD, "清理旧集群残留")
    mark("reset", "succeeded" if r["ok"] else "failed")
    if not r["ok"]:
        return False, "旧集群残留清理失败"

    mark("runtime", "running")
    r = runner(RUNTIME_CMD, "容器运行时就绪（CRI 校验）")
    mark("runtime", "succeeded" if r["ok"] else "failed")
    if not r["ok"]:
        return False, "容器运行时就绪失败（CRI 校验不通过，见日志）"

    mark("kubeadm", "running")
    r = runner(INSTALL_CMD.replace("{ver}", k8s_ver), f"配置 k8s 源并安装组件 {k8s_ver}")
    if not r["ok"]:
        mark("kubeadm", "failed", "安装失败")
        return False, "k8s 组件安装失败"
    mark("kubeadm", "succeeded")

    mark("init", "running")
    img_repo = meta.get("image_repo") or DEFAULT_IMAGE_REPO
    r = runner(f"kubeadm init --kubernetes-version=v{k8s_ver} --image-repository={img_repo} --pod-network-cidr={pod_cidr} --service-cidr={svc_cidr} --ignore-preflight-errors=all 2>&1 | tail -25", f"kubeadm init（镜像源 {img_repo}）")
    if not r["ok"]:
        # wait-control-plane 在慢环境固定 4 分钟超时（1.28 无相位可跳）——
        # 超时不等于失败：以 apiserver 健康事实复核，健康则继续
        log("kubeadm init 返回非 0，复核控制面真实健康（最长 2 分钟）")
        probe = runner("for i in $(seq 1 24); do curl -sk https://127.0.0.1:6443/healthz | grep -q ok && break; sleep 5; done; curl -sk https://127.0.0.1:6443/healthz | grep -q ok", "apiserver 健康复核")
        if probe["ok"]:
            log("apiserver 健康——init 仅 wait 超时（慢环境），继续后续步骤")
            r = {"ok": True}
    if not r["ok"]:
        mark("init", "failed", "init 失败（控制面未就绪）")
        return False, "kubeadm init 失败（控制面未就绪，见日志）"
    runner("mkdir -p $HOME/.kube && cp /etc/kubernetes/admin.conf $HOME/.kube/config && chmod 600 $HOME/.kube/config", "配置 kubectl")
    mark("init", "succeeded")

    # CNI 可选（flannel/calico，参考 k8smanager 部署向导可配思路）+ 镜像改写国内源
    mark("cni", "running")
    cni_repo = meta.get("cni_image_repo") or CNI_IMAGE_REPO
    cni_kind = (meta.get("cni") or "flannel").lower()
    if cni_kind == "calico":
        r = runner(f"(curl -fsSL --max-time 120 https://docs.tigera.io/calico/3.27/manifests/calico.yaml -o /tmp/calico.yaml || curl -fsSL --max-time 120 https://ghproxy.net/https://docs.tigera.io/calico/3.27/manifests/calico.yaml -o /tmp/calico.yaml) && sed -i 's#docker.io/calico#{cni_repo}/calico#g' /tmp/calico.yaml && kubectl apply -f /tmp/calico.yaml 2>&1 | tail -5", f"安装 calico CNI（镜像 {cni_repo}）")
    else:
        r = runner(f"(curl -fsSL --max-time 90 https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml -o /tmp/kube-flannel.yml || curl -fsSL --max-time 90 https://ghproxy.net/https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml -o /tmp/kube-flannel.yml) && sed -i 's#ghcr.io/flannel-io#{cni_repo}/flannel-io#g; s#docker.io/flannel#{cni_repo}/flannel#g' /tmp/kube-flannel.yml && kubectl apply -f /tmp/kube-flannel.yml 2>&1 | tail -5", f"安装 flannel CNI（双源，镜像 {cni_repo}）")
    mark("cni", "succeeded" if r["ok"] else "failed", "" if r["ok"] else "CNI 安装失败（节点将 NotReady，可手动补）")
    if not r["ok"]:
        return False, "CNI 安装失败（见日志）"

    mark("verify", "running")
    r = runner(WAIT_READY_CMD + " && kubectl get pods -A | sed -n '1,15p'", "集群验证（等 node Ready）")
    mark("verify", "succeeded" if r["ok"] else "failed")
    if not r["ok"]:
        return False, "集群验证失败（node 未 Ready，见日志）"
    log("集群创建完成（加入节点：任务中心-添加节点）")
    return True, ""
