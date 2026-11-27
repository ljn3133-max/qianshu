"""值班 API：值班表管理 + 告警认领 + 升级记录。

GET    /oncall/contacts          值班表
POST   /oncall/contacts          添加联系人
PUT    /oncall/contacts/{id}     修改
DELETE /oncall/contacts/{id}     删除
POST   /oncall/ack/{alert_id}    认领告警（停止升级链）
GET    /oncall/escalations       升级记录
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import require_roles
from app.models.oncall import AlertEscalation, OncallContact
from app.models.user import User
from app.services.oncall import ack_alert, escalation_status

logger = logging.getLogger("qianshu.oncall_api")
router = APIRouter(prefix="/oncall", tags=["oncall"])


class ContactIn(BaseModel):
    name: str = Field(max_length=64)
    role: str = Field(default="", max_length=64)
    channel: str = Field(default="wecom", pattern="^(wecom|sms|phone|inapp)$")
    contact: str = Field(default="", max_length=128)
    level: int = Field(default=1, ge=1, le=9)
    enabled: bool = True


def _out(c: OncallContact) -> dict:
    return {"id": c.id, "name": c.name, "role": c.role, "channel": c.channel,
            "contact": c.contact, "level": c.level, "enabled": c.enabled}


@router.get("/contacts")
def list_contacts(db: Session = Depends(get_db),
                  user: User = Depends(require_roles("admin", "operator", "viewer"))):
    return [_out(c) for c in db.query(OncallContact).order_by(OncallContact.level).all()]


@router.post("/contacts")
def create_contact(body: ContactIn, db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin"))):
    c = OncallContact(**body.model_dump())
    db.add(c)
    db.commit()
    return _out(c)


@router.put("/contacts/{cid}")
def update_contact(cid: int, body: ContactIn, db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin"))):
    c = db.get(OncallContact, cid)
    if c is None:
        raise HTTPException(404, "联系人不存在")
    for k, v in body.model_dump().items():
        setattr(c, k, v)
    db.commit()
    return _out(c)


@router.delete("/contacts/{cid}")
def delete_contact(cid: int, db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin"))):
    c = db.get(OncallContact, cid)
    if c is None:
        raise HTTPException(404, "联系人不存在")
    db.delete(c)
    db.commit()
    return {"ok": True}


class AckIn(BaseModel):
    note: str = Field(default="", max_length=500)


@router.post("/ack/{alert_id}")
def ack(alert_id: int, body: AckIn, db: Session = Depends(get_db),
        user: User = Depends(require_roles("admin", "operator"))):
    """认领告警：有人在管了，升级链停止（问题是否消失另看 recovered）。"""
    return ack_alert(db, alert_id, user.username, body.note, user_id=user.id)


@router.get("/escalations")
def list_escalations(limit: int = 50, db: Session = Depends(get_db),
                     user: User = Depends(require_roles("admin", "operator", "viewer"))):
    rows = db.query(AlertEscalation).order_by(AlertEscalation.id.desc()).limit(limit).all()
    out = []
    for r in rows:
        ok, detail = escalation_status(r)
        out.append({"id": r.id, "alert_id": r.alert_id, "level": r.level,
                    "contact": r.contact_name, "channel": r.channel,
                    "notified_at": r.notified_at.isoformat(timespec="seconds"),
                    "acked": r.acked, "ok": ok, "detail": detail})
    return out


@router.get("/value-report")
def value_report(year: int = None, month: int = None,
                 db: Session = Depends(get_db),
                 user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """每月价值账单（平台为你做了什么）。"""
    from datetime import datetime as _dt

    from app.services.value_report import monthly_report

    now = _dt.now()
    return monthly_report(db, year or now.year, month or now.month)


@router.get("/knowledge-probe")
def knowledge_probe(db: Session = Depends(get_db),
                    user: User = Depends(require_roles("admin", "operator"))):
    """手动跑知识探针（口径漂移自检）。"""
    from app.services.knowledge_probe import run_probes

    return run_probes(db)


# ---------------- 引导式校准（首月每周"我学到的"） ----------------

class ConfirmIn(BaseModel):
    note: str = Field(default="", max_length=500)


@router.get("/calibration")
def calibration_report(week: str = "",
                       db: Session = Depends(get_db),
                       user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """校准周报：平台本周学到的 + 待拍板项。"""
    from app.services.calibration import weekly_report

    return weekly_report(db, week)


@router.post("/calibration/generate")
def calibration_generate(db: Session = Depends(get_db),
                         user: User = Depends(require_roles("admin", "operator"))):
    """手动触发生成本周周报。"""
    from app.services.calibration import generate_weekly

    return generate_weekly(db, force=True)


@router.post("/calibration/confirm/{item_id}")
def calibration_confirm(item_id: int, body: ConfirmIn,
                        db: Session = Depends(get_db),
                        user: User = Depends(require_roles("admin", "operator"))):
    """拍板确认一条"学到的"。"""
    from app.services.calibration import confirm_item

    return confirm_item(db, item_id, user.username, user_id=user.id)
