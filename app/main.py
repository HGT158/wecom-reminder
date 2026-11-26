import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import core, db, service
from .callback import router as callback_router
from .config import APP_VERSION, TZ, base_url, cfg, now, push_cfg

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("reminder.web")

TOKEN = str(cfg["server_token"])
BANNERS = {
    "pushfail": "任务已创建，但确认推送发送失败：请到企业微信后台把当前出口 IP 加入应用可信IP",
    "badtime": "提醒时间必须晚于现在，请重新设置",
    "edited": "✅ 已保存修改",
    "cleared": "✅ 已清空已完成记录",
}

NAG_OPTS = [("none", "只提醒一次"), ("5", "每 5 分钟催"), ("10", "每 10 分钟催"),
            ("30", "每 30 分钟催"), ("60", "每 60 分钟催")]
REP_OPTS = [("none", "不重复"), ("daily", "每天"), ("mon", "每周一"), ("tue", "每周二"),
            ("wed", "每周三"), ("thu", "每周四"), ("fri", "每周五"), ("sat", "每周六"), ("sun", "每周日")]


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    core.start()
    yield
    core.stop()


app = FastAPI(title="待办提醒", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent.parent / "static")), name="static")
app.include_router(callback_router)


def _token_match(val: str | None) -> bool:
    return bool(val) and hmac.compare_digest(val, TOKEN)


def authed(request: Request, k: str | None) -> bool:
    return _token_match(k) or _token_match(request.cookies.get("k"))


def _set_cookie(resp, k: str):
    if _token_match(k):
        resp.set_cookie("k", TOKEN, max_age=31536000, httponly=True, samesite="lax")


def _is_prefetch(request: Request) -> bool:
    purpose = (request.headers.get("purpose") or request.headers.get("sec-purpose") or "").lower()
    return "prefetch" in purpose


def vm(t):
    r = core.effective_remind(t)
    anchor = core.parse_min(t["remind_at"])
    n = now()
    if r.date() == n.date():
        prefix = "今天 "
    elif r.date() == (n.date() + timedelta(days=1)):
        prefix = "明天 "
    else:
        prefix = f"{r.month}-{r.day} "

    snooze_raw = t["snooze_until"] if "snooze_until" in t.keys() else None
    snooze_label = f"已延后 · 原日程 {anchor.strftime('%H:%M')}" if (snooze_raw and t["repeat"]) else ""
    max_nags = push_cfg()["max_nags"]
    return {
        "id": t["id"],
        "content": t["content"],
        "time_display": prefix + r.strftime("%H:%M"),
        "repeat_label": core.REPEAT_LABEL.get(t["repeat"], ""),
        "nag_label": (f"{t['nag_interval']}分钟催 · 已催{t['nag_count']}/{max_nags}"
                      if t["nag_interval"] else ""),
        "snooze_label": snooze_label,
        "status": t["status"],
        "last_error": t["last_error"],
    }


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": APP_VERSION}


@app.get("/", response_class=PlainTextResponse)
def index(request: Request, k: str = "", msg: str = ""):
    if not authed(request, k):
        # 提示里的基地址取自配置，避免写死端口与实际部署方式不符；
        # 令牌本身不打出来（此页面未鉴权）
        base = base_url()
        hint = f"{base}/?k=<server_token>" if base else "/?k=<server_token>"
        return PlainTextResponse(f"请用带令牌的链接访问：{hint}", status_code=401)
    pc = push_cfg()
    quiet_label = (f"催办免打扰 {pc['quiet_start']}:00–次日{pc['quiet_end']}:00"
                   if pc["quiet_enabled"] else "")
    hb = cfg.get("heartbeat") or {}
    heartbeat_label = (f"每天 {hb.get('time', '08:00')} 推送今日待办"
                       if hb.get("enabled") else "")
    banner = BANNERS.get(msg, msg)
    resp = templates.TemplateResponse(request, "index.html", {
        "active": [vm(t) for t in db.list_active()],
        "done": [vm(t) for t in db.list_done()],
        "banner": banner,
        "quiet_label": quiet_label,
        "heartbeat_label": heartbeat_label,
        "bookmark_url": f"{base_url()}/?k={TOKEN}",
        "version": APP_VERSION,
    })
    _set_cookie(resp, k)
    return resp


