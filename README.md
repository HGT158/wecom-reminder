# wecom-reminder · 企业微信待办提醒

基于企业微信服务端 API 的自部署待办提醒服务。支持手机网页精准录入与微信/企业微信聊天框 `/ai <要求>` 自然语言指令，到点由服务器推送到微信（微信插件）与桌面端企业微信，未确认时按设定间隔持续催办，直至点击确认链接或回复 AI 完成。

核心零成本运行（可选接入 DeepSeek 等 OpenAI 兼容大模型实现 `/ai` 命令），无第三方推送服务、单容器部署。

## 功能特性

- 网页表单录入：快捷时间按钮、日历选择器与滚轮时间选择（仅允许选择未来时间），无自然语言解析误差
- `/ai` 微信聊天指令（可选）：在微信插件或桌面企业微信聊天框发送 `/ai <要求>`，支持增删改查、延后、完成与待办总结（兼容 DeepSeek 等所有 OpenAI 协议接口）
- 双端提醒：到点推送至手机微信（微信插件）与桌面企业微信客户端
- 间隔催办：未确认时按 5 / 10 / 30 / 60 分钟反复推送，点击消息内确认链接即停止
- 免打扰时段：默认**关闭**（夜间照常催办）；开启后催办在设定时段内静默顺延，时段可配置（默认 22:00–8:00）
- 重复任务：支持每天与每周固定星期几，催办或延后不破坏原重复日程，确认后自动排入下一轮
- 每日心跳：每天定时推送当日待办摘要，兼作服务在岗检测
- 持久化与恢复：SQLite 单文件存储，服务重启后未完成任务自动补发

## 工作原理

```
手机浏览器 ──表单──────────────────┐
                                   ▼
微信/企微 ── /ai 指令 ──► FastAPI + APScheduler（Docker 容器）
                          │  ├─ SQLite（任务持久化 + 回调去重）
                          │  └─ 可选 AI 引擎（DeepSeek / OpenAI 兼容接口）
                          ▼
              企业微信服务端 API（自建应用，官方接口）
                          │
          ┌───────────────┴───────────────┐
          ▼                               ▼
  手机微信（微信插件）              桌面企业微信客户端
```

## 环境要求

- 一台安装 Docker 的服务器（1C1G 即可）
- 一个属于自己的企业微信：在 [work.weixin.qq.com](https://work.weixin.qq.com) 免费注册，企业名称随意
- 一个域名：企业微信回调接口官方要求必须使用域名，不支持 IP

## 部署步骤

> 首次部署建议先看 [部署说明.md](部署说明.md) —— 约 15 分钟的完整 SOP，含暴露方式选型、可信 IP 解锁、`/ai` 配置与排错表。下面是精简版概览。

### 一、企业微信配置

1. 登录[管理后台](https://work.weixin.qq.com)，在「应用管理」创建自建应用，记录三项凭据：`corpid`（「我的企业」页）、应用 `secret` 与 `agentid`；应用可见范围设置为仅自己
2. 「我的企业」→「微信插件」：用个人微信扫码关注，此后应用消息将直接出现在微信会话列表
3. 电脑安装企业微信客户端并登录同一账号，用于接收桌面端提醒

### 二、服务端部署

```bash
git clone https://github.com/HGT158/wecom-reminder.git
cd wecom-reminder
cp config.example.yaml config.yaml
```

编辑 `config.yaml`：

| 配置项 | 说明 |
|---|---|
| `server_token` | 网页与确认链接的访问令牌，请设置为随机长字符串 |
| `public_base_url` | 对外访问基地址，建议使用 `https://<你的域名>` |
| `wecom.corpid / secret / agentid` | 第一步获取的企业凭据 |
| `wecom.callback_token / encoding_aes_key` | 回调验证与接收 `/ai` 消息凭据，见下文第三步 |
| `push.quiet_enabled` | 免打扰开关，默认 `false`（关闭，夜间照常催办） |
| `push.quiet_start / quiet_end` | 免打扰时段，仅在开关打开时生效，默认 22:00–8:00 |
| `push.max_nags` | 单次提醒催办次数上限，默认 48 |
| `heartbeat` | 每日待办摘要推送时间，默认 08:00 |
| `ai.enabled / base_url / api_key / model` | `/ai` 命令开关与 OpenAI 兼容模型配置（默认 DeepSeek）；`allowed_users` 可限制触发 `/ai` 的企微 UserID |

启动服务：

```bash
docker compose up -d --build
```

端口说明：`docker-compose.yml` 默认将容器端口绑定到 `127.0.0.1:8443`（仅本机回环），需配合宿主机 nginx 等反向代理对外提供 80/443 服务。若暂无反代，可将 ports 改为 `"8443:8000"`，直接以 `http://<域名>:8443` 访问与回调（简单模式，无 HTTPS）。**下文所有地址按反代模式书写；选简单模式时请把 `https://<域名>` 换成 `http://<域名>:8443` 并放行 TCP 8443。**

### 三、回调验证与可信 IP

1. 企业微信管理后台 → 应用详情 →「接收消息」→「设置API接收消息」，加密方式选择**安全模式**，记录页面生成的 Token 与 EncodingAESKey（先不要保存）
2. 将两项值填入 `config.yaml` 的 `wecom.callback_token` 与 `wecom.encoding_aes_key`，执行 `docker compose restart`
3. 返回后台填写回调 URL：`https://<域名>/wecom/callback`，保存后自动完成验证
4. 应用详情 →「企业可信IP」→ 添加服务器公网 IP（未配置时调用接口会返回 60020 错误）

### 四、验收

1. 手机浏览器打开 `https://<域名>/?k=<server_token>`，新建一条 2 分钟后的提醒：
   - 到点手机微信与桌面企业微信客户端同时收到推送
   - 点击消息内的确认链接，任务标记完成、催办停止
   - 重启容器后未完成任务自动恢复，错过的提醒补发并标注
2. 若配置并开启了 `ai.enabled: true`，在微信插件或桌面企微自建应用聊天框发送：
   - `/ai 明天上午9点提醒我交周报，每30分钟催一次`
   - `/ai 今天还有哪些未完成待办？`
   - 几秒后应收到 AI 执行结果并附带快捷确认/编辑链接

## 日常运维

```bash
docker compose logs -f --tail 100     # 查看日志
docker compose up -d --build          # 更新代码后重建
cp data/reminder.db data/backup.db    # 备份（单文件即全部数据）
```

建议通过 crontab 对 `data/reminder.db` 做每周定时备份。

## 安全说明

- `config.yaml`（含全部凭据与 AI `api_key`）已加入 `.gitignore`，不会进入仓库；部署时请勿提交
- 网页访问与确认链接均需携带 `server_token`
- `/wecom/callback` 对所有收到的 `/ai` 请求执行企微签名校验、AES 解密校验、`MsgId` 幂等去重，并支持 `ai.allowed_users` 白名单控制
- `/ai` 仅允许操作待办数据库（增删改查、总结、延后），且单次最多删除 2 条任务，不开放任何宿主机或容器 Shell 能力
- 容器时区固定为 Asia/Shanghai，提醒时间按北京时间解析
- 服务器仅需对外开放 80/443（或简单模式的 8443），应用端口仅监听本机回环

## 技术栈

FastAPI · APScheduler · SQLite · 企业微信服务端 API · OpenAI 兼容协议（可选）· Docker

## 许可证

本项目基于 [MIT License](LICENSE) 开源，可自由使用、修改与分发。
