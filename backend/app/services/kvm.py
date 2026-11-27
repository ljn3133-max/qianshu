"""KVM/libvirt 探活：libvirt URI 连通测试取版本。

产品口径：探活=取到 libvirt/hypervisor 版本才算通。虚拟机清单采集适配器排期中——
探活是真实现（libvirt 只读连接 / virsh version），不假装采集能力。

探活链（只读）：
  1) python libvirt 模块：libvirt.openReadOnly(uri)（只读连接，零变更面）
     -> getLibVersion()/getVersion() 取 libvirt 与 hypervisor 版本
  2) virsh 兜底：virsh -c <uri> version（本机装有 libvirt-clients 时）
  3) 客户端缺失 -> 明确报缺什么（不装死、不假装连通）

URI 形态（表单「libvirt URI」直接填）：
  qemu:///system                     本机 KVM
  qemu+ssh://user@host/system        SSH 隧道（推荐，密钥认证；账号密码随 SSH 体系走）
  qemu+tcp://host:16509/system       TCP 直连（libvirtd 监听 + SASL 认证）
  esx://host/sdk                     ESXi 裸连（vCenter 场景走 ESXi/vCenter 接入）
"""
from __future__ import annotations

import logging
import re
import subprocess

logger = logging.getLogger("qianshu.kvm")

_VER_RE = re.compile(r"\b(\d+\.\d+(?:\.\d+){0,2})\b")


def _normalize_uri(uri: str) -> str:
    """裸主机/IP -> 默认 qemu+ssh://<host>/system（教程口径），带 scheme 原样透传。"""
    raw = (uri or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        return raw
    return f"qemu+ssh://{raw}/system"


def _fmt_version(n: int) -> str:
    """libvirt getLibVersion 的 1000000*大+1000*中+小 形态 -> x.y.z。"""
    if not n:
        return ""
    return f"{n // 1000000}.{(n // 1000) % 1000}.{n % 1000}"


def _probe_with_libvirt(uri: str) -> dict:
    import libvirt  # noqa: PLC0415  仅探活路径需要，缺模块走 virsh 兜底

    conn = libvirt.openReadOnly(uri)   # 只读连接：无任何变更权限面
    try:
        lib_ver = _fmt_version(conn.getLibVersion())
        hyper = ""
        try:
            hyper = _fmt_version(conn.getVersion())
        except Exception:  # 非 KVM hypervisor 可能不支持
            hyper = ""
        hv = ""
        try:
            hv = conn.getType() or ""
        except Exception:
            hv = ""
        parts = [f"libvirt {lib_ver}" if lib_ver else ""]
        if hyper:
            parts.append(f"hypervisor {hyper}")
        if hv:
            parts.append(hv)
        return {"ok": True, "version": lib_ver,
                "detail": " · ".join(p for p in parts if p) or "连通"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _probe_with_virsh(uri: str) -> dict:
    r = subprocess.run(
        ["virsh", "-c", uri, "version"],
        capture_output=True, text=True, timeout=10,
    )
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise RuntimeError(out.strip()[:200] or f"virsh 退出码 {r.returncode}")
    # 形态：Using library: libvirt 9.0.0 / Running hypervisor: QEMU 7.2.0
    lib_m = re.search(r"library:\s*libvirt\s+([\d.]+)", out, re.I)
    hv_m = re.search(r"hypervisor:\s*(\S+)\s+([\d.]+)", out, re.I)
    ver = lib_m.group(1) if lib_m else ""
    detail_bits = [f"libvirt {ver}" if ver else ""]
    if hv_m:
        detail_bits.append(f"{hv_m.group(1)} {hv_m.group(2)}")
    if not ver and not hv_m:
        picked = _VER_RE.findall(out)
        ver = picked[-1] if picked else ""
        detail_bits = [f"版本 {ver}" if ver else out.strip()[:80]]
    return {"ok": True, "version": ver,
            "detail": " · ".join(p for p in detail_bits if p)}


def probe_kvm(uri: str) -> dict:
    """KVM/libvirt 探活取版本。uri 支持完整 libvirt URI 或裸主机（默认 qemu+ssh）。

    返回 {ok, detail, version}；ok=True 仅当取到 libvirt/hypervisor 版本。
    """
    full = _normalize_uri(uri)
    if not full:
        return {"ok": False, "version": "", "detail": "缺 libvirt URI（如 qemu+ssh://user@host/system）"}
    try:
        return _probe_with_libvirt(full)
    except ImportError:
        pass   # 本机无 python libvirt 模块 -> virsh 兜底
    except Exception as e:
        msg = str(e)[:160]
        if "requires authentication" in msg.lower() or "auth" in msg.lower():
            return {"ok": False, "version": "",
                    "detail": f"连接需要认证（SASL/TCP）：{msg}——探活仅支持免认证/SSH 密钥通道"}
        return {"ok": False, "version": "", "detail": f"libvirt 连接失败：{msg}"}
    try:
        return _probe_with_virsh(full)
    except FileNotFoundError:
        return {"ok": False, "version": "",
                "detail": "千枢主机缺 libvirt 客户端（libvirt-python 或 virsh）——"
                          "安装 libvirt-clients 后重试；本期支持登记与连通测试，采集能力随版本更新自动启用"}
    except Exception as e:
        return {"ok": False, "version": "", "detail": f"virsh 连通失败：{str(e)[:160]}"}
