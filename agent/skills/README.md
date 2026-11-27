# 千枢 Skill 清单（27 条，按功能生成）

| Skill | 干什么 | 场景 |
|---|---|---|
| [app_errors](app_errors.md) | 查应用 ERROR 日志：数量 + 最新样本内容（应用名 = 链路库 app_name 或域名） | link_fault, host_res |
| [backend_health](backend_health.md) | 查域名的后端 ECS：运行状态、CPU/内存/磁盘 24h 峰值、每台的流量/5xx/RT | link_fault |
| [bill_breakdown](bill_breakdown.md) | 账单拆解：目标（实例/产品/场景）近 N 天账单行、逐日趋势、计费场景构成——'这笔钱怎么来的' | cost_analysis, dba |
| [bill_config](bill_config.md) | 计费相关配置实况：SQL 审计/日志的开通状态、冷热存储时长与收费统计方式——'是不是开着、哪块在收钱' | cost_analysis, dba |
| [biz_errors](biz_errors.md) | 查业务报错：应用 ERROR 与 SLB 5xx 的小时分布——资源异常时段业务是否受损 | host_res, link_fault |
| [cdn_domain_traffic](cdn_domain_traffic.md) | 查 CDN/DCDN 按域名的每日流量——CDN 流量包到底被哪个域名烧掉、各占多少 | cost_analysis |
| [cdn_url_breakdown](cdn_url_breakdown.md) | CDN 域名的 URL/文件级流量下钻：哪些文件在烧流量、峰值日较前日谁涨了（流量突增归因） | cost_analysis, link_fault, host_res |
| [cost_anomaly_list](cost_anomaly_list.md) | 查近 14 天成本异动记录（池将耗尽/包刚耗尽/按量突增/新增费用项，含确认状态） | cost_analysis |
| [db_health](db_health.md) | 查下游 RDS/Redis 关键指标（CPU/内存/磁盘/连接/平均RT），含各指标最高实例读数，看是否有下游瓶颈 | link_fault, dba |
| [dba_space_trend](dba_space_trend.md) | 查某 RDS 实例空间近 14 天趋势（数据/日志/备份/磁盘，含 14 天增量） | dba |
| [expiry_list](expiry_list.md) | 查即将到期的域名/SSL证书/包年资产/资源包清单（90 天内，按剩余天数升序） | cost_analysis |
| [find_domain](find_domain.md) | 按关键词查找已知链路域名（把『xxx.com打不开』对准到具体域名），返回后端/流量/健康概要 | link_fault |
| [host_metrics](host_metrics.md) | 查主机内存/CPU/磁盘水位时间线：峰值时刻、峰值、当前值（资源类问题先查这个定位事发时刻） | host_res, link_fault |
| [instance_cost_top](instance_cost_top.md) | 查某产品近 N 天费用 Top 实例（钱具体花在哪台机器/哪个域名上） | cost_analysis |
| [object_chain](object_chain.md) | 对象全景因果链：域名/实例近 N 天的多层同窗读数（CDN/SLB/应用/主机/DB/变更/钱）+ 联动变化事件——跨层分析第一入口 | link_fault, host_res, cost_analysis, dba |
| [pack_pools](pack_pools.md) | 查全部资源包抵扣能力池：类型/包数/剩余/剩余比例（告警统计方式是池不是单包） | cost_analysis |
| [path_latency_detail](path_latency_detail.md) | 查域名下各接口路径的响应时间（平均/p95）与 5xx：定位到底哪个 URI 慢 | link_fault |
| [pool_usage_trend](pool_usage_trend.md) | 查某类资源包的每日消耗趋势、平均/最近日消耗、几天耗尽（如 CDN 流量包、日志服务包） | cost_analysis |
| [process_logs](process_logs.md) | 查进程对应的应用日志：服务名归一匹配 AppName，看事发时段它在跑什么（日志量形状/级别/内容样本） | host_res |
| [process_top](process_top.md) | 查进程级 top（内存或 CPU）：各进程峰值/日常水位/突变时刻——谁在吃资源 | host_res |
| [product_cost_trend](product_cost_trend.md) | 查某产品的日成本趋势（按量/包月拆分，T+1 账单统计方式），看费用是否突增 | cost_analysis |
| [rds_list](rds_list.md) | 查 RDS 实例清单（引擎/状态/到期），下钻慢SQL或空间前先看这个 | dba |
| [recent_changes](recent_changes.md) | 云上变更时间线：谁在什么时候改了什么（重启/改配置/扩容等） | link_fault, cost_analysis, dba, host_res |
| [slb_5xx_detail](slb_5xx_detail.md) | 查域名 5xx 明细：具体哪些 URI、什么状态码、哪个后端返回的 | link_fault |
| [slb_overview](slb_overview.md) | 查域名在 SLB 层的健康：状态码分布、5xx率、平均RT、按后端分组 | link_fault |
| [slowlog_top](slowlog_top.md) | 查某 RDS 实例的慢SQL Top（执行次数/耗时/SQL 摘要），定位慢查询 | dba |
| [space_suspects](space_suspects.md) | 空间上涨的表级嫌疑：从 SQL 证据找膨胀源（写语句大扫描）与临时文件源（大扫描+聚合/排序），定位到表 | dba |
