"""个人 API Token：用户自助管理访问令牌。

- GET    /users/me/tokens          我的令牌列表（只回显前缀，绝不回显明文/哈希）
- POST   /users/me/tokens          新建（明文只在本次响应里回显一次）+ 审计
- DELETE /users/me/tokens/{id}     吊销（删行）+ 审计

鉴权走既有 get_current_user（JWT 或 Bearer API Token 并存），权限=所属用户角色。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.security import generate_api_token
from app.models.user import User
from app.models.user_api_token import UserApiToken
from app.services import audit

router = APIRouter(prefix="/users", tags=["users"])


class TokenCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, description="令牌用途备注")
    expires_days: Optional[int] = Field(default=None, ge=1, le=3650,
                                       description="有效天数；空=永不过期")


def _row_out(row: UserApiToken) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "token_prefix": row.token_prefix,
        "last_used_at": row.last_used_at.isoformat(sep=" ", timespec="seconds") if row.last_used_at else "",
        "created_at": row.created_at.isoformat(sep=" ", timespec="seconds") if row.created_at else "",
        "expires_at": row.expires_at.isoformat(sep=" ", timespec="seconds") if row.expires_at else "",
        "enabled": bool(row.enabled),
    }


@router.get("/me/tokens")
def list_tokens(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (db.query(UserApiToken).filter(UserApiToken.user_id == user.id)
            .order_by(UserApiToken.id.desc()).all())
    return [_row_out(r) for r in rows]


@router.post("/me/tokens")
def create_token(body: TokenCreateIn, request: Request,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    plain, token_hash, prefix = generate_api_token()
    row = UserApiToken(
        user_id=user.id,
        name=body.name.strip(),
        token_hash=token_hash,
        token_prefix=prefix,
        expires_at=datetime.now() + timedelta(days=body.expires_days) if body.expires_days else None,
        enabled=True,
    )
    db.add(row)
    db.flush()   # 取自增 id，审计与业务同事务（回滚则一致不落库）
    audit.record(
        db, action="user_api_token.create", user_id=user.id, username=user.username,
        resource_type="user_api_token", resource_id=str(row.id),
        detail={"name": row.name, "token_prefix": prefix, "expires_days": body.expires_days},
        client_ip=get_client_ip(request),
    )
    db.commit()
    out = _row_out(row)
    out["token"] = plain   # 明文只此一次回显，库内仅存哈希
    return out


@router.delete("/me/tokens/{token_id}")
def revoke_token(token_id: int, request: Request,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(UserApiToken).filter(
        UserApiToken.id == token_id, UserApiToken.user_id == user.id).first()
    if row is None:
        raise HTTPException(404, "令牌不存在")
    detail = {"name": row.name, "token_prefix": row.token_prefix}
    db.delete(row)
    audit.record(
        db, action="user_api_token.revoke", user_id=user.id, username=user.username,
        resource_type="user_api_token", resource_id=str(token_id),
        detail=detail,
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True}
