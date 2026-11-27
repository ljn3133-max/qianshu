# Skill：资产拓扑生成（Asset Topology）

> 可复用的资产拓扑方法。适配新环境时通过配置扩展，不写环境定制代码。

## 触发
- 用户问："画架构图 / 资产拓扑 / 系统全景 / 帮我出一张对外的架构图"

## 方法论（四步）
1. **采集**：资产同步（ECS/SLB/RDS/Redis/…，只读 API）+ 链路观测（link_edges：域名→后端带流量权重）+ SLB 后端挂载（注意 CLB 的 VServerGroup，默认组常为空）
2. **语义化**：按**架构层级**分组（接入层/网关层/应用层/大数据/中间件/数据库/第三方），按**组件集群**聚合（EMR N 台=1 框；Cassandra 多集群多框）。识别规则配置化：`config/components.json`（product_role 优先、name_rules 其次、应用层兜底）
3. **成图**：画布型架构图（固定坐标+平移缩放+正交直角走线+框拖拽微调），双版：明细（带成员）/汇报（脱敏导出）
4. **保鲜**：扫描机制——手动全量扫描（资产同步并发+diff 留痕）+定时轻扫描（30 分钟）；页面显示数据新鲜度与组件变化（新增/消失/扩/缩）

## 接口
| 接口 | 用途 |
|---|---|
| `GET /api/topology/arch?redact=0|1` | 架构图数据（roles/groups/links + last_scan 新鲜度） |
| `GET /api/topology/graph?redact=` | 节点级明细图（关系下钻用） |
| `POST /api/topology/scan` | 手动全量扫描（异步，轮询 `/scan/status`） |
| `GET /api/topology/scan/status` | 扫描进度/结果 |
| `GET /api/topology/scans` | 扫描历史（含组件变化 diff） |

## 规则模板（config/components.json）
```json
{
  "product_role": {"slb": ["gateway", "SLB 负载均衡"], "rds": ["db", "RDS 数据库"]},
  "name_rules": [
    {"pattern": "cassandra", "role": "db", "component": "Cassandra"},
    {"pattern": "apollo", "role": "third_party", "component": "Apollo 配置中心"}
  ],
  "clustered_components": ["Cassandra", "Kafka", "EMR 大数据集群"]
}
```
换环境只需在 name_rules 追加该环境的组件命名规则（如 `{"pattern": "nacos", "role": "middleware", "component": "Nacos"}`）。

## 脱敏口径（对外导出）
实例 ID 序号化、IP 掩码、规模数字与边权重拍平（线条粗细也泄露体量）；业务名保留。

## 兼容与表达规则（D 类知识）
- CLB 后端大本营是 VServerGroup，默认服务器组为空——只采默认组全漏
- ECS 私网 IP 字段是 `ip_internal`（不是 ip_private），SLB backend 的 ServerIp 对齐要用它
- 集群键要剥尾部编号与节点角色后缀（EMR core-1-1/master-1-1 同集群）
- 力导向点图不适合架构图——验收标准是"懂架构的人一眼看得懂"，不是"渲染出来了"
