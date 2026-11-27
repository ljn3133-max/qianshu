"""千枢智能体（copilot）后端引擎。

模块：
  llm.py        OpenAI 兼容 chat completions（流式，场景矩阵取模型，llm_call_logs 记账）
  intent.py     意图四分类 query/analyze/change/smalltalk（规则分类，保留 LLM 分类接口）
  tools_read.py 只读工具注册表（包装 ai_tools/diag_tools，零改动复用）
  snapshot.py   系统快照（回答前必查：云账号/数据源/资产/拓扑/最近变更）
  loop.py       主循环（意图→快照→工具循环 ≤8 轮→渲染；token 熔断；abort）
  reviewer.py   审查智能体（主回答与修订两轮交付：四维审查 verdict，成本护栏+fail-open）
  renderer.py   markdown 回复组织 + 快照引用条

红线：只读工具不提供任意 SQL/HTTP/shell 通道；变更只能经白名单工具与确认门；
绝不执行 LLM 生成的命令/SQL 原文。
"""
