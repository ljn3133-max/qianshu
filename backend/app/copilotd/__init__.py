"""qianshu-copilotd：千枢智能体独立运行时。

千枢 FastAPI 挂掉时本守护仍存活：自检 → 定位/修复/拉起千枢 →
验证恢复，全部只动千枢自身（进程/配置），带留痕与审计。

硬约束：
  - 仅标准库，不依赖千枢 FastAPI 运行时（独立进程、独立日志）；
  - 生命周期通道只监听 127.0.0.1 + 文件 token 鉴权，不对外；
  - start_cmd 白名单校验（只许指向千枢服务），无任意命令通道；
  - 任何终止动作只对「cmdline 含 app.main:app」的千枢进程执行，绝不误杀他者进程。

入口：cd /opt/qianshu/backend && python3 -m app.copilotd
"""
