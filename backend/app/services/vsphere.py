"""vCenter/ESXi 探活 + pyvmomi SDK 只读采集。

产品口径：探活=取到版本/系统信息才算通。探活用免认证 HTTP 端点取真实版本字符串，
不假装采集能力。

探活链（只读，全部免认证）：
  1) https://{host}/sdk/vimServiceVersions.xml  —— 官方免认证端点，列 API 版本
  2) https://{host}/                             —— 登录页文案（"VMware ESXi 7.0.3" 等）
  3) http 兜底（老环境 http 直开）

SDK 采集（pyvmomi 只读）：
  - list_vms：虚拟机清单（vim.VirtualMachine 容器视图，只读属性）
  - fetch_perf：基础指标（cpu.usage.average / mem.usage.average / disk.used.latest，
    perfManager.QueryPerf 取最新样本）
  - format_metric_lines：落 VictoriaMetrics 的 Prometheus 文本行
    （vmware_vm_cpu_usage_percent / vmware_vm_mem_usage_percent / vmware_vm_disk_used_bytes）
  主机级（ESXi 物理机，与 VM 同 seam）：
  - list_hosts / collect_host_inventory：HostSystem 清单（product='vmware_host' 入库，
    清扫守卫同款，provider.sync_vmware_hosts）
  - 同套 perf counter 取主机 CPU/内存/磁盘 -> vmware_host_cpu_usage_percent /
    vmware_host_mem_usage_percent / vmware_host_disk_used_bytes（format_metric_lines prefix）
  🔑 指标口径（百分比计数器 v/100、disk.used.latest 单位 KB）按 vSphere 公开文档，
  未经真实 vCenter 实测（无环境红线）——接真实环境后核一遍数值量纲。
  pyvmomi 未安装时给出明确缺件提示（同 kvm.py 客户端缺失口径），不假装采集成功。

SSL 校验：vCenter/ESXi 默认自签证书——verify 参数
贯通到底层 httpx；账号 extra.ssl_skip=true（表单「跳过 SSL 校验（自签证书）」默认开）
则 verify=False；关闭跳过时证书校验失败会给出人话提示（指引回开开关）。
SDK 通道同口径：ssl_skip -> sslContext 不校验。
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager

logger = logging.getLogger("qianshu.vsphere")

_VER_RE = re.compile(r"\b(\d+\.\d+(?:\.\d+){0,2})\b")


def _fetch(url: str, timeout: int = 8, verify: bool = False) -> "object":
    import httpx

    return httpx.get(url, timeout=timeout, verify=verify, follow_redirects=True,
                     headers={"User-Agent": "qianshu-datasource-probe"})


def _is_ssl_verify_error(err: str) -> bool:
    """证书校验失败特征（自签证书场景）：转成人话提示。"""
    e = err.lower()
    return ("certificate" in e or "ssl" in e or "tls" in e or "self-signed" in e
            or "verify failed" in e)


def _parse_service_versions(xml_text: str) -> str:
    """vimServiceVersions.xml：<version>X.Y.Z</version> 与 version="urn:vim25" 形态都收。"""
    vers = re.findall(r"<version>([^<]+)</version>", xml_text or "")
    vers += re.findall(r'urn:vim(\d+)', xml_text or "")
    picked = [v.strip() for v in vers if _VER_RE.fullmatch(v.strip())]
    if not picked:
        return ""
    return sorted(picked, key=lambda s: [int(x) for x in s.split(".")])[-1]


def _parse_landing(text: str) -> tuple:
    """登录页文案取（产品名, 版本）：VMware ESXi / VMware vCenter Server / VMware vSphere。"""
    plain = re.sub(r"<[^>]+>", " ", text or "")
    product = ""
    for name in ("VMware vCenter Server", "VMware ESXi", "VMware vSphere", "VMware"):
        if name.lower() in plain.lower():
            product = name
            break
    m = re.search(r"(?:ESXi|vCenter Server|vSphere)\s*(?:version\s*)?(\d+\.\d+(?:\.\d+){0,2})", plain, re.I)
    version = m.group(1) if m else ""
    return product, version


def probe_vsphere(host: str, verify: bool = False) -> dict:
    """vCenter/ESXi 探活取版本。host 支持 IP/域名（可带 https:// 前缀，自动剥掉）。

    verify：HTTPS 证书校验（False=跳过，适配 vCenter 默认自签证书；True=严格校验）。
    返回 {ok, detail, version, product}；ok=True 仅当取到版本或产品标识（不止端口通）。
    """
    raw = (host or "").strip()
    raw = re.sub(r"^https?://", "", raw).rstrip("/")
    if not raw:
        return {"ok": False, "detail": "缺主机地址", "version": "", "product": ""}
    last_err = ""
    ssl_verify_failed = False
    for scheme in ("https", "http"):
        # http 兜底不受 HTTPS 证书校验影响（无证书），verify 只约束 https 链路
        v = verify if scheme == "https" else False
        # 1) vimServiceVersions.xml（免认证）
        try:
            r = _fetch(f"{scheme}://{raw}/sdk/vimServiceVersions.xml", verify=v)
            if r.status_code == 200 and "<" in r.text:
                ver = _parse_service_versions(r.text)
                if ver:
                    return {"ok": True, "version": ver, "product": "vSphere",
                            "detail": f"版本 {ver}（vimServiceVersions）· 本期支持登记与连通测试，采集能力随版本更新自动启用"}
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:60]}"
            ssl_verify_failed = ssl_verify_failed or (v and _is_ssl_verify_error(str(e)))
        # 2) 登录页文案
        try:
            r = _fetch(f"{scheme}://{raw}", verify=v)
            if r.status_code == 200:
                product, ver = _parse_landing(r.text)
                if ver or product:
                    return {"ok": True, "version": ver, "product": product or "vSphere",
                            "detail": f"版本 {ver or '未知'}（{product or 'VMware'} 登录页）"
                                      f"· 本期支持登记与连通测试，采集能力随版本更新自动启用"}
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:60]}"
            ssl_verify_failed = ssl_verify_failed or (v and _is_ssl_verify_error(str(e)))
    detail = f"未取到版本信息（{last_err or 'HTTP 无有效响应'}）"
    if ssl_verify_failed:
        detail += " · SSL 证书校验失败（多为自签证书）——请在账号表单开启「跳过 SSL 校验（自签证书）」后重试"
    return {"ok": False, "version": "", "product": "", "detail": detail}


# ---------------- vSphere SDK 只读采集（pyvmomi） ----------------

class VSphereError(RuntimeError):
    pass


def _conn_params(account) -> dict:
    """账号表单 -> 连接参数：extra.host（vCenter/ESXi 地址）/ extra.port / extra.ssl_skip。"""
    import json as _json

    try:
        extra = _json.loads(account.extra_json) if account.extra_json else {}
    except ValueError:
        extra = {}
    host = str(extra.get("host") or "").strip() or (account.region or "").strip()
    host = re.sub(r"^https?://", "", host).rstrip("/")
    return {
        "host": host,
        "port": int(extra.get("port") or 443),
        "verify_ssl": not extra.get("ssl_skip", True),
        "datacenter": str(extra.get("datacenter") or "").strip(),
    }


@contextmanager
def session(host: str, user: str = "", password: str = "", port: int = 443,
            verify_ssl: bool = False):
    """pyvmomi 只读连接 -> content（service instance 的 RetrieveContent）。

    只读语义：仅做属性读取与 QueryPerf 查询，零变更面。seam：mock 测试替换本函数。
    """
    try:
        from pyVim.connect import Disconnect, SmartConnect  # pyvmomi 包内模块名是 pyVim（大小写敏感）
        from pyVmomi import vim  # noqa: F401  依赖面校验：两个包同属 pyvmomi 发行版
    except ImportError as e:
        raise VSphereError(
            "缺 pyvmomi 模块（pip install pyvmomi）——vSphere SDK 采集不可用；"
            "指标可先用 Agent/node_exporter 兜底，资产暂维持登记+探活"
        ) from e
    import ssl as _ssl

    ctx = None if verify_ssl else _ssl._create_unverified_context()
    si = SmartConnect(host=host, user=user, pwd=password, port=port, sslContext=ctx)
    try:
        yield si.RetrieveContent()
    finally:
        try:
            Disconnect(si)
        except Exception:  # 断连失败不影响结果
            logger.debug("Disconnect 失败（忽略）", exc_info=True)


def _vm_guest_ip(vm) -> str:
    """Guest 主 IP（VMware Tools 上报）；取不到返回空，不猜。"""
    guest = getattr(vm, "guest", None)
    ip = _s(getattr(guest, "ipAddress", "")).strip()
    if ip:
        return ip
    for nic in (getattr(guest, "net", None) or []):
        for cand in (getattr(nic, "ipAddress", None) or []):
            cand = _s(cand).strip()
            if cand and not cand.startswith("127.") and ":" not in cand:
                return cand
    return ""


def _vm_disks(vm) -> list:
    """虚拟机磁盘清单（只读）：容量取 config.hardware.device 的 capacityInKB。

    资产清单要回答"这台机器硬盘多大"，不能只依赖性能指标里的已用量；
    单块盘取不到就跳过，不让坏数据拖垮整轮。
    """
    out = []
    try:
        from pyVmomi import vim
        cfg = getattr(vm, "config", None)
        for dev in (getattr(getattr(cfg, "hardware", None), "device", None) or []):
            if not isinstance(dev, vim.vm.device.VirtualDisk):
                continue
            try:
                kb = int(getattr(dev, "capacityInKB", 0) or 0)
            except (TypeError, ValueError):
                continue
            if kb <= 0:
                continue
            out.append({
                "label": _s(getattr(dev, "deviceInfo", None)
                            and getattr(dev.deviceInfo, "label", "")),
                "size_gb": round(kb / 1024 / 1024, 1),
                "type": "disk",
            })
    except Exception:
        logger.debug("读取 VM 磁盘清单失败（忽略）", exc_info=True)
    return out


def _vm_row(vm, with_details: bool = True) -> dict:
    """单台虚拟机只读属性 -> 统一清单行。属性取不到就留空，不让单台坏数据拖垮整轮。

    with_details=False 用于 5 分钟指标采集轮：只取指标标签要的 name/instance_id，
    不额外拉 guest/hardware.device（vSphere 惰性属性，逐台访问会增加往返耗时）。
    """
    summary = getattr(vm, "summary", None)
    config = getattr(summary, "config", None) or getattr(vm, "config", None)
    runtime = getattr(summary, "runtime", None)

    power = _s(getattr(runtime, "powerState", ""))   # poweredOn / poweredOff / suspended
    status = {"poweredOn": "running", "poweredOff": "stopped", "suspended": "suspended"}.get(power, power)

    host_name = ""
    try:
        host_name = _s(getattr(getattr(runtime, "host", None), "name", ""))
    except Exception:  # MOR 取属性可能失败（权限/断连）——主机名留空
        host_name = ""

    instance_uuid = ""
    try:
        instance_uuid = _s(getattr(vm.config, "instanceUuid", "")) if getattr(vm, "config", None) else ""
    except Exception:
        instance_uuid = ""

    disks = _vm_disks(vm) if with_details else []
    disk_total_gb = round(sum(d["size_gb"] for d in disks), 1) if disks else 0
    guest_ip = _vm_guest_ip(vm) if with_details else ""
    # IP 只能由 VMware Tools 上报；Tools 未运行时 vCenter 没有 IP 可读（UI 需说明原因）
    tools_status = (_s(getattr(getattr(vm, "guest", None),
                               "toolsRunningStatus", "")) if with_details else "")
    mo_id = _s(getattr(vm, "_moId", ""))
    return {
        "mo_id": mo_id,
        "instance_id": instance_uuid or mo_id,
        "name": _s(getattr(vm, "name", "")) or _s(getattr(config, "name", "")),
        "status": status,
        "cpu_count": int(getattr(config, "numCpu", 0) or 0),
        "memory_mb": int(getattr(config, "memorySizeMB", 0) or 0),
        "guest_os": _s(getattr(config, "guestFullName", "")),
        "host_name": host_name,
        "guest_ip": guest_ip,
        "disk_total_gb": disk_total_gb,
        "disk_count": len(disks),
        "disks": disks,
        "extra": {"power_state": power, "instance_uuid": instance_uuid, "mo_id": mo_id,
                  "guest_ip": guest_ip, "tools_status": tools_status,
                  "disk_total_gb": disk_total_gb, "disk_count": len(disks)},
    }


def _s(v) -> str:
    return str(v) if v is not None else ""


def list_vm_refs(content, with_details: bool = False) -> list:
    """虚拟机清单（只读）：[(mo_id, vm 句柄, row)]——指标采集要句柄，资产同步只要 row。

    默认不带磁盘/guest IP（指标轮不为明细付额外往返）；资产同步显式 with_details=True。
    """
    from pyVmomi import vim

    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    try:
        out = []
        for vm in (getattr(view, "view", None) or []):
            row = _vm_row(vm, with_details=with_details)
            out.append((row["mo_id"], vm, row))
        return out
    finally:
        try:
            view.Destroy()
        except Exception:
            logger.debug("Destroy 容器视图失败（忽略）", exc_info=True)


def list_vms(content) -> list:
    """虚拟机资产清单（只读）：含磁盘/guest IP 等详情。"""
    return [row for _mo_id, _vm, row in list_vm_refs(content, with_details=True)]


def _host_mgmt_ip(host) -> str:
    """ESXi 管理地址：优先取管理 vmkernel 网卡 IP，取不到再判断 host.name 是否 IP。"""
    try:
        cfg = getattr(host, "config", None)
        for vnic in (getattr(getattr(cfg, "network", None), "vnic", None) or []):
            ip = _s(getattr(getattr(vnic, "spec", None), "ipAddress", "")).strip()
            if ip:
                return ip
    except Exception:
        logger.debug("读取 ESXi 管理 IP 失败（忽略）", exc_info=True)
    name = _s(getattr(host, "name", "")).strip()
    try:
        import ipaddress
        ipaddress.ip_address(name)
        return name
    except ValueError:
        return ""


def _host_datastores(host) -> dict:
    """主机可访问存储汇总（只读）：容量/剩余 GB。共享存储按 datastore 去重，不按主机重复计。"""
    total = free = 0
    seen = set()
    try:
        for ds in (getattr(host, "datastore", None) or []):
            key = _s(getattr(ds, "_moId", "")) or _s(getattr(ds, "name", ""))
            if not key or key in seen:
                continue
            seen.add(key)
            summary = getattr(ds, "summary", None)
            total += int(getattr(summary, "capacity", 0) or 0)
            free += int(getattr(summary, "freeSpace", 0) or 0)
    except Exception:
        logger.debug("读取主机存储汇总失败（忽略）", exc_info=True)
    return {
        "datastore_count": len(seen),
        "datastore_total_gb": round(total / 1024 / 1024 / 1024, 1),
        "datastore_free_gb": round(free / 1024 / 1024 / 1024, 1),
    }


def _host_row(host, with_details: bool = True) -> dict:
    """单台 ESXi 主机（HostSystem）只读属性 -> 统一清单行（与 _vm_row 同口径）。

    内存 API 单位是字节，统一行约定使用 MB，必须做单位换算；
    补 ESXi 版本、管理 IP、可访问存储容量；集群放 extra，不再占用 host_name。
    with_details=False 用于指标轮：不拉存储容量等资产明细。
    属性取不到就留空，不让单台坏数据拖垮整轮。
    """
    summary = getattr(host, "summary", None)
    hardware = getattr(summary, "hardware", None) or getattr(host, "hardware", None)
    runtime = getattr(summary, "runtime", None)

    conn = _s(getattr(runtime, "connectionState", ""))   # connected/disconnected/notResponding
    status = {"connected": "running", "disconnected": "disconnected",
              "notResponding": "not_responding"}.get(conn, conn)

    host_name = _s(getattr(host, "name", ""))
    cluster = ""
    try:
        pname = _s(getattr(getattr(host, "parent", None), "name", ""))
        # 独立 ESXi 的 parent 是 ComputeResource，名字=主机名/IP；那不是"集群"，
        # 写进所属集群会出现"集群=自己的 IP"的假信息。
        if pname and pname != host_name:
            cluster = pname
    except Exception:   # MOR 取属性可能失败（权限/断连）——集群名留空
        cluster = ""

    uuid = ""
    try:
        uuid = _s(getattr(hardware, "uuid", ""))
    except Exception:
        uuid = ""

    # hardware.memorySize 单位是字节；统一行约定 memory_mb 用 MB
    try:
        memory_mb = int(getattr(hardware, "memorySize", 0) or 0) // (1024 * 1024)
    except (TypeError, ValueError):
        memory_mb = 0

    esxi_version = ""
    try:
        product = getattr(getattr(host, "config", None), "product", None)
        esxi_version = _s(getattr(product, "fullName", "")) or _s(getattr(product, "version", ""))
    except Exception:
        esxi_version = ""

    mgmt_ip = _host_mgmt_ip(host) if with_details else ""
    storage = (_host_datastores(host) if with_details
               else {"datastore_count": 0, "datastore_total_gb": 0, "datastore_free_gb": 0})
    mo_id = _s(getattr(host, "_moId", ""))
    return {
        "mo_id": mo_id,
        "instance_id": uuid or mo_id,
        "name": host_name,
        "status": status,
        "cpu_count": int(getattr(hardware, "numCpuCores", 0) or 0),
        "memory_mb": memory_mb,
        "guest_os": esxi_version,   # 主机没有 guest OS，用系统版本回答"这是什么系统"
        "host_name": "",            # 主机自身不是"跑在哪台宿主机上"，集群放 extra
        "mgmt_ip": mgmt_ip,
        "disk_total_gb": storage["datastore_total_gb"],
        "disk_count": storage["datastore_count"],
        "extra": {
            "connection_state": conn, "uuid": uuid, "mo_id": mo_id,
            "cluster": cluster, "esxi_version": esxi_version, "mgmt_ip": mgmt_ip,
            "cpu_mhz": int(getattr(hardware, "cpuMhz", 0) or 0),
            "cpu_model": _s(getattr(hardware, "cpuModel", "")),
            "vendor": _s(getattr(hardware, "vendor", "")),
            "model": _s(getattr(hardware, "model", "")),
            **storage,
        },
    }


def list_host_refs(content, with_details: bool = False) -> list:
    """主机清单（只读）：[(mo_id, host 句柄, row)]——指标采集要句柄，资产同步只要 row。"""
    from pyVmomi import vim

    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.HostSystem], True)
    try:
        out = []
        for host in (getattr(view, "view", None) or []):
            row = _host_row(host, with_details=with_details)
            out.append((row["mo_id"], host, row))
        return out
    finally:
        try:
            view.Destroy()
        except Exception:
            logger.debug("Destroy 容器视图失败（忽略）", exc_info=True)


def list_hosts(content) -> list:
    """主机资产清单（只读）：含存储容量等详情。"""
    return [row for _mo_id, _host, row in list_host_refs(content, with_details=True)]


# 基础指标口径：cpu/mem 用 average 百分比计数器，磁盘用虚拟盘已用空间
PERF_COUNTER_KEYS = {
    "cpu_usage_pct": ("cpu", "usage", "average"),
    "mem_usage_pct": ("mem", "usage", "average"),
    "disk_used_kb": ("disk", "used", "latest"),
}


def _pct(v) -> float:
    """vSphere 百分比计数器返回值是百分之一百分点（2500=25%）——/100 转真百分比。"""
    try:
        return float(v) / 100.0
    except (TypeError, ValueError):
        return 0.0


def _counter_ids(perf_manager) -> dict:
    """{(group,name,rollup): counterId} -> {alias: counterId}。"""
    by_key = {}
    for c in (getattr(perf_manager, "perfCounter", None) or []):
        try:
            by_key[(_s(getattr(c.groupInfo, "key", "")),
                    _s(getattr(c.nameInfo, "key", "")),
                    _s(getattr(c, "rollupType", "")))] = c.key
        except Exception:
            continue
    out = {}
    for alias, k in PERF_COUNTER_KEYS.items():
        if k in by_key:
            out[alias] = by_key[k]
    return out


# 最近一次 QueryPerf 的环境诊断（统计级别/实时档开关），供采集状态透传到 UI。
# vSphere 采集由 _vsphere_lock 串行，单进程内无并发覆盖问题。
_last_perf_diag: dict = {}


def reset_perf_diag() -> None:
    _last_perf_diag.clear()


def get_last_perf_diag() -> dict:
    return dict(_last_perf_diag)


def _perf_diag(pm, interval_id: int) -> dict:
    """只读读取 vCenter 性能统计配置，用于把"0 样本"归因到具体设置。

    vCenter 7/8 的 historicalInterval 只列 300/1800/7200/86400 四个历史档，
    20 秒实时档不会出现在该列表里。所以：
      - 若版本暴露了 20 秒档，stats_level 直接给实时档级别；
      - 否则给 hist_levels（历史档级别列表），全 0 即统计关闭；
      - 历史档 >0 而实时样本仍为空时，问题更可能在只读账号的性能读取权限。
    权限不足时只记录 error，不阻断采集。
    """
    diag: dict = {"interval_id": interval_id}
    try:
        intervals = getattr(pm, "historicalInterval", None) or []
        hist_levels, hist_enabled = [], []
        chosen = None
        for iv in intervals:
            try:
                sp = int(getattr(iv, "samplingPeriod", 0) or 0)
                key = int(getattr(iv, "key", 0) or 0)
                lv = int(getattr(iv, "level", -1))
                en = bool(getattr(iv, "enabled", False))
            except (TypeError, ValueError):
                continue
            hist_levels.append(lv)
            hist_enabled.append(en)
            if (sp == 20 or key == 20) and chosen is None:
                chosen = iv
        if chosen is not None:
            diag["stats_level"] = int(getattr(chosen, "level", -1))
            diag["interval_enabled"] = bool(getattr(chosen, "enabled", False))
            diag["sampling_period"] = int(getattr(chosen, "samplingPeriod", 0) or 0)
        if hist_levels:
            diag["hist_levels"] = hist_levels
            diag["hist_enabled"] = hist_enabled
    except Exception as e:  # 权限不足/版本差异：诊断失败不影响采集
        diag["error"] = f"{type(e).__name__}: {str(e)[:100]}"
    return diag


def _set_perf_diag(pm, interval_id: int) -> dict:
    diag = _perf_diag(pm, interval_id)
    _last_perf_diag.clear()
    _last_perf_diag.update(diag)
    return diag


def _result_series(res) -> list:
    """QueryPerf 结果里的指标序列，兼容 pyvmomi 新旧字段名。

    pyvmomi 8.0（vSphere 8 WSDL）把 PerfEntityMetric 的序列字段命名为
    ``value``；旧版本为 ``series``。只读其中一个会让所有查询都显示 0 条，
    例如 vCenter 6.7 搭配 pyvmomi 8.0.3 时数据在 value 里。
    """
    return (getattr(res, "series", None) or getattr(res, "value", None) or [])


def _latest_valid(series) -> float | None:
    """序列最后一个有效样本；vSphere 用 -1 表示"该采样点无数据"，跳过。"""
    vals = getattr(series, "value", None) or []
    for v in reversed(vals):
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv >= 0:
            return fv
    return None


def _probe_intervals(pm, vm_refs: list, ids: dict) -> dict:
    """0 样本时逐个统计档探测（只读）：{"20": n, "300": n, "1800": n}。

    n = 该档返回且至少有一个有效值(>=0)的序列数。全部为 0 时才是 vCenter
    侧性能数据未生成；有序列但值全为 -1 同样按无数据计。
    """
    from pyVmomi import vim

    out: dict = {}
    if not vm_refs:
        return out
    _mo_id, vm = vm_refs[0]
    mids_rt = [vim.PerformanceManager.MetricId(counterId=ids[k], instance="")
               for k in ("cpu_usage_pct", "mem_usage_pct") if ids.get(k)]
    if not mids_rt:
        return out
    for iv in (20, 300, 1800):
        # 磁盘 used.latest 只在 5 分钟及以上历史档存在；20 秒档不查它。
        mids = mids_rt
        if iv != 20 and ids.get("disk_used_kb"):
            mids = mids_rt + [vim.PerformanceManager.MetricId(
                counterId=ids["disk_used_kb"], instance="*")]
        try:
            res = pm.QueryPerf(querySpec=[vim.PerformanceManager.QuerySpec(
                entity=vm, metricId=mids, maxSample=1, intervalId=iv)])
            out[str(iv)] = sum(1 for r in (res or []) for s in _result_series(r)
                               if _latest_valid(s) is not None)
        except Exception as e:
            out[str(iv)] = f"err:{type(e).__name__}"
    return out


def fetch_perf(content, vm_refs: list, interval_id: int = 20) -> dict:
    """vm_refs=[(mo_id, vm_obj)] -> {mo_id: {cpu_usage_pct, mem_usage_pct, disk_used_kb}}。

    合并两个统计档：
      - 实时档（默认 20 秒）有 cpu.usage.average，部分装了 Tools 的 VM 也有 mem；
      - disk.used.latest 只在 5 分钟历史档采集，20 秒档查不到，必须单独查 300。
    结果字段兼容 pyvmomi 新旧命名（series/value）；-1 为"无数据"哨兵，跳过。
    只读查询。
    """
    from pyVmomi import vim

    reset_perf_diag()
    if not vm_refs:
        return {}
    pm = content.perfManager
    ids = _counter_ids(pm)
    if not ids:
        raise VSphereError("perfManager 未取到 cpu/mem/disk 计数器（权限不足或版本差异）")

    out: dict = {mo_id: {"cpu_usage_pct": None, "mem_usage_pct": None,
                         "disk_used_kb": 0.0}
                 for mo_id, _vm in vm_refs}

    def _query(keys: tuple, iv: int) -> list:
        mids = [vim.PerformanceManager.MetricId(
                    counterId=ids[k], instance="*" if k == "disk_used_kb" else "")
                for k in keys if ids.get(k)]
        if not mids:
            return []
        specs = [vim.PerformanceManager.QuerySpec(entity=vm, metricId=mids,
                                                  maxSample=1, intervalId=iv)
                 for _mo_id, vm in vm_refs]
        return pm.QueryPerf(querySpec=specs) or []

    def _merge(results: list) -> None:
        for res in results:
            entity = getattr(res, "entity", None)
            mo_id = _s(getattr(entity, "_moId", "")) if entity is not None else ""
            rec = out.get(mo_id)
            if rec is None:
                continue
            disk_parts, disk_total = [], []
            for series in _result_series(res):
                sid = getattr(series, "id", None)
                cid = getattr(sid, "counterId", None) if sid is not None else None
                val = _latest_valid(series)
                if val is None:
                    continue
                if cid == ids.get("cpu_usage_pct"):
                    rec["cpu_usage_pct"] = _pct(val)
                elif cid == ids.get("mem_usage_pct"):
                    rec["mem_usage_pct"] = _pct(val)
                elif cid == ids.get("disk_used_kb"):
                    # disk.used.latest 的 instance="" 已是全盘总量，分项
                    # (DISKFILE/SWAPFILE/...) 之和与它相等——两者都加会翻倍。
                    if getattr(sid, "instance", "") == "":
                        disk_total.append(val)
                    else:
                        disk_parts.append(val)
            if disk_total:
                rec["disk_used_kb"] += disk_total[0]
            elif disk_parts:
                rec["disk_used_kb"] += sum(disk_parts)

    results_rt = _query(("cpu_usage_pct", "mem_usage_pct"), interval_id)
    _merge(results_rt)
    results_disk = _query(("disk_used_kb",), 300)
    _merge(results_disk)
    results = results_rt + results_disk

    # QueryPerf 静默空返回时，页面只会看到"0 点"——落一条可行动的告警。
    if not results:
        diag = _set_perf_diag(pm, interval_id)
        logger.warning("vSphere QueryPerf 无返回（%d 实体, intervalId=%d, stats_level=%s, "
                       "hist_levels=%s）——检查 vCenter 性能统计级别与只读账号 "
                       "System.View 权限", len(vm_refs), interval_id,
                       diag.get("stats_level"), diag.get("hist_levels"))

    if all(r["cpu_usage_pct"] is None and r["mem_usage_pct"] is None
           for r in out.values()):
        _log_empty_perf(pm, results, vm_refs, ids, interval_id)
    return out


def _log_empty_perf(pm, results, vm_refs, ids, interval_id: int) -> None:
    """QueryPerf 有实体返回但 CPU/内存全空时落一条可行动的告警。

    "series=[]" 不一定代表 vCenter 无数据：pyvmomi 8.0 把序列字段命名为 value，
    只读 series 会丢弃所有值；必须经 _result_series 兼容两种字段。
    当前告警只用于真实的"三档均无有效样本"
    （统计级别为 0 / 权限不足 / vCenter 性能服务异常）。资产同步不受影响。
    """
    prov = ""
    try:
        if vm_refs:
            ps = pm.QueryPerfProviderSummary(entity=vm_refs[0][1])
            prov = (f"currentSupported={getattr(ps, 'currentSupported', None)}, "
                    f"refreshRate={getattr(ps, 'refreshRate', None)}")
    except Exception as e:
        prov = f"providerSummary_err={type(e).__name__}"
    diag = _set_perf_diag(pm, interval_id)
    probes = _probe_intervals(pm, vm_refs, ids)
    if probes:
        _last_perf_diag["probes"] = probes
        diag["probes"] = probes
    n_series = sum(len(_result_series(r)) for r in (results or []))
    if probes and all(isinstance(v, int) and v == 0 for v in probes.values()):
        hint = ("三个统计档均无有效样本——vCenter 侧性能数据未生成；"
                "请用同一账号在 vSphere Client 确认性能曲线，并检查 vpxd/perfcharts")
    else:
        hint = "请在 vCenter 检查「统计级别」是否 ≥1，以及账号性能读取权限"
    logger.warning("vSphere QueryPerf 无样本（实体=%d, series=%d, counters=%s, "
                   "interval=%d, stats_level=%s, hist_levels=%s, probes=%s, %s）——%s；"
                   "资产同步/拓扑不受影响",
                   len(vm_refs), n_series, ids, interval_id,
                   diag.get("stats_level"), diag.get("hist_levels"), probes, prov, hint)


def _esc(v) -> str:
    return _s(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def format_metric_lines(perf_rows: list, name_by_moid: dict, account_id: int = 0,
                        ts_ms: int = 0, prefix: str = "vmware_vm") -> list:
    """指标行（Prometheus 文本协议）：{prefix}_cpu_usage_percent / mem / disk_used_bytes。

    perf_rows=[{mo_id, instance_id, name, cpu_usage_pct, mem_usage_pct, disk_used_kb}]；
    disk.used.latest 单位 KB -> bytes。数值为 None 的指标跳过（不出假 0）。
    prefix：vmware_vm（虚拟机，默认）/ vmware_host（ESXi 主机）。
    """
    lines = []
    for r in perf_rows:
        labels = (f'instance_id="{_esc(r.get("instance_id"))}",'
                  f'name="{_esc(r.get("name") or name_by_moid.get(r.get("mo_id"), ""))}",'
                  f'account_id="{int(account_id or 0)}"')
        tail = f" {ts_ms}" if ts_ms else ""
        cpu, mem = r.get("cpu_usage_pct"), r.get("mem_usage_pct")
        if cpu is not None:
            lines.append(f"{prefix}_cpu_usage_percent{{{labels}}} {cpu:.2f}{tail}")
        if mem is not None:
            lines.append(f"{prefix}_mem_usage_percent{{{labels}}} {mem:.2f}{tail}")
        kb = r.get("disk_used_kb")
        if kb:
            lines.append(f"{prefix}_disk_used_bytes{{{labels}}} {kb * 1024:.0f}{tail}")
    return lines


def _account_conn(account, user: str, password: str) -> dict:
    p = _conn_params(account)
    if not p["host"]:
        raise VSphereError("缺主机地址（表单「主机地址」/ extra.host）")
    p.update({"user": user, "password": password})
    return p


def collect_inventory(account, user: str = "", password: str = "") -> list:
    """虚拟机清单（账号级入口，provider.sync 用）：rows=[{mo_id, instance_id, ...}]。"""
    p = _account_conn(account, user, password)
    with session(p["host"], p["user"], p["password"], p["port"], p["verify_ssl"]) as content:
        return list_vms(content)


def collect_host_inventory(account, user: str = "", password: str = "") -> list:
    """主机（HostSystem）清单（账号级入口，provider.sync_vmware_hosts 用）。

    与 collect_inventory 同 seam，只读属性。
    """
    p = _account_conn(account, user, password)
    with session(p["host"], p["user"], p["password"], p["port"], p["verify_ssl"]) as content:
        return list_hosts(content)


def collect_metrics_lines(account, user: str = "", password: str = "") -> list:
    """VM + 主机基础指标 -> Prometheus 文本行（metrics_job 用）。单 session 同轮取清单+指标。

    主机级（HostSystem）与 VM 同套 perf counter 各取一份——
    vmware_vm_* 与 vmware_host_* 两组指标行一并返回。
    """
    p = _account_conn(account, user, password)
    with session(p["host"], p["user"], p["password"], p["port"], p["verify_ssl"]) as content:
        vmr = list_vm_refs(content)
        hr = list_host_refs(content)
        vm_refs = [(mo_id, vm) for mo_id, vm, _row in vmr if mo_id]
        host_refs = [(mo_id, host) for mo_id, host, _row in hr if mo_id]
        perf = fetch_perf(content, vm_refs + host_refs)

        def _merged(refs):
            out = []
            for mo_id, _obj, row in refs:
                rec = dict(row)
                rec.update(perf.get(mo_id, {}))
                out.append(rec)
            return out

        acc_id = getattr(account, "id", 0)
        lines = format_metric_lines(_merged(vmr), {}, account_id=acc_id)
        lines += format_metric_lines(_merged(hr), {}, account_id=acc_id,
                                     prefix="vmware_host")
        return lines
