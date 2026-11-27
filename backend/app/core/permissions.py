"""权限码与策略求值。

千枢权限模型：三角色默认能力 + 部门级策略覆盖 + 用户级策略覆盖。
权限码（K8s 域+高危+终端）：
  k8s.write      资源变更/管理任务提交
  k8s.dangerous  高危动作（清空/升级/删除/驱逐/Secret 揭示）
  machines.ssh   终端类（SSH/Pod exec）
扩展新域=加码+角色默认表两处。

叠加优先级（高→低）：
  用户 deny > 用户 allow > 部门 deny > 部门 allow > 角色默认
部门策略沿部门链继承（调用方把祖先链策略行都传进来即可），部门维度内 deny 优先于
allow——链上任意 deny 压过任意 allow。无部门/无部门策略时与历史行为**完全一致**
（effective_codes 第三参缺省，向后兼容旧调用）。
"""
from __future__ import annotations

PERMISSION_CODES = {
    "k8s.write": "K8s 资源变更与管理任务",
    "k8s.dangerous": "高危动作（清空/升级/删除/驱逐/揭示）",
    "machines.ssh": "终端（机器 SSH / Pod exec）",
    "agent.query": "千枢智能体问答（只读工具）",        # 智能体域
    "agent.change": "千枢智能体变更（确认门后执行）",
}

ROLE_DEFAULTS = {
    "admin": ["k8s.write", "k8s.dangerous", "machines.ssh", "agent.query", "agent.change"],
    "operator": ["k8s.write", "machines.ssh", "agent.query"],
    "viewer": ["agent.query"],
}


def effective_codes(role: str, policies: list, dept_policies: list | None = None) -> list:
    """角色默认 + 部门策略 + 用户策略（deny 优先）。

    policies / dept_policies=[(code, effect)]。叠加次序：
      codes = ((角色默认 ∪ 部门 allow) − 部门 deny ∪ 用户 allow) − 用户 deny
    即用户 deny > 用户 allow > 部门 deny > 部门 allow > 角色默认。
    dept_policies 缺省=None（旧调用两参）时保持原有行为。
    """
    codes = set(ROLE_DEFAULTS.get(role, []))
    if dept_policies:
        codes |= {c for c, e in dept_policies if e == "allow"}
        codes -= {c for c, e in dept_policies if e == "deny"}
    denies = {c for c, e in policies if e == "deny"}
    allows = {c for c, e in policies if e == "allow"}
    codes |= allows
    codes -= denies
    return sorted(codes & set(PERMISSION_CODES))
