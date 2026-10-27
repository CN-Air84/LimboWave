# OOBE 新板块合集 · 语音 / 联网查证（待验收）

此目录仅用于评审；**未修改正式 OOBE，不读取或保存应用配置**。

## 打开

```powershell
& G:\LimboWave\.venv\Scripts\python.exe G:\LimboWave\tools\demos\voice_oobe_demo.py
```

也可运行同目录的 `start_voice_oobe_demo.ps1`，不弹控制台。

## 本轮方案

- 在原有“欢迎介绍”与“接入服务商”之间增加一屏语音介绍。
- 沿用原有 OOBE 深色背景、主题色、标题、按钮和左对齐文案；可切浅色。
- 右侧可以模拟“按住 → 松开转写 → 编辑 → 确认发送 → 模拟朗读 → 停止”。
- 不打开麦克风、不联网、不播放声音；模拟内容明确标注。
- 可选的设置页预览 API / 本机兼容服务、地址、模型和掩码密钥；请勿输入真实密钥。
- “模拟保存”只记住 demo 内的两个开关与服务方式，清空密钥输入，不写磁盘。
- 左侧流程对照和顶部“待验收”为演示壳，不属于拟上线产品界面。

## 重点验收

1. “不想打字的时候，也可以说给我听”这句主标题和语气是否合适。
2. 单屏三点说明与右侧交互是否过重；是否保留可选配置分支。
3. 默认关闭、校对再发、主动朗读、一键停止、云端数据说明是否清晰。
4. “知道了，继续”直接进入原来的模型服务配置，不强迫启用语音。

## 检查

```powershell
$env:QT_QPA_PLATFORM='offscreen'
& G:\LimboWave\.venv\Scripts\python.exe -m pytest G:\LimboWave\tools\demos\test_voice_oobe_demo.py -q
```

8 项交互检查通过，深浅色和配置页已截图检查。验收通过前，不接入 `oobe_copy.py`、`oobe_demo.py` 或 `oobe_live.py`。


## 联合验收顺序（2026-10-27）

原有欢迎 → 语音陪伴 → 联网查证 → 原有服务商接入。
三个新增板块在同一个窗口中往返；继续使用上述启动命令 / 脚本，不需开多份 demo。
正式 `oobe_copy.py`、`oobe_demo.py`、`oobe_live.py` **没有接入新板块**，等用户统一验收后再实装。

### 联网查证板块

- 主标题：“新消息，我们一起查证。”不知道的，不装作知道。
- 默认未配置状态；勾选“模拟已配置”只影响 demo，不启用真实联网。
- 模拟搜索授权 → 摘要 → 单独授权读文 → 正文来源；允许、拒绝、失败都可体验。
- 明确 Google/Bing 经第三方 SerpAPI、Tavily 独立 Key、可能计费、正式入口和隐私边界。
- 不输入真实 Key，不加载真实网页，不调用真实 HTTP/搜索/记忆/语音服务，不写应用配置。
- `example.invalid` 是虚构示例地址，不会打开；模拟查阅时间不是实际查询或新闻发布日期。
- 离开页面、更换服务或重置均清除模拟授权；验收动作不代表同意正式产品联网。

可生成整套截图：

```powershell
$env:QT_QPA_PLATFORM='offscreen'
& G:\LimboWave\.venv\Scripts\python.exe G:\LimboWave\tools\demos\voice_oobe_demo.py --screenshots G:\LimboWave\.tmp\oobe-joint-net01
```

历史记录中的“8 项检查”是语音初版的检查数；请以当前测试结果为准。
