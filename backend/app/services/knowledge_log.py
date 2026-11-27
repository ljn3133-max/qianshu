"""自改经验沉淀：code apply 成功 → 诉求/改动摘要/文件 落知识包。

定位：平台自改（code_mod 管道）每成功应用一次，就把「诉求→改动摘要→文件」登记进
知识包 agent/knowledge/自改经验.md（照 agent/knowledge-changelog.md 的落法：日期标题 +
要点行追加式登记）；供知识包一致性检查收录（knowledge_check 扫描 agent/knowledge/*.md）。

红线/边界：
  - 只写知识包自己的档案文件（agent/knowledge/），不动 playbooks/变更日志/skills 等既有知识件；
  - 沉淀是收尾动作不是执行前提：写失败只记日志，绝不阻断 code apply 主流程；
  - 不落代码全文（诉求/摘要/文件名够复盘即可，全文在 agent_code_snapshots）。

测试隔离：QIANSHU_KNOWLEDGE_DIR 环境变量可重定向落点目录（mock 测试不污染真知识包）。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("qianshu.knowledge_log")

REPO_ROOT = Path(__file__).resolve().parents[3]   # /opt/qianshu
LOG_NAME = "自改经验.md"


def knowledge_dir() -> Path:
    """落点目录：QIANSHU_KNOWLEDGE_DIR 优先（测试隔离），缺省 agent/knowledge/。"""
    override = os.environ.get("QIANSHU_KNOWLEDGE_DIR", "").strip()
    return Path(override) if override else (REPO_ROOT / "agent" / "knowledge")


_FILE_HEAD = (
    "# 自改经验（平台代码自改 · 诉求→改动→文件）\n\n"
    "> code apply 成功自动追加（code_mod 管道收尾，一次应用一条）；"
    "知识包一致性检查收录本目录（agent/knowledge/*.md）。\n"
)


def record_self_change(*, intent: str, snapshot_id, ticket_id=0, files=None,
                       lines: int = 0, verify_passed: bool = True) -> dict:
    """追加一条自改经验（诉求→改动摘要→文件）。-> {ok, path|error}，永不抛。"""
    try:
        d = knowledge_dir()
        d.mkdir(parents=True, exist_ok=True)
        fp = d / LOG_NAME
        if not fp.exists():
            fp.write_text(_FILE_HEAD, encoding="utf-8")
        paths = [str(p) for p in (files or []) if p]
        summary = f"{len(paths)} 个文件 / {lines} 行变更；验证门{'通过' if verify_passed else '未过'}"
        entry = (
            f"\n## {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
            f"快照 #{int(snapshot_id or 0)}（工单 #{int(ticket_id or 0)}）\n"
            f"- 诉求：{str(intent or '（未填）').strip()[:300]}\n"
            f"- 改动摘要：{summary}\n"
            f"- 文件：{'、'.join(f'`{p}`' for p in paths) or '（无）'}\n"
        )
        with open(fp, "a", encoding="utf-8") as fh:
            fh.write(entry)
        return {"ok": True, "path": str(fp)}
    except Exception as e:   # noqa: BLE001  沉淀失败不阻断 apply
        logger.warning("自改经验沉淀失败（不阻断 apply）: %s", str(e)[:120])
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
