# User Experience Improvements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让主要聊天操作有可见反馈，保护正在编辑的内容，解释不能发送的原因。

**Architecture:** 保留 PySide6 / React 的现有布局、主题令牌和业务层。展示态保留在组件中，草稿仅存内存，不新增明文持久化；沿用服务端权限与回执机制。工作区有既有未提交修改，原地做最小增量，不切换分支或覆盖其他工作。

**Tech Stack:** Python / PySide6 / pytest-qt；TypeScript / React / Testing Library / Vitest。

---

## 取舍
- 推荐：先修复反馈缺失、草稿丢失和发送入口不一致，改动小，收益可验证。
- 暂不做整体视觉重设计：会改变用户已熟悉的界面，且不能解决上述问题。
- 暂不合并桌面和网页的所有交互：两端的安全、输入与并发约束不同。

### Task 1: 桌面操作反馈
**Files:** `src/limbowave/ui/chat_view.py`, `tests/ui/test_composer_usability.py`
1. 写失败测试：重要状态可见、纯文本展示、关闭不会影响草稿、就绪不占空间。
2. 运行新测试确认失败。
3. 在输入区增加使用现有主题令牌的可关闭提示；保留 status_text 接口。
4. 运行聊天视图、历史加载及主题相关回归。

### Task 2: 暂时不可用时允许写草稿
**Files:** `src/limbowave/ui/chat_view.py`, `tests/ui/test_chat_view.py`, `tests/ui/test_composer_usability.py`
1. 测试模型不可用时文字仍可编辑，发送仍阻止，恢复后不自动发送。
2. 分开文本编辑与模型/附件能力的启用条件；保留只读、历史加载和压缩锁定。
3. 显示不能发送的原因和恢复方式；增加输入框与图标按钮可访问名称。
4. 验证粘贴/拖放不能绕过禁用的附件功能。

### Task 3: 编辑旧消息时保护原草稿
**Files:** `src/limbowave/ui/chat_view.py`, `src/limbowave/app.py`, `tests/ui/test_composer_usability.py`, `tests/ui/test_chat_view.py`
1. 测试进入编辑后取消恢复原文字和附件，多次编辑以及切换会话不串草稿。
2. 为 ComposerDraft 增加编辑前草稿快照；首次编辑保存，取消恢复，提交后清除编辑备份。
3. 调整应用接线，必须先保存原草稿，再加载旧消息附件。
4. 验证发送旧消息仍携带旧消息附件，不误用原草稿附件。

### Task 4: 网页发送的一致性和可解释性
**Files:** `web/src/components/Composer.tsx`, `web/src/components/Composer.test.tsx`
1. 参数化测试空/失效模型、busy、disabled、running 时点击/回车/表单提交一致。
2. 共用 canSend 判断；无模型或已删除模型给出可执行指引，不静默无响应。
3. 保留 IME 输入和手机回车换行；桌面显示快捷键提示。
4. 不更改服务端鉴权、断线命令回执和控制器中的既有修改。

### Task 5: 全面验证与记录
**Files:** `docs/audits/2026-10-14-user-experience-review.md`
1. 跑定向 Python / Web 测试、类型检查、代码检查及构建。
2. 离屏渲染检查桌面提示、编辑态和长文案；不启动用户真实资料库。
3. 记录项目主要用户路径、已修问题及需要后续产品决策的事项；明确验证边界。
4. 不自动提交已有工作区内容。
