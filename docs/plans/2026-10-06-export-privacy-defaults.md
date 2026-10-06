# 会话导出隐私默认值 Implementation Plan

**Goal:** 会话导出默认不写入模型、站点与路由原因，HTML 可显式勾选保留。
**Architecture:** ExportService 新增默认关闭的 include_model_info 参数；ExportPanel 通过回调传递选项，应用层同步生成匹配的隐私提示。其他文本格式不新增元数据，正文、思考、图片与工具步骤不变。
**Tech Stack:** Python / PySide6 / pytest。

1. 在 tests/unit/test_export_service.py 覆盖默认隐藏、显式保留、全部格式与批量导出；在 tests/ui/test_floating_panels.py 覆盖默认未勾选和格式切换。
2. 修改 src/limbowave/application/services/export_service.py，仅在显式允许时渲染模型、站点和路由原因，并更新隐私提示。
3. 修改 src/limbowave/ui/export_panel.py 和 src/limbowave/app.py，接通仅 HTML 可用的选项。
4. 运行导出与悬浮窗测试、相关集成测试和静态检查，确认默认导出文件源码中不存在模型与站点元数据。

## 验证结果

- 导出服务、悬浮窗和故障注入专项测试：69 passed。
- 修改文件 Ruff 检查通过；3 个源文件 mypy 检查通过；git diff --check 通过。
- 离屏渲染检查导出面板，新增选项默认未勾选，布局正常。
- 全量测试在 300 秒后超时，未完成；首个失败定位到 tests/ui/test_checkbox_style.py::test_unchecked_and_checked_both_draw_a_visible_box（离屏像素断言失败，Qt 同时提示缺少字体目录）。单独运行该测试可复现；未改动复选框绘制实现。
