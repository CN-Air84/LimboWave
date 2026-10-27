# NET-01：真实受控的搜索与网页抽取

## 配置与使用

1. 解锁本地资料库，打开 **设置 → 联网搜索**。
2. 选择 **Google / Bing · 经 SerpAPI**、**Tavily**、**Microsoft Azure · Bing Web Search（旧版）**
   或 **自定义 · JSON 搜索 API**，填入对应服务的 API Key。
   - SerpAPI 选项使用第三方 SerpAPI Key，不接受 Google / Azure 原生 Key。
   - Azure 旧版选项使用 `Ocp-Apim-Subscription-Key` 调用
     `https://api.bing.microsoft.com/v7.0/search`。Bing Search APIs 已于 **2025-08-11** 退役，
     此项只用于仍可调用的既有资源兼容，不承诺新账户能够开通或继续可用。
   - 自定义选项仅允许公网 HTTPS GET 地址；应用追加 `q` 和 `count` 参数，并发送
     `Authorization: Bearer <Key>`。响应必须是 JSON 对象，包含
     `results[{title,url,snippet,published_date?}]`。不允许配置任意请求头、请求体或重定向。
3. 设置返回条数（1–10，默认 5），保存后立即生效，无需重启。
4. 模型调用 `web_search` 时，搜索词经现有权限网关逐次确认；域名授权包括 `*` 不会免除搜索确认。
   搜索词会被发送给所选服务，可能产生该服务费用。应用不会自动开户、购买、切换服务或重试收费请求。
5. `read_url` 只读取获授权的公开网页，不需要搜索 Key；清洗正文、移除脚本与导航、保留最终 URL 和查阅时间。

搜索默认关闭；没有配置凭据、资料库未解锁、用户拒绝、额度不足、HTTP 错误或无法抽取正文时均返回明确错误。
设置显示“已配置”只代表存在密钥引用，**不代表在线验证成功**。
密钥输入框不会回填明文，切换服务或隐藏页面会清除未保存输入；更换服务需重新填写对应 Key。
“清除密钥并关闭搜索”同时关闭搜索及移除配置引用。普通配置只保存 `credential_ref`，不含密钥。

## 官方 API 状态与兼容边界

官方文档核验于 **2026-10-27**：

- Bing Search APIs 于 **2025-08-11** 停用，不适合作为新功能依赖。
  https://learn.microsoft.com/en-us/lifecycle/announcements/bing-search-api-retirement
- Google Custom Search JSON API 不再接受新客户，既有客户需在 **2027-01-01** 前迁移，
  因此本项目不提供该 API 的官方直连接入。
  https://developers.google.com/custom-search/v1/overview
- 本实现默认推荐有明确凭据的 SerpAPI / Tavily；同时保留 Azure Bing v7 旧资源兼容项，
  但界面明确标注退役状态，不把它推荐给新用户。
  https://serpapi.com/search-api
  https://serpapi.com/bing-search-api
  https://docs.tavily.com/documentation/api-reference/endpoint/search

模型原生 grounding 暂不实现：它依赖各模型供应商的请求参数、计费、执行授权及引用协议，
不能通过切换一个布尔值就等价于现有工具权限边界。禁止静默降级到抓取搜索结果 HTML 或其他供应商。

## 返回证据与诚实表达

- `web_search`：`provider`、`engine`、`provider_label`、`query`、`retrieved_at`、
  `results[{source_id,title,url,snippet,published_date?}]`、`count`、`omitted_results`、`truncated`。
  `evidence_type=search_snippets`，只能说“我刚刚搜索到……”，不能声称已经阅读全文。
- `read_url`：`url`（实际最终 URL）、`title`、`text`、`status`、`content_type`、`bytes`、
  `retrieved_at`、`truncated`、`evidence_type=page_content`。
  成功后可说“我刚刚查阅了这篇文章，文章提到……”，并给出来源；截断时不能称完整全文。
- 所有网络内容标记为 `untrusted_external_content`，返回 notes 明确指出外部证据不是指令。
  不执行文章内要求调用工具、读取本地文件、泄露秘密或覆盖用户指令的内容。
- `retrieved_at` 是本次取回时间（UTC ISO 8601），不是新闻发布时间。
  搜索服务提供的日期只是来源声明；日期缺失时不能推断“今天发布”。搜索引擎本身可能使用缓存。