@app.post("/create")
def create(request: Request, content: str = Form(...), remind_at: str = Form(...),
           nag: str = Form("none"), repeat: str = Form("none")):
    if not authed(request, None):
        return PlainTextResponse("未授权", status_code=401)
    try:
        dt = datetime.strptime(remind_at.strip(), "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)
    except ValueError:
        return RedirectResponse("/?msg=时间格式不正确", status_code=303)

    nag_min = service.NAG_MAP.get(nag) if nag in service.NAG_MAP else nag
    rep = service.REPEAT_MAP.get(repeat, repeat)
    try:
        res = service.create_reminder(content, dt, nag_min, rep, notify=True)
    except service.TaskValidationError as e:
        key = "badtime" if "晚于现在" in str(e) else quote(str(e))
        return RedirectResponse(f"/?msg={key}", status_code=303)
    if not res["push_ok"]:
        return RedirectResponse("/?msg=pushfail", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.get("/sw.js")
def service_worker():
    return FileResponse(Path(__file__).parent.parent / "static" / "sw.js",
                        media_type="application/javascript")


@app.get("/done/{task_id}")
def done_link(task_id: int, request: Request, k: str = ""):
    if not _token_match(k):
        return PlainTextResponse("链接无效（缺访问令牌）", status_code=401)
    if _is_prefetch(request):
        return PlainTextResponse("prefetch ignored", status_code=204)
    t = db.get(task_id)
    if not t:
        return PlainTextResponse("任务不存在或已删除", status_code=404)
    msg, nxt = core.complete(t)
    resp = templates.TemplateResponse(request, "done.html", {"msg": msg, "next": nxt, "icon": "✓", "version": APP_VERSION})
    _set_cookie(resp, k)
    return resp


@app.get("/defer/{task_id}")
def defer_link(task_id: int, request: Request, k: str = ""):
    if not _token_match(k):
        return PlainTextResponse("链接无效（缺访问令牌）", status_code=401)
    if _is_prefetch(request):
        return PlainTextResponse("prefetch ignored", status_code=204)
    try:
        res = service.defer_reminder(task_id, minutes=60)
    except service.TaskValidationError:
        return PlainTextResponse("任务不存在或已删除", status_code=404)
    if res["already_done"]:
        resp = templates.TemplateResponse(request, "done.html", {
            "icon": "⏰", "msg": f"「{res['task']['content']}」已经完成过啦，不用延后", "next": None, "version": APP_VERSION})
        _set_cookie(resp, k)
        return resp
    new_at = res["new_at"]
    resp = templates.TemplateResponse(request, "done.html", {
        "icon": "⏰", "msg": f"已延后「{res['task']['content']}」1 小时",
        "next": f"下次提醒：{new_at.strftime(core.FMT_MIN)}", "version": APP_VERSION})
    _set_cookie(resp, k)
    return resp


@app.get("/task/{task_id}/edit")
def edit_form(task_id: int, request: Request, k: str = "", msg: str = ""):
    if not authed(request, k):
        return PlainTextResponse("未授权", status_code=401)
    t = db.get(task_id)
    if not t or t["status"] == "done":
        return RedirectResponse("/", status_code=303)
    nag_v = str(t["nag_interval"]) if t["nag_interval"] else "none"
    rep_v = t["repeat"] or "none"

    nag_choices = list(NAG_OPTS)
    nag_labels = dict(nag_choices)
    if nag_v not in nag_labels:
        custom_label = f"每 {nag_v} 分钟催"
        nag_choices.append((nag_v, custom_label))
        nag_labels[nag_v] = custom_label

    resp = templates.TemplateResponse(request, "edit.html", {
        "t_id": t["id"],
        "content": t["content"],
        "dt_iso": core.effective_remind(t).strftime("%Y-%m-%dT%H:%M"),
        "nag_opts": [{"v": v, "label": lb, "sel": v == nag_v} for v, lb in nag_choices],
        "rep_opts": [{"v": v, "label": lb, "sel": v == rep_v} for v, lb in REP_OPTS],
        "nag_label": nag_labels[nag_v],
        "rep_label": dict(REP_OPTS).get(rep_v, "不重复"),
        "nag_v": nag_v,
        "rep_v": rep_v,
        "banner": BANNERS.get(msg, msg), "version": APP_VERSION,
    })
    _set_cookie(resp, k)
    return resp


@app.post("/task/{task_id}/edit")
def edit_task(task_id: int, request: Request, content: str = Form(...),
              remind_at: str = Form(...), nag: str = Form("none"), repeat: str = Form("none")):
    if not authed(request, None):
        return PlainTextResponse("未授权", status_code=401)
    t = db.get(task_id)
    if not t or t["status"] == "done":
        return RedirectResponse("/", status_code=303)
    try:
        dt = datetime.strptime(remind_at.strip(), "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)
    except ValueError:
        return RedirectResponse("/", status_code=303)
    nag_min = service.NAG_MAP.get(nag) if nag in service.NAG_MAP else nag
    rep = service.REPEAT_MAP.get(repeat, repeat)
    try:
        service.update_reminder(task_id, content=content, remind_at=dt,
                                nag_interval=nag_min, repeat=rep, require_future_if_set=True)
    except service.TaskValidationError as e:
        key = "badtime" if "晚于现在" in str(e) else quote(str(e))
        return RedirectResponse(f"/task/{task_id}/edit?msg={key}", status_code=303)
    return RedirectResponse("/?msg=edited", status_code=303)


@app.post("/task/{task_id}/complete")
def complete_web(task_id: int, request: Request):
    if not authed(request, None):
        return PlainTextResponse("未授权", status_code=401)
    t = db.get(task_id)
    if t:
        msg, nxt = core.complete(t)
        text = msg + (f"（{nxt}）" if nxt else "")
        return RedirectResponse(f"/?msg={quote(text)}", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.post("/task/{task_id}/defer")
def defer(task_id: int, request: Request):
    if not authed(request, None):
        return PlainTextResponse("未授权", status_code=401)
    t = db.get(task_id)
    if t and t["status"] != "done":
        service.defer_reminder(task_id, minutes=60)
    return RedirectResponse("/", status_code=303)


@app.post("/task/{task_id}/delete")
def delete_task(task_id: int, request: Request):
    if not authed(request, None):
        return PlainTextResponse("未授权", status_code=401)
    db.delete(task_id)
    return RedirectResponse("/", status_code=303)


@app.post("/tasks/clear-done")
def clear_done_tasks(request: Request):
    if not authed(request, None):
        return PlainTextResponse("未授权", status_code=401)
    db.clear_done()
    return RedirectResponse("/?msg=cleared", status_code=303)
