# Tool-step visibility fade implementation plan

**Goal:** 工具步骤全局显隐时渐隐/渐显，保留详情和审计数据。

**Architecture:** 在消息行内用一条可反向的 220ms QVariantAnimation 同步工具区和分段线的透明度。正文不参与淡化；淡出结束后才隐藏工具区和纯工具分段。流式更新沿用当前进度；隐藏窗口或禁用系统动画时立即结算，并移除临时绘图效果。

**Tech Stack:** Python / PySide6 / pytest-qt.

## Steps
1. 在 tests/ui/test_tool_step_visibility_animation.py 添加双向淡化、反向切换、流式更新、隐藏生命周期、无动画回退回归测试，先验证失败。
2. 修改 src/limbowave/ui/chat_view.py 的工具展示状态同步、动画和隐藏清理；不改工具数据及详情展开逻辑。
3. 更新 tests/ui/test_tool_steps_ui.py 原来要求同步隐藏的断言，等待淡出收尾。
4. 运行相关 UI 测试和完整 UI 回归，检查静态代码问题。

## Trade-offs
- 直接 setVisible 无法提供渐隐；对整张回复卡做淡化会错误影响正文。
- 选择仅淡化工具区域及分段线，淡出后移除占位；不重建工具组件；后续加入同时间线的高度、间距及滑动过渡。

## Follow-up: slide and layout motion
- 工具内容保留自然高度，在可裁剪视口里向上移出/向下移入；视口高度与透明度共用时间线。
- 同步插值工具区和分段线周围的布局间距，避免动画结束时背景板仍跳变。
- 覆盖纯工具段、正文内工具段、历史工具先于正文、展开详情、反向切换和窗口缩放。