- 失败通过 `ToolResult(ok=False, error=..., data={code,...})` 返回。
  凭据或额度错误不会回显供应商原始错误体、认证 URL、API Key 或传输异常详情。

## 安全与资源边界

- 仍以 ToolGateway 为唯一模型入口，先参数/来源/权限检查，再 DNS、凭据解析及 HTTP。
  Web 来源即使拿到确认或域名授权也不能执行这些工具。
- 页面只支持标准端口 HTTP / HTTPS，拒绝嵌入用户名密码、控制字符、非公网 IP、内网、
  回环、链路本地、云元数据、CGNAT、组播和可绕过地址检查的 IPv6 转换范围。
- DNS 结果全部检查；连接锁定已检查的数字 IP，TLS 证书和 SNI 仍使用原始主机名，
  不进行第二次主机名解析。不继承环境代理、Cookie、模型 Key 或系统登录状态。
- 每次重定向重新检查地址。最多 3 次同源跳转；跨来源（含 HTTPS 降级）返回
  `redirect_requires_permission` 和 `redirect_url`，由新的 `read_url` 另行授权。
  搜索 API 重定向全部拒绝，绝不转发凭据。
- 默认 20 秒请求预算；DNS 查询最多 4 个并发解析工作线程，避免超时解析无限堆积。
  页面原始响应最多 2 MiB，抽取文本默认 48 KiB；搜索 JSON 最多 1 MiB；网关还限制整体 JSON 输出。
  超限会标记截断或拒绝解析，不把截断 JSON 当完整搜索结果。
- 只接受 HTML/XHTML、纯文本、Markdown；请求 identity 编码，不接受压缩响应以避免解压炸弹。
  不执行 JavaScript，不加载图片/iframe，不提交表单，不绕过登录、反爬、付费墙。
- 内置搜索使用固定端点 `https://serpapi.com/search.json`、`https://api.tavily.com/search`
  与 `https://api.bing.microsoft.com/v7.0/search`。Azure 密钥只进订阅密钥请求头，不进 URL。
  Tavily 不请求生成式答案、原始全文或图片；返回结果经校验、去重、条数/摘要长度限制。
- 自定义端点仍经过公网 URL / DNS / 响应大小与 JSON 结构校验；只允许 Bearer Key 的固定认证方式，
  避免“自定义”变成任意头注入、内网请求或秘密转发通道。

## 代理 / fake-IP DNS 注意事项

若代理或 TUN 模式把公网域名解析为 `198.18.0.0/15` 等非公网地址，传输会返回
`blocked_address`，不会放宽 SSRF 检查。需在代理中为目标使用真实公网 DNS / 非 fake-IP 模式，
或使用能返回真实公网地址的受控网络。当前不支持自动继承 HTTP(S)_PROXY 或系统代理。
本机验证发现 example.com、serpapi.com、api.tavily.com 被解析到 198.18.*，因而实际取回被阻止；
这不是在线成功测试。不要把“适配器离线测试通过”等同于“已用真实 Key 完成搜索”。

## 主要实现与验证

- `domain/network.py`：配置、响应、安全错误和外部证据提示。
- `infrastructure/web_access.py` / `web_search.py`：公网传输和搜索适配。
- `application/services/web_content.py`：只读 HTML 清洗与编码处理。
- `network_settings_service.py` / `ui/network_settings_page.py`：加密凭据及设置入口。
- `infrastructure/network_backend.py` / `app.py`：IPC 启动前注入真实后端，逐次加载最新配置。
- `tool_gateway.py` / `domain/permissions.py`：输出预算、结构化错误和搜索逐次确认。
- `policy_enforcement.ts`：向模型说明近期信息核验、引用、隐私和诚实表述要求。

离线测试使用假传输验证服务参数、权限隔离、DNS 固定、重定向、超时、HTML 清洗、密钥保护及设置 UI。
付费供应商端到端搜索须由用户提供真实凭据后验证；没有凭据时不宣称搜索已经在线验证。


## 本轮验证与 OOBE

NET-01 针对性回归 134 项通过；Ruff 和13个受影响生产文件的 strict mypy 检查通过。
广回归存在14个跨模块失败和一个缺失包装模块收集阻断，未宣称全量全绿；完整交接见 `NET-01-HANDOFF.md`。
联网 OOBE 介绍已进入纯模拟的联合 demo，推荐入口 `tools/demos/start_oobe_joint_demo.ps1`；
不联网、不保存配置，正式 OOBE 等用户统一验收通过后再接入。
