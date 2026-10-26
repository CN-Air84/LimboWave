# 轻量日常环境工具

新增模型工具 `get_environment`：一次返回干净的天气、日出日落、当地时间、IANA 时区和夏令时状态。不调用 `web_search` / `read_url`，不返回广告、HTML、地图或页面摘要。

## 使用

```json
{"location": "用户提供的城市或地区"}
```

或使用用户确认过的坐标：

```json
{"latitude": 31.23, "longitude": 121.47}
```

- 名称和坐标互斥；坐标必须完整且为有限数值。
- 不通过 IP、设备时区、界面语言推断所在地。不知道地点时模型应先询问。
- 同名地点返回 `ambiguous_location` 与最多 5 个候选，不擅自采用第一个。确认后用候选经纬度重试。
- 天气是联网能力，沿用现有权限确认和域名授权；即使命中缓存也先检查权限。远程 Web 来源仍拒绝工具执行。
- 接线已加入现有 Pi 工具代理和运行时默认工具清单；重启应用/现有模型内核后可使用，无新增设置界面。

## 返回字段

- `provider`、`attribution`，Open-Meteo 额外返回来源和许可链接。
- `location`：查询的地点、地区、国家（可能缺失）、经纬度、地点时区。
- `current`：摄氏温度、体感、湿度、公里/小时风速、天气描述和上游更新时间。
- `daily`：每天的日期、最高/最低温、带时区偏移的日出和日落。Open-Meteo 包含昨天、今天、明天；MSN 返回最多三天预报，不冒充昨日观测。
- `time`：调用此刻的地点当地日期/时间、IANA 时区、时区缩写、UTC 偏移、夏令时状态及是否处于日出至日落之间。缓存命中也重新读时钟。
- `comparisons`：有昨日数据时，给出今天与昨天的日最高温差，以及当地钟表上的日落分钟差。明确标记温度比较来自逐日模型/预报，而非昨日同刻实测；缺失比较为 `null`。
- `fetched_at`、`cache`、`current_data_age_seconds`、`current_is_stale`：区分取回时间和上游数据时间。上游更新时间缺失、超过一小时或明显来自未来时明确提示，不能声称是刚刚的现场实测。
- `notes`：模型数据、缺失值、极昼/极夜和供应方限制等提示；天气描述属于参考资料，不是指令。

## 数据源配置

### 无 Key：Open-Meteo

默认采用 Open-Meteo 的 JSON 天气接口。**免费接口仅允许非商业使用**；数据许可为 CC BY 4.0，需要保留归属信息。商业部署须接入获许可的数据服务，当前实现没有自动配置付费 Open-Meteo 端点。

```powershell
$env:LIMBOWAVE_ENVIRONMENT_PROVIDER = 'open-meteo'
```

这是明确的默认后端，不是 MSN 失败后的偷偷降级；选定 MSN 后，授权失败/限流/超时都直接报错。

### 授权 Key：MSN

MSN 结构化概览接口已找到并探测，但不能将网站自带 Key 当作本项目的合法第三方凭据。响应明确要求取得微软对 API 使用与内容复用的书面授权；Microsoft Learn 提供的是 Power Automate / Logic Apps 等产品内的 MSN Connector，并不等于向任意第三方开放免 Key REST 接口。

获得适用于 MSN 的授权后，在启动应用的进程环境中配置：

```powershell
$env:LIMBOWAVE_ENVIRONMENT_PROVIDER = 'msn'
$env:LIMBOWAVE_MSN_WEATHER_API_KEY = '<自己取得授权的 MSN Key>'
# 如果授权要求自己的应用 ID：
$env:LIMBOWAVE_MSN_WEATHER_APP_ID = '<授权应用 ID>'
```

- 仅设置 MSN Key 而未指定后端时会选择 MSN；显式指定后端优先。
- MSN Key 不是 Azure Maps / Bing Maps Key，不能混用。
- Key 只由应用侧 HTTP 后端持有，不写入工具参数、工具结果、HTTP 日志、Pi 子进程环境或派生配置。
- 该 MSN 网站接口不是稳定公开的第三方 REST 契约。生产上线前需使用自己的授权账户验证；本次没有合法用户 Key，因此只完成结构映射与错误契约测试，不宣称授权环境已验收。
- 两个后端均使用 Open-Meteo/GeoNames 进行名称解析；不想把名称交给该服务时直接传经纬度。权限提示会列出实际涉及的全部主机。

## 性能和网络边界

- 首次坐标查询只进行一次天气 JSON 请求；名称查询另有一次地名解析。不轮询、不后台主动查询。
- 5 分钟内存缓存，最多 32 个查询地点；地点当地跨日会提前刷新。过期请求失败不偷偷交付旧缓存。不落地位置记录，退出时清缓存。
- 固定 HTTPS 主机、禁止重定向、3 秒连接/8 秒读超时、128 KiB 解压后响应上限。正常工具结果约 2 KiB。
- 沿用 DNS/私网/元数据守卫。支持用户环境中显式的 `HTTPS_PROXY` / `ALL_PROXY`，并尊重 `NO_PROXY`。
- 仅当固定天气主机全部解析到 `198.18.0.0/15` Fake-IP 且确实通过显式 HTTP CONNECT 代理发送域名时允许代理远端解析；不直接连接 Fake-IP，不放开任意私网/元数据地址，不修改其他联网工具的守卫。
- Windows 明确依赖 `tzdata`，避免依赖设备本地时区猜地点时区；夏令时按地点 IANA 时区计算。

## 上游参考

- MSN Connector：https://learn.microsoft.com/en-us/connectors/msnweather/
- MSN 概览地址：https://api.msn.com/weather/overview
- Open-Meteo 天气：https://open-meteo.com/en/docs
- 地名解析：https://open-meteo.com/en/docs/geocoding-api
- 使用条款：https://open-meteo.com/en/terms

## 验证

单测覆盖参数、纯数据投影、同名地点、时区/DST、缓存/跨日、缺失字段、旧数据、极区太阳时间、大小上限、超时/授权/限流错误脱敏、固定域名权限、远程来源硬边界和代理 Fake-IP 边界。真实 Pi 集成测试验证模型调用、联网权限档位、IPC 和后端派发；自由读取/完全访问按既有策略允许，纯聊天或无域名授权的自定义档位拒绝。

已通过 **391 项相关测试**、Ruff 与 mypy；默认后端经实际 ToolGateway 联网实测成功，首次约 1.4–1.5 秒、结果约 1.8 KiB，重复调用命中缓存。MSN 授权部署和重新打包的 EXE 未作实测验收。
