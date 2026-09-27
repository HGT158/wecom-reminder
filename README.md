# 企业微信待办提醒（自部署版）

外出没带电脑时，用手机浏览器建一条待办，到点通过**企业微信官方 API** 把提醒推到你的手机微信和电脑企微客户端；未确认则按设定间隔反复催办，直到你在微信里点一下确认链接。

纯代码实现（无 LLM、无第三方推送服务、零运行成本），单容器部署。

## 功能

- 📱 手机网页表单录入（快捷时间按钮 + 日历 + 滚轮时间选择，无解析错误）
- ⏰ 到点推送：手机微信（微信插件）+ 电脑企业微信客户端 双端提醒
- 🔁 间隔催办：未确认时按 5/10/30/60 分钟反复催，直到点确认链接
- 🌙 免打扰：催办默认 22:00–8:00 静默顺延（config.yaml 可调）
- 🔁 重复任务：每天 / 每周几，完成后自动排下一次
- ☀️ 每日心跳：每天早上推送"今日待办 N 件"，收不到即知服务异常
- 💾 SQLite 单文件存储，重启自动恢复，错过的提醒补发

## 架构

```
手机浏览器 ──表单──► FastAPI + APScheduler (Docker)
                        │ SQLite
                        ▼
              企业微信 API（官方，需自建应用）
                        │
        手机微信(微信插件) + 电脑企微客户端
```

## 部署（给自己或朋友）

> 前提：一个属于自己的**企业微信**（work.weixin.qq.com 免费注册，企业名随意），
> 一台装了 Docker 的服务器，一个域名（企微回调官方要求必须域名，IP 不行）。

### 1. 企业微信侧（约 15 分钟）

1. 管理后台 → 应用管理 → 创建自建应用，记下 `corpid`（我的企业页）、`secret`、`agentid`；可见范围设为仅自己
2. 我的企业 → 微信插件 → 用个人微信扫码关注（之后应用消息直接出现在微信里）
3. 电脑装企业微信客户端并登录（接收 PC 端弹窗）

### 2. 服务器侧

```bash
git clone https://github.com/HGT158/wecom-reminder.git && cd wecom-reminder
cp config.example.yaml config.yaml
# 编辑 config.yaml：填 corpid/secret/agentid/server_token(随机串)/public_base_url
docker compose up -d --build
```

> 端口说明：compose 默认 `127.0.0.1:8443:8000`（仅回环，配合宿主机 nginx 443 反代）。
> 没有反代的话，把 ports 改成 `"80:8000"`，直接用 `http://域名` 访问与回调（简单模式，无 HTTPS）。

### 3. 企业微信后台收尾

- 应用详情 → 接收消息 → 设置API接收消息：安全模式，生成 Token/EncodingAESKey
  → **先**填进 config.yaml 的 `wecom.callback_token / encoding_aes_key` 并 `docker compose restart`
  → **再**回后台填 URL `https://<域名>/wecom/callback` 保存（验证通过才能进行下一步）
- 应用详情 → 企业可信IP → 添加服务器公网 IP

### 4. 验收

手机打开 `https://<域名>/?k=<server_token>` 建一个 2 分钟后的提醒：
到点手机微信 + 电脑企微同时收到 → 点消息里的确认链接 → 任务完成、催办停止。

## 配置说明（config.yaml）

| 项 | 说明 |
|---|---|
| server_token | 网页与确认链接的访问令牌，务必随机长串 |
| public_base_url | 确认链接基地址，用你的 https 域名 |
| wecom.* | 企微凭据（corpid/secret/agentid + 回调 Token/AESKey） |
| push.quiet_start / quiet_end | 催办免打扰时段（默认 22–8 点） |
| push.max_nags | 单次提醒最多催办次数（默认 48，防僵尸任务） |
| heartbeat | 每日"今日待办"摘要推送，兼作在岗心跳 |

## 安全与数据

- **config.yaml 不入库**（.gitignore 已排除），凭据只存在服务器上
- 任务数据在 `data/reminder.db` 单文件，每周备份它即可
- 时区固定 Asia/Shanghai（容器内）

## 技术栈

FastAPI · APScheduler · SQLite · 企业微信服务端 API · Docker
