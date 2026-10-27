# OOBE 新板块统一验收入口

**仅演示，未接入正式 OOBE。所有新板块等用户统一验收通过后再实装。**

推荐运行 `tools/demos/start_oobe_joint_demo.ps1`，或：

```powershell
& G:\LimboWave\.venv\Scripts\python.exe G:\LimboWave\tools\demos\oobe_joint_demo.py
```

该入口继承当前共享 `voice_oobe_demo.py` 的全部页面，不复制语音/其他介绍。
联网介绍默认打开；可从左侧跳转到语音和已有页面。之后共享稿增减页面，合集跟随复用。
联网内容区在最小窗口可滚动，页脚按钮保持可见；不会为了排版修改正式引导。

- 默认关闭搜索；模拟未配置、逐次授权、搜索摘要、再次授权读文、拒绝和失败。
- Google/Bing 明确标记经 SerpAPI；Tavily 使用独立服务 Key，可能计费。
- 明确真实入口“设置 → 联网搜索”，只读抓取、来源/时间、加密密钥和隐私边界。
- 全部交互是假数据，不联网、不打开示例链接、不输入真实 Key、不保存应用配置。
- 演示中的允许不是正式产品授权；离开页面会清除模拟授权。

验证：

```powershell
$env:QT_QPA_PLATFORM='offscreen'
& G:\LimboWave\.venv\Scripts\python.exe -m pytest G:\LimboWave\tools\demos\test_network_oobe_joint.py -q
```

截图生成参数：`--screenshots G:\LimboWave\.tmp\oobe-joint-net01`。
