import logging
import time

import requests

from .config import cfg

log = logging.getLogger("reminder.wecom")

TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"


class WeComError(RuntimeError):
    pass


_token_cache = {"token": None, "expire_at": 0.0}


def _get_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["expire_at"] - 120:
        return _token_cache["token"]
    w = cfg["wecom"]
    r = requests.get(TOKEN_URL, params={"corpid": w["corpid"], "corpsecret": w["secret"]}, timeout=10)
    d = r.json()
    if d.get("errcode"):
        raise WeComError(f"gettoken失败 errcode={d.get('errcode')}: {d.get('errmsg')}")
    _token_cache["token"] = d["access_token"]
    _token_cache["expire_at"] = time.time() + d.get("expires_in", 7200)
    return _token_cache["token"]


def send_text(content: str) -> None:
    """发一条 text 消息到应用可见范围。失败重试 3 次；token 失效自动刷新。"""
    w = cfg["wecom"]
    payload = {
        "touser": w.get("touser", "@all"),
        "msgtype": "text",
        "agentid": int(w["agentid"]),
        "text": {"content": content[:2000]},
    }
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(SEND_URL, params={"access_token": _get_token()}, json=payload, timeout=10)
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
            _token_cache["token"] = None
            time.sleep(1)
            continue
        raise last_err  # 60020 IP不在白名单 / 81013 可见范围等，重试无意义
    raise last_err
