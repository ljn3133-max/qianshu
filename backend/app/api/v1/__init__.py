from fastapi import APIRouter, Depends

from app.api.v1 import agent, agent_copilot, agent_lifecycle, agent_schedules, agent_workflows, machines, ai, alerts, assets, audit, auth, bootstrap, changes, clouds, cost, dashboard, dba, datasource, departments, diagnosis, domains, ecs_ops, events_webhook, expiry, integrations, issues, tools, inspection, k8mgmt, k8s, link, logs, mcp_server, messages, monitor, multicloud, oncall, ownership, policy, profiles, settings, synth, topology, users
from app.core.deps import require_password_changed

api_router = APIRouter(dependencies=[Depends(require_password_changed)])
api_router.include_router(auth.router)
api_router.include_router(departments.router)   # 组织架构+Namespace 授权
api_router.include_router(users.router)   # 个人 API Token
api_router.include_router(agent.router)
api_router.include_router(agent_copilot.router)   # 千枢智能体会话
api_router.include_router(agent_workflows.router)  # 千枢智能体工作流
api_router.include_router(agent_schedules.router)  # 千枢智能体工作流定时体检
api_router.include_router(agent_lifecycle.router)  # copilotd 独立运行时生命周期桥
api_router.include_router(issues.router)  # 平台自愈问题收件箱
api_router.include_router(audit.router)
api_router.include_router(machines.router)
api_router.include_router(clouds.router)
api_router.include_router(settings.router)
api_router.include_router(bootstrap.router)
api_router.include_router(assets.router)
api_router.include_router(ecs_ops.router)
api_router.include_router(monitor.router)
api_router.include_router(logs.router)
api_router.include_router(alerts.router)
api_router.include_router(messages.router)
api_router.include_router(cost.router)
api_router.include_router(inspection.router)
api_router.include_router(link.router)
api_router.include_router(diagnosis.router)
api_router.include_router(domains.router)     # 域名登记
api_router.include_router(ai.router)
api_router.include_router(dba.router)
api_router.include_router(dashboard.router)
api_router.include_router(expiry.router)
api_router.include_router(profiles.router)
api_router.include_router(changes.router)
api_router.include_router(topology.router)
api_router.include_router(ownership.router)   # 归属配置
api_router.include_router(events_webhook.router)
api_router.include_router(integrations.router)
api_router.include_router(k8s.router)
api_router.include_router(k8mgmt.router)
api_router.include_router(tools.router)
api_router.include_router(mcp_server.router)
api_router.include_router(synth.router)
api_router.include_router(oncall.router)
api_router.include_router(multicloud.router)
api_router.include_router(datasource.router)
api_router.include_router(policy.router)   # 监控分级策略
