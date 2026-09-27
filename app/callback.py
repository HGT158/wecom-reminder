"""企微接收消息回调：唯一用途是通过 URL 验证以解锁"企业可信IP"配置。

企微新规：配置可信IP前须先设置"可信域名"（需 ICP 备案）或"接收消息服务器URL"
（不查备案，但官方要求 URL 必须是域名、不能填 IP）。本端点实现标准回调解密协议。
"""
import base64
import hashlib
import logging
import struct
import xml.etree.ElementTree as ET

from Crypto.Cipher import AES
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from .config import cfg

log = logging.getLogger("reminder.callback")
router = APIRouter()


def _signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    return hashlib.sha1("".join(sorted([token, timestamp, nonce, encrypt])).encode()).hexdigest()


def _decrypt(encrypt_b64: str) -> bytes:
    key = base64.b64decode(cfg["wecom"]["encoding_aes_key"] + "=")
    if len(key) != 32:
        raise ValueError("encoding_aes_key 无效（应为43位字符串）")
    data = base64.b64decode(encrypt_b64, validate=True)
    if len(data) % 16 != 0:
        raise ValueError("密文长度不是 16 的倍数，非安全模式格式")
    plain = AES.new(key, AES.MODE_CBC, key[:16]).decrypt(data)
    msg_len = struct.unpack(">I", plain[16:20])[0]
    # 明文模式下这段是乱数据，长度字段不会合理，抛错后走原样返回
    if not (0 < msg_len < 2048 and 20 + msg_len <= len(plain)):
        raise ValueError("解密结果不符合安全模式格式")
    return plain[20:20 + msg_len]


def _configured() -> bool:
    return bool(cfg["wecom"].get("callback_token")) and bool(cfg["wecom"].get("encoding_aes_key"))


@router.get("/wecom/callback")
async def verify(request: Request):
    if not _configured():
        return PlainTextResponse(
            "服务端未配置 wecom.callback_token / encoding_aes_key（见部署说明第 3 步）", status_code=500)
    q = request.query_params
    msg_signature = q.get("msg_signature", "")
    timestamp = q.get("timestamp", "")
    nonce = q.get("nonce", "")
    echostr = q.get("echostr", "")
    if _signature(cfg["wecom"]["callback_token"], timestamp, nonce, echostr) != msg_signature:
        log.warning("URL验证签名不符")
        return PlainTextResponse("签名不符", status_code=400)
    try:
        return PlainTextResponse(_decrypt(echostr).decode())
    except Exception:
        # 明文模式：echostr 本身就是明文，校验签名后原样返回
        log.info("echostr 非安全模式密文，按明文模式原样返回")
        return PlainTextResponse(echostr)


@router.post("/wecom/callback")
async def receive(request: Request):
    """消息/事件推送：本系统只主动推送、不处理来信，按协议快速回 success。"""
    body = await request.body()
    try:
        encrypt = ET.fromstring(body).findtext("Encrypt") or ""
        q = request.query_params
        if encrypt and _configured():
            sig = _signature(cfg["wecom"]["callback_token"], q.get("timestamp", ""),
                             q.get("nonce", ""), encrypt)
            if sig != q.get("msg_signature", ""):
                return PlainTextResponse("签名不符", status_code=400)
    except Exception:
        log.exception("回调消息解析失败")
    return PlainTextResponse("success")
