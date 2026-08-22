# 多平台 AI Newsletter 部署与运营

本分支在 ainews-open 的采集、正文提取、SQLite、OpenAI 兼容模型、后台和测试基础上，增加了“统一期刊 + 渠道变体 + 独立发布记录”的多平台链路。项目继续遵守仓库中的 MIT License 和原版权声明。

## 每日运行时序

- 05:00 `ainews collect-daily`：冻结过去 24 小时窗口，采集、提取、去重、排序，生成唯一 `CanonicalIssue`。不足 5 条时状态为 `blocked`，当天不发送。
- 05:50 `ainews prepare-daily`：按 `channels.yaml` 为 `review` / `manual_export` 渠道生成网页预览、微信草稿和人工素材 ZIP。
- 06:00 `ainews deliver-daily`：只处理明确设为 `mode: auto` 的渠道。渠道互相隔离，单个失败不会阻断其他渠道。

systemd 模板位于 `deploy/systemd/`，默认工作目录是 `/opt/ainews`。复制后执行：

```bash
sudo cp deploy/systemd/ainews-* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ainews-collect.timer ainews-prepare.timer ainews-deliver.timer
systemctl list-timers 'ainews-*'
```

## 首次配置

1. 复制 `.env.example` 为 `.env`，填写域名、SMTP、Tavily、LLM 和公众号凭据。
2. 将公众号服务器固定公网 IP 加入微信公众平台白名单，并配置管理员预览 OpenID。
3. 保持 `channels.yaml` 中网页和微信为 `review`，运行 `docker compose up -d --build`。
4. 配置域名 DNS 指向服务器；Caddy 自动申请 HTTPS 证书。公开期刊固定地址为 `https://<域名>/issues/YYYY-MM-DD`。
5. 检查 `output/previews/site/`、微信管理员预览和 SMTP 告警。连续 7 个成功审核日后，通过带 `X-Admin-Token` 的受保护接口手工切换模式。系统不会自行切换：

```bash
curl -X PATCH https://<域名>/admin/channels/static_site/mode \
  -H 'X-Admin-Token: <后台令牌>' -H 'Content-Type: application/json' -d '{"mode":"auto"}'
curl -X PATCH https://<域名>/admin/channels/wechat/mode \
  -H 'X-Admin-Token: <后台令牌>' -H 'Content-Type: application/json' -d '{"mode":"auto"}'
```

微信接口会在服务端再次检查审核天数；未达到门槛时返回 400。也可通过 `GET /admin/channels` 查看当前配置。

如需在 7 天前进行受控联调，可临时设置 `AINEWS_WECHAT_ALLOW_EARLY_AUTO=true`；生产环境应恢复为 `false`。

## 微信语义

系统使用 wechatpy 处理认证、素材和群发接口，调用顺序是：

```text
draft/add → message/mass/preview → message/mass/sendall → message/mass/get
```

`freepublish/submit` 只会“发布”文章，不会主动触达粉丝，因此本实现不调用它。正式群发固定使用 `is_to_all=true`、`send_ignore_reprint=0`，并生成 `ainews-YYYYMMDD-vN` 形式的 `clientmsgid`。数据库幂等键为 `(issue_id, channel, account_id, content_version)`；05:50 创建的草稿会在 06:00 被复用。

## 网页与隐私

审核模式写入 `output/previews/site`，Caddy 只挂载 `output/site`。正式模式生成首页、日期归档、`latest.json`、Atom `feed.xml` 和 `robots.txt`。渠道链接只增加 `utm_source`、`utm_medium` 和 `utm_campaign`，统计可直接使用 Caddy 访问日志，不设置第三方追踪 Cookie。

## 后续渠道

- 小红书：启用 `xiaohongshu` 并保持 `manual_export`，系统会在 `output/exports/xiaohongshu/` 生成 ZIP。管理员在官方 App 中核对发布；系统不保存 Cookie、不模拟登录。
- X：配置和能力声明已预留但默认禁用。接入时应实现 `PublisherAdapter`，使用 OAuth 2.0 可刷新令牌和官方 `/2/tweets`；余额不足只告警。
- 其他平台：实现 `prepare`、`validate`、`preview`、`publish`、`fetch_status` 契约并注册适配器。适配器只读取已确认的 `CanonicalIssue`，不能再次生成或改写新闻事实。

## 常用检查

```bash
uv sync --python 3.12 --extra dev
uv run pytest -q
uv run ruff check src tests
uv run ainews collect-daily
uv run ainews prepare-daily
uv run ainews deliver-daily
```

SQLite schema 14 新增 `issues`、`channel_variants`，并扩充 `publications`。迁移保持现有文章和摘要数据兼容；发布错误、外部消息 ID、重试状态及脱敏后的响应会持续写入数据库。
