import logging
import threading
import time

import requests

from .config import cfg

log = logging.getLogger("reminder.wecom")

TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
# 企业微信 text.content 接口按 UTF-8 字节限制为 2048 字节，留出安全余量按 1900 字节分片
MAX_TEXT_BYTES = 1900


class WeComError(RuntimeError):
    pass


_token_cache = {"token": None, "expire_at": 0.0}
_token_lock = threading.Lock()


def _get_token(force_refresh: bool = False) -> str:
    with _token_lock:
        if not force_refresh and _token_cache["token"] and time.time() < _token_cache["expire_at"] - 120:
            return _token_cache["token"]
        w = cfg["wecom"]
        r = requests.get(TOKEN_URL, params={"corpid": w["corpid"], "corpsecret": w["secret"]}, timeout=10)
        d = r.json()
        if d.get("errcode"):
            raise WeComError(f"gettoken失败 errcode={d.get('errcode')}: {d.get('errmsg')}")
        _token_cache["token"] = d["access_token"]
        _token_cache["expire_at"] = time.time() + d.get("expires_in", 7200)
        return _token_cache["token"]


def split_utf8_chunks(text: str, max_bytes: int = MAX_TEXT_BYTES) -> list[str]:
    """按 UTF-8 字节上限切分长消息，尽量保留完整行，不截断多字节字符。"""
    text = text or ""
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]

    chunks: list[str] = []
    cur_lines: list[str] = []
    cur_bytes = 0

    for line in text.splitlines(keepends=True):
        line_bytes = len(line.encode("utf-8"))
        if line_bytes > max_bytes:
            if cur_lines:
                chunks.append("".join(cur_lines).rstrip("\n"))
                cur_lines = []
                cur_bytes = 0
            buf = []
            buf_bytes = 0
            for ch in line:
                cb = len(ch.encode("utf-8"))
                if buf_bytes + cb > max_bytes:
                    chunks.append("".join(buf))
                    buf = [ch]
                    buf_bytes = cb
                else:
                    buf.append(ch)
                    buf_bytes += cb
            if buf:
                tail = "".join(buf)
                cur_lines = [tail]
                cur_bytes = len(tail.encode("utf-8"))
            continue

        if cur_bytes + line_bytes > max_bytes and cur_lines:
            chunks.append("".join(cur_lines).rstrip("\n"))
            cur_lines = [line]
            cur_bytes = line_bytes
        else:
            cur_lines.append(line)
            cur_bytes += line_bytes

    if cur_lines:
        chunks.append("".join(cur_lines).rstrip("\n"))
    return [c for c in chunks if c] or [text[:500]]


def _send_single(content: str, touser: str) -> None:
    w = cfg["wecom"]
    payload = {
        "touser": touser,
        "msgtype": "text",
        "agentid": int(w["agentid"]),
        "text": {"content": content},
    }
    last_err = None
    force_refresh = False
    for attempt in range(3):
        try:
            token = _get_token(force_refresh=force_refresh)
            force_refresh = False
            r = requests.post(SEND_URL, params={"access_token": token}, json=payload, timeout=10)
            d = r.json()
        except requests.RequestException as e:
            last_err = e
            time.sleep(2 ** attempt)
            continue
        code = d.get("errcode", 0)
        if code == 0:
            return
        last_err = WeComError(f"企微API errcode={code}: {d.get('errmsg')}")
        if code in (40014, 42001):  # access_token 失效 → 刷新重试
            force_refresh = True
            time.sleep(1)
            continue
        raise last_err  # 60020 IP不在白名单 / 81013 可见范围等，重试无意义
    raise last_err


def send_text(content: str, touser: str | None = None) -> None:
    """发 text 消息到指定用户或应用默认可见范围；超长内容自动分段发送。"""
    w = cfg["wecom"]
    target = (touser or "").strip() or w.get("touser", "@all")
    chunks = split_utf8_chunks(content)
    for chunk in chunks[:5]:
        _send_single(chunk, target)
