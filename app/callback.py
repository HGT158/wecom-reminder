"""企微接收消息回调：处理 URL 验证以及 /ai 命令异步触发。"""
import base64
import hashlib
import hmac
import logging
import struct
import xml.etree.ElementTree as ET

from Crypto.Cipher import AES
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import PlainTextResponse

from . import ai, db
from .config import ai_cfg, cfg
from .wecom import send_text

log = logging.getLogger("reminder.callback")
router = APIRouter()


def _signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    return hashlib.sha1("".join(sorted([token, timestamp, nonce, encrypt])).encode()).hexdigest()


def _decrypt(encrypt_b64: str) -> bytes:
    """按企微回调标准 AES-CBC 解密并校验 PKCS#7 填充与企业 corpid。"""
    key = base64.b64decode(cfg["wecom"]["encoding_aes_key"] + "=")
    if len(key) != 32:
        raise ValueError("encoding_aes_key 无效（应为43位字符串）")
    data = base64.b64decode(encrypt_b64, validate=True)
    if len(data) < 32 or len(data) % 16 != 0:
        raise ValueError("密文长度不是 16 的倍数，非安全模式格式")
    plain = AES.new(key, AES.MODE_CBC, key[:16]).decrypt(data)

    # 企微使用 32 字节分组 PKCS#7 填充；兼容验证并剥离尾部填充
    pad = plain[-1]
    if 1 <= pad <= 32 and plain[-pad:] == bytes([pad]) * pad:
        content = plain[:-pad]
    else:
        content = plain

    if len(content) < 20:
        raise ValueError("解密内容过短")
    msg_len = struct.unpack(">I", content[16:20])[0]
    if not (0 < msg_len <= 65536 and 20 + msg_len <= len(content)):
        raise ValueError("解密结果不符合安全模式格式")

    corpid = str(cfg["wecom"].get("corpid") or "").strip()
    receive_id = content[20 + msg_len:].decode("utf-8", errors="replace").strip()
    if corpid and receive_id and receive_id != corpid:
        raise ValueError("回调密文 corpid 不匹配")
    return content[20:20 + msg_len]


def _configured() -> bool:
    return bool(cfg["wecom"].get("callback_token")) and bool(cfg["wecom"].get("encoding_aes_key"))


def _handle_ai_background(from_user: str, prompt: str) -> None:
    """在响应企微回调之后异步执行 AI 指令，并主动推送回复给发送者。"""
    try:
        reply = ai.handle_command(prompt)
    except Exception:
        log.exception("AI 命令后台执行异常 from_user=%s", from_user)
        reply = "AI 处理遇到意外错误，请稍后重试或使用网页端操作。"
    if not reply:
        return
    try:
        send_text(reply, touser=from_user)
    except Exception:
        log.exception("AI 结果回推企微失败 from_user=%s", from_user)


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
    expected = _signature(cfg["wecom"]["callback_token"], timestamp, nonce, echostr)
    if not hmac.compare_digest(expected, msg_signature):
        log.warning("URL验证签名不符")
        return PlainTextResponse("签名不符", status_code=400)
    try:
        return PlainTextResponse(_decrypt(echostr).decode())
    except Exception:
        # 明文模式：echostr 本身就是明文，校验签名后原样返回
        log.info("echostr 非安全模式密文，按明文模式原样返回")
        return PlainTextResponse(echostr)


@router.post("/wecom/callback")
async def receive(request: Request, background_tasks: BackgroundTasks):
    """接收企微推送消息：验签解密并去重后，命中 /ai 命令则后台执行，立即返回 success 防 5 秒超时。"""
    body = await request.body()
    if not _configured():
        log.warning("收到企微回调，但未配置 callback_token / encoding_aes_key")
        return PlainTextResponse("success")

    try:
        encrypt = ET.fromstring(body).findtext("Encrypt") or ""
    except Exception:
        log.exception("回调外层 XML 解析失败")
        return PlainTextResponse("success")

    q = request.query_params
    msg_signature = q.get("msg_signature", "")
    expected = _signature(cfg["wecom"]["callback_token"], q.get("timestamp", ""),
                          q.get("nonce", ""), encrypt)
    if not encrypt or not hmac.compare_digest(expected, msg_signature):
        log.warning("回调消息签名不符或缺少密文")
        return PlainTextResponse("签名不符", status_code=400)

    try:
        plain_xml = _decrypt(encrypt).decode("utf-8")
        root = ET.fromstring(plain_xml)
    except Exception:
        log.exception("回调消息解密或内层 XML 解析失败")
        return PlainTextResponse("success")

    msg_type = (root.findtext("MsgType") or "").strip()
    from_user = (root.findtext("FromUserName") or "").strip()
    msg_id = (root.findtext("MsgId") or "").strip()
    create_time = (root.findtext("CreateTime") or "").strip()
    content = root.findtext("Content") or ""

    if msg_type != "text":
        return PlainTextResponse("success")

    is_ai, prompt = ai.parse_ai_command(content)
    if not is_ai:
        return PlainTextResponse("success")

    dedup_key = (
        f"msg:{msg_id}"
        if msg_id
        else f"txt:{from_user}:{create_time}:{hashlib.sha1(content.encode()).hexdigest()[:12]}"
    )
    if not db.claim_callback_msg(dedup_key):
        log.info("忽略重复回调消息 key=%s", dedup_key)
        return PlainTextResponse("success")

    allowed = ai_cfg()["allowed_users"]
    if allowed and from_user not in allowed:
        log.warning("拦截未授权企微用户触发 /ai：FromUserName=%s", from_user)
        return PlainTextResponse("success")

    log.info("收到 /ai 指令 from_user=%s msg_id=%s prompt_len=%d", from_user, msg_id, len(prompt))
    background_tasks.add_task(_handle_ai_background, from_user, prompt)
    return PlainTextResponse("success")
