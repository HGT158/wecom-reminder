import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import core, db
from .callback import router as callback_router
from .config import APP_VERSION, TZ, cfg, now

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("reminder.web")

TOKEN = str(cfg["server_token"])
FMT_MIN = core.FMT_MIN
NAG_MAP = {"none": None, "5": 5, "10": 10, "30": 30, "60": 60}
REPEAT_MAP = {"none": "", "daily": "daily", "mon": "mon", "tue": "tue", "wed": "wed",
              "thu": "thu", "fri": "fri", "sat": "sat", "sun": "sun"}
BANNERS = {
    "pushfail": "任务已创建，但确认推送发送失败：请到企业微信后台把当前出口 IP 加入应用可信IP",
    "badtime": "提醒时间必须晚于现在，请重新设置",
}


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


def authed(request: Request, k: str | None) -> bool:
    return k == TOKEN or request.cookies.get("k") == TOKEN


def _set_cookie(resp, k: str):
    if k == TOKEN:
        resp.set_cookie("k", TOKEN, max_age=31536000, httponly=True)


def vm(t):
    r = core.parse_min(t["remind_at"])
    n = now()
    if r.date() == n.date():
        prefix = "今天 "
    elif r.date() == (n.date() + timedelta(days=1)):
        prefix = "明天 "
    else:
        prefix = f"{r.month}-{r.day} "
    return {
        "id": t["id"],
        "content": t["content"],
        "time_display": prefix + r.strftime("%H:%M"),
        "repeat_label": core.REPEAT_LABEL.get(t["repeat"], ""),
        "nag_label": (f"{t['nag_interval']}分钟催 · 已催{t['nag_count']}/{cfg['push']['max_nags']}"
                      if t["nag_interval"] else ""),
        "status": t["status"],
        "last_error": t["last_error"],
    }


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": APP_VERSION}


@app.get("/", response_class=PlainTextResponse)
def index(request: Request, k: str = "", msg: str = ""):
    if not authed(request, k):
        return PlainTextResponse(
            "请用带令牌的链接访问：http://<服务器IP>:8443/?k=你的server_token", status_code=401)
    banner = BANNERS.get(msg, msg)
    resp = templates.TemplateResponse(request, "index.html", {
        "active": [vm(t) for t in db.list_active()],
        "done": [vm(t) for t in db.list_done()],
        "banner": banner,
        "bookmark_url": f"{(cfg.get('public_base_url') or '').rstrip('/')}/?k={TOKEN}",
        "version": APP_VERSION,
    })
    _set_cookie(resp, k)
    return resp


@app.post("/create")
def create(request: Request, content: str = Form(...), remind_at: str = Form(...),
           nag: str = Form("none"), repeat: str = Form("none")):
    if not authed(request, None):
        return PlainTextResponse("未授权", status_code=401)
    content = content.strip()
    if not content:
        return RedirectResponse("/?msg=内容不能为空", status_code=303)
    try:
        dt = datetime.strptime(remind_at.strip(), "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)
    except ValueError:
        return RedirectResponse("/?msg=时间格式不正确", status_code=303)
    # 表单是分钟精度，允许选"当前分钟"（视为立即提醒），只拦明确过去的时间
    if dt < now() - timedelta(minutes=1):
        return RedirectResponse("/?msg=badtime", status_code=303)
    nag_min = NAG_MAP.get(nag)
    rep = REPEAT_MAP.get(repeat, "")
    task_id = db.create_task(content, dt.strftime(FMT_MIN), nag_min, rep)
    log.info("建任务 id=%s %s @ %s nag=%s repeat=%s", task_id, content, dt, nag_min, rep)
    try:
        core.push_created(task_id, content, dt, nag_min, rep)
    except Exception as e:
        log.warning("建任务确认推送失败 id=%s: %s", task_id, e)
        return RedirectResponse("/?msg=pushfail", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.get("/sw.js")
def service_worker():
    return FileResponse(Path(__file__).parent.parent / "static" / "sw.js",
                        media_type="application/javascript")


@app.get("/done/{task_id}")
def done_link(task_id: int, request: Request, k: str = ""):
    if k != TOKEN:
        return PlainTextResponse("链接无效（缺访问令牌）", status_code=401)
    t = db.get(task_id)
    if not t:
        return PlainTextResponse("任务不存在或已删除", status_code=404)
    msg, nxt = core.complete(t)
    resp = templates.TemplateResponse(request, "done.html", {"msg": msg, "next": nxt, "icon": "✓"})
    _set_cookie(resp, k)
    return resp


@app.get("/defer/{task_id}")
def defer_link(task_id: int, request: Request, k: str = ""):
    if k != TOKEN:
        return PlainTextResponse("链接无效（缺访问令牌）", status_code=401)
    t = db.get(task_id)
    if not t:
        return PlainTextResponse("任务不存在或已删除", status_code=404)
    if t["status"] == "done":
        resp = templates.TemplateResponse(request, "done.html", {
            "icon": "⏰", "msg": f"「{t['content']}」已经完成过啦，不用延后", "next": None})
        _set_cookie(resp, k)
        return resp
    new_at = max(core.parse_min(t["remind_at"]), now()) + timedelta(hours=1)
    db.update(task_id, remind_at=new_at.strftime(FMT_MIN), nag_count=0,
              last_sent_at=None, last_attempt_at=None, last_error=None, status="pending")
    log.info("延后 id=%s -> %s", task_id, new_at)
    resp = templates.TemplateResponse(request, "done.html", {
        "icon": "⏰", "msg": f"已延后「{t['content']}」1 小时",
        "next": f"下次提醒：{new_at.strftime(core.FMT_MIN)}"})
    _set_cookie(resp, k)
    return resp


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
        new_at = max(core.parse_min(t["remind_at"]), now()) + timedelta(hours=1)
        db.update(task_id, remind_at=new_at.strftime(FMT_MIN), nag_count=0,
                  last_sent_at=None, last_attempt_at=None, last_error=None, status="pending")
    return RedirectResponse("/", status_code=303)


@app.post("/task/{task_id}/delete")
def delete_task(task_id: int, request: Request):
    if not authed(request, None):
        return PlainTextResponse("未授权", status_code=401)
    db.delete(task_id)
    return RedirectResponse("/", status_code=303)
