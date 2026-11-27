"""通知发送：企微/钉钉机器人 webhook（markdown 消息）。

只做发消息；重试/静默等策略由调用方（告警评估、巡检推送）控制。
"""
from __future__ import annotations

import json
import logging

import httpx

from app.core.config import settings
from app.core.security import decrypt_secret

logger = logging.getLogger("qianshu.notify")


class NotifyError(RuntimeError):
    """发送失败（网络/HTTP/errcode）。"""


def _post_and_check(url: str, payload: dict, verify: bool = True) -> None:
    try:
        resp = httpx.post(url, json=payload, timeout=10, verify=verify)
    except httpx.HTTPError as e:
        raise NotifyError(f"网络错误: {type(e).__name__} {str(e)[:120]}") from e
    except (httpx.InvalidURL, ValueError) as e:  # 非法 URL（如端口非数字）
        raise NotifyError(f"Webhook 地址非法: {str(e)[:120]}") from e
    if resp.status_code != 200:
        raise NotifyError(f"HTTP {resp.status_code}: {resp.text[:150]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise NotifyError(f"响应非 JSON: {resp.text[:150]}") from e
    errcode = data.get("errcode")
    if errcode not in (0, None):
        raise NotifyError(f"机器人返回错误 errcode={errcode}: {data.get('errmsg', '')}")


def send_wecom_bot(url: str, title: str, content_md: str, verify: bool = True) -> None:
    """企业微信群机器人。markdown 消息无独立标题字段，标题拼进正文。"""
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": f"**{title}**\n{content_md}"},
    }
    _post_and_check(url, payload, verify=verify)


def send_dingtalk_bot(url: str, title: str, content_md: str, at_mobiles: list = None,
                      verify: bool = True) -> None:
    """钉钉群机器人。at_mobiles 里的手机号会在群里被 @。"""
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": f"**{title}**\n{content_md}"},
        "at": {"atMobiles": at_mobiles or [], "isAtAll": False},
    }
    _post_and_check(url, payload, verify=verify)


def send_generic_webhook(url: str, title: str, content_md: str, verify: bool = True) -> None:
    """通用 webhook（POST JSON）：请求体 {"title", "content"}，不校验响应体格式。"""
    try:
        resp = httpx.post(url, json={"title": title, "content": content_md},
                          timeout=10, verify=verify)
    except httpx.HTTPError as e:
        raise NotifyError(f"网络错误: {type(e).__name__} {str(e)[:120]}") from e
    except (httpx.InvalidURL, ValueError) as e:
        raise NotifyError(f"Webhook 地址非法: {str(e)[:120]}") from e
    if resp.status_code >= 400:
        raise NotifyError(f"HTTP {resp.status_code}: {resp.text[:150]}")


def send_to_channel(channel, title: str, content_md: str) -> None:
    """按渠道类型发送。channel 为 AlertChannel 模型实例。

    邮件渠道已于 2026-11-12 下线；历史 email 行保留在库中不展示，删除入口仍可用。
    """
    if channel.type == "email":
        raise NotifyError("邮件渠道已下线，请改用企微/钉钉/通用 Webhook")
    try:
        url = decrypt_secret(channel.webhook_url_cipher)
    except Exception as e:
        raise NotifyError("webhook 解密失败：MASTER_KEY 不匹配或密文损坏") from e
    try:
        mobiles = json.loads(channel.at_mobiles or "[]")
    except ValueError:
        mobiles = []
    # verify_ssl 默认 True；内网自签 webhook 可显式关闭校验
    verify = bool(getattr(channel, "verify_ssl", True))
    if channel.type == "wecom_bot":
        send_wecom_bot(url, title, content_md, verify=verify)
    elif channel.type == "dingtalk_bot":
        send_dingtalk_bot(url, title, content_md, mobiles, verify=verify)
    else:
        send_generic_webhook(url, title, content_md, verify=verify)


def notify_all_channels(db, title: str, content: str, level: str = "critical",
                        at_mobiles: list = None, module: str | None = None,
                        account_id: int | None = None) -> dict:
    """内置告警统一通知出口。

    所有告警源（规则评估/拨测/host_watch/升级链）只走这一个出口——
    发送配置只有一处（告警渠道表），测试模式强制落测试渠道（安全红线机制化）。
    渠道全挂时站内信兜底，告警绝不因渠道故障丢失。

    module 非空时按「模块开关 AND 账号去向」过滤；
    站内信与外部推送各自独立判断（用户可能只关其中一路）。module=None 表示
    平台系统消息，不受监控开关控制（保持旧行为）。
    """
    from app.models.alert_channel import AlertChannel

    from app.services.monitor_policy import can_inapp, can_push

    lvl = level if level in ("info", "warning", "critical") else "warning"
    sent, errors, blocked = [], [], []

    # 1) 站内信（必达兜底；只给第一个管理员，保持既有口径）
    if module is None or can_inapp(db, module, account_id):
        try:
            n = notify_inapp(db, title, content, level=lvl, audience="first_admin")
            if n:
                sent.append("inapp")
        except Exception as e:
            errors.append(f"inapp: {str(e)[:60]}")
    else:
        blocked.append("inapp")

    # 2) 渠道表（测试模式强制落测试渠道，不真发）
    if module is not None and not can_push(db, module, account_id):
        blocked.append("push")
    elif settings.NOTIFY_TEST_MODE:
        sent.append(f"test-mode({settings.NOTIFY_TEST_CHANNEL})")
    else:
        for ch in (db.query(AlertChannel)
                   .filter(AlertChannel.enabled == True, AlertChannel.type != "email")  # noqa: E712
                   .all()):
            try:
                send_to_channel(ch, title, content)
                sent.append(ch.name)
            except Exception as e:
                errors.append(f"{ch.name}: {str(e)[:60]}")

    try:
        db.commit()
    except Exception:
        db.rollback()   # 提交失败必须回滚（防悬挂写锁）
    return {"sent": sent, "errors": errors, "blocked": blocked}


