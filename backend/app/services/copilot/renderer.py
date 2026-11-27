"""回复组织：markdown 回复 + 快照引用条 + 轨迹摘要。

回答模板固定引用快照事实，避免脱离系统数据作答；前端 markdown-it 直接渲染。
"""
from __future__ import annotations


def snapshot_brief(snapshot: dict) -> str:
    """一行快照摘要（系统提示/引用条共用）。"""
    if not snapshot:
        return "（快照缺失）"
    ca = snapshot.get("cloud_accounts") or {}
    ds = snapshot.get("datasources") or {}
    assets = snapshot.get("assets") or {}
    topo = snapshot.get("topology") or {}
    vendors = "、".join(f"{k}×{v}" for k, v in (ca.get("vendors") or {}).items()) or "未标注"
    by_product = assets.get("by_product") or {}
    product_bits = "、".join(f"{k} {v}" for k, v in sorted(by_product.items(), key=lambda x: -x[1])[:4])
    return (
        f"云账号 {ca.get('total', 0)} 个（{vendors}）；"
        f"数据源启用 {ds.get('enabled_count', 0)} 类"
        f"（指标 {len(ds.get('metric') or [])} / 日志 {len(ds.get('log') or [])}）；"
        f"资产 {assets.get('total', 0)} 项（{product_bits or '暂无'}）；"
        f"拓扑 {topo.get('edges', 0)} 条边/覆盖 {topo.get('hosts', 0)} 主机"
        f"（已确认 {topo.get('confirmed', 0)}）；"
        f"最近变更 {len(snapshot.get('recent_changes') or [])} 条"
    )


def snapshot_citation(snapshot: dict) -> str:
    """回答末尾的快照引用条（markdown）。"""
    ts = (snapshot or {}).get("generated_at") or ""
    return f"\n\n---\n> **系统快照**（{ts}）：{snapshot_brief(snapshot)}"


def render_answer(content: str, snapshot: dict, with_citation: bool = True) -> str:
    """最终回复：正文（markdown）+ 快照引用条。"""
    text = (content or "").strip()
    if not with_citation:
        return text
    return text + snapshot_citation(snapshot)


def tool_trace_line(tool_calls: list) -> str:
    """工具轨迹折叠行摘要（前端工具卡头行）。"""
    if not tool_calls:
        return "未调用工具"
    names = "、".join(t.get("name", "") for t in tool_calls[:6])
    more = f" 等 {len(tool_calls)} 个" if len(tool_calls) > 6 else ""
    ms = sum(int(t.get("ms") or 0) for t in tool_calls)
    return f"调用 {len(tool_calls)} 个工具（{names}{more}）· {ms / 1000:.1f}s"