def notify_inapp(db, title: str, content_md: str, *, level: str = "info",
                 msg_type: str = "system", ref_id: int | None = None,
                 audience: str = "admins", module: str | None = None,
                 account_id: int | None = None) -> int:
    """站内信单一写出口。

    所有站内信统一走这里，避免各调用方分别实现收件人解析与兜底而漏发。
    只写站内信、绝不碰机器人渠道（对外推送仍必须经 notify_all_channels 或用户显式触发）。
    audience: "admins"=全部管理员（无管理员兜底 user_id=1）| "active"=全部启用用户
    | "first_admin"=仅第一个管理员（告警保底口径）。
    module/account_id：模块或账号关了站内信就返回 0 不写；
    module=None 为平台系统消息，不受监控开关控制。
    返回写入条数；不 commit——由调用方与其余写库同事务提交（成败一致）。
    """
    from app.models.in_app_message import InAppMessage
    from app.models.user import User
    from app.services.monitor_policy import can_inapp

    if module is not None and not can_inapp(db, module, account_id):
        return 0

    if audience == "active":
        uids = [r[0] for r in db.query(User.id)
                .filter(User.is_active == True).all()]  # noqa: E712
    elif audience == "first_admin":
        uid = db.query(User.id).filter(User.role == "admin").first()
        uids = [uid[0]] if uid else [1]
    else:
        uids = [r[0] for r in db.query(User.id).filter(User.role == "admin").all()] or [1]
    for uid in uids:
        db.add(InAppMessage(user_id=uid, msg_type=msg_type, title=title,
                            content_md=content_md, level=level, ref_id=ref_id))
    return len(uids)


# ---------------- 短信/电话渠道（标准库直调阿里云 RPC 签名） ----------------

def _rpc_sign(params: dict, secret: str) -> str:
    """阿里云 RPC 签名 V1（HMAC-SHA1）：规范化参数串 -> 签名。"""
    import base64
    import hashlib
    import hmac
    from urllib.parse import quote

    items = sorted((k, str(v)) for k, v in params.items() if k != "Signature")
    qs = "&".join(f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in items)
    src = "GET&%2F&" + quote(qs, safe="~")
    sig = hmac.new((secret + "&").encode(), src.encode(), hashlib.sha1).digest()
    return base64.b64encode(sig).decode()


def send_sms(ak: str, sk: str, phone: str, sign_name: str, template_code: str,
             template_param: str = "{}") -> dict:
    """发短信（阿里云 dysmsapi SendSms，RPC 签名直调）。

    安全：NOTIFY_TEST_MODE 时绝不真发（返回模拟成功）——测试红线机制化。
    """
    if settings.NOTIFY_TEST_MODE:
        return {"ok": True, "simulated": True, "detail": f"[测试模式] 短信 {phone} 未真发"}
    import time
    import uuid

    import httpx

    params = {
        "AccessKeyId": ak, "Action": "SendSms", "Format": "JSON",
        "PhoneNumbers": phone, "RegionId": "cn-hangzhou",
        "SignName": sign_name, "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex, "SignatureVersion": "1.0",
        "TemplateCode": template_code, "TemplateParam": template_param,
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Version": "2017-05-25",
    }
    params["Signature"] = _rpc_sign(params, sk)
    r = httpx.get("https://dysmsapi.aliyuncs.com/", params=params, timeout=15).json()
    return {"ok": r.get("Code") == "OK", "detail": str(r)[:200]}


def send_voice_call(ak: str, sk: str, phone: str, tts_code: str,
                    tts_param: str = "") -> dict:
    """语音电话（阿里云 dyvmsapi SingleCallByVoice）——升级链最终手段。"""
    if settings.NOTIFY_TEST_MODE:
        return {"ok": True, "simulated": True, "detail": f"[测试模式] 语音 {phone} 未真呼"}
    import time
    import uuid

    import httpx

    params = {
        "AccessKeyId": ak, "Action": "SingleCallByVoice", "Format": "JSON",
        "CalledNumber": phone, "RegionId": "cn-hangzhou",
        "SignatureMethod": "HMAC-SHA1", "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "TtsCode": tts_code, "TtsParam": tts_param, "Version": "2017-05-25",
    }
    params["Signature"] = _rpc_sign(params, sk)
    r = httpx.get("https://dyvmsapi.aliyuncs.com/", params=params, timeout=15).json()
    return {"ok": r.get("Code") == "OK", "detail": str(r)[:200]}
