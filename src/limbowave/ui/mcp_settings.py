"""Explicit, lazy MCP settings. Saving never starts a server."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.mcp_service import McpService
from limbowave.domain.mcp import McpConfiguration, McpError, McpServer
from limbowave.ui import theme
from limbowave.ui.background_tasks import BackgroundTasks
from limbowave.ui.floating import ask_confirm


class McpSettingsPage(QWidget):
    def __init__(self, service: McpService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service
        self._jobs = BackgroundTasks(self)
        self._servers: list[McpServer] = []
        self._dirty = False
        self._loading = False
        self._switching = False
        self._selected = -1
        self._testing = False
        self._panel: QWidget | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        title = QLabel("MCP · 连接外部能力")
        title.setStyleSheet(f"font-size: {theme.FS_TITLE}px; font-weight: 600;")
        root.addWidget(title)
        hint = QLabel(
            "只连接你信任的服务器。stdio 会运行本机程序，HTTP 会联系指定服务；这些程序不在沙盒中。"
            "保存和启动应用均不会建立连接。完全访问自动允许 MCP；自定义模式逐次确认。"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)
        row = QHBoxLayout()
        self.selector = QComboBox()
        self.selector.setMinimumWidth(230)
        self.new_button = QPushButton("新增服务器")
        self.delete_button = QPushButton("删除")
        row.addWidget(self.selector, 1)
        row.addWidget(self.new_button)
        row.addWidget(self.delete_button)
        root.addLayout(row)

        self.form_widget = QWidget()
        form = QFormLayout(self.form_widget)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)
        self.server_id = QLineEdit()
        self.server_id.setReadOnly(True)
        self.name = QLineEdit()
        self.name.setMaxLength(100)
        self.enabled = QCheckBox("启用此服务器（模型可发现并申请调用）")
        self.transport = QComboBox()
        self.transport.addItem("本地进程 · stdio", "stdio")
        self.transport.addItem("远程服务 · Streamable HTTP", "streamable_http")
        self.command = QLineEdit()
        self.command.setPlaceholderText("可执行文件名或完整路径，例如 python / npx")
        self.args = QPlainTextEdit()
        self.args.setPlaceholderText('["-m", "your_mcp_server"]  · JSON 数组，不是 shell 命令')
        self.args.setMaximumHeight(65)
        self.cwd = QLineEdit()
        self.cwd.setPlaceholderText("可选：服务器工作目录")
        self.env_refs = QPlainTextEdit()
        self.env_refs.setPlaceholderText('{"API_KEY": "MY_MCP_API_KEY"}  · 值为主机环境变量名称')
        self.env_refs.setMaximumHeight(65)
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://your-server.example/mcp（仅回环地址允许 HTTP）")
        self.header_refs = QPlainTextEdit()
        self.header_refs.setPlaceholderText(
            '{"Authorization": "MY_MCP_AUTH"}  · 环境变量值需含 Bearer 前缀'
        )
        self.header_refs.setMaximumHeight(65)
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 45)
        self.timeout.setSuffix(" 秒")
        for label, widget in (
            ("服务器 ID", self.server_id),
            ("显示名称", self.name),
            ("状态", self.enabled),
            ("连接方式", self.transport),
            ("可执行文件", self.command),
            ("参数（JSON）", self.args),
            ("工作目录", self.cwd),
            ("环境变量引用", self.env_refs),
            ("MCP 地址", self.url),
            ("请求头引用", self.header_refs),
            ("整次操作超时", self.timeout),
        ):
            form.addRow(label, widget)
        root.addWidget(self.form_widget)
        actions = QHBoxLayout()
        self.save_button = QPushButton("保存服务器")
        self.save_button.setProperty("accent", True)
        self.test_button = QPushButton("连接并发现工具…")
        self.reload_button = QPushButton("放弃未保存修改")
        for button in (self.save_button, self.test_button, self.reload_button):
            actions.addWidget(button)
        actions.addStretch(1)
        root.addLayout(actions)
        self.feedback = QLabel()
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        root.addWidget(self.feedback)
        self.results = QPlainTextEdit()
        self.results.setReadOnly(True)
        self.results.setPlaceholderText("尚未测试连接。结果仅代表这一次发现，不是持续在线状态。")
        self.results.setMaximumHeight(140)
        root.addWidget(self.results)
        note = QLabel(
            "密钥不要写进参数、URL 或普通配置。这里只保存环境变量名称，不保存密钥值。"
            "新增或修改主机环境变量后，请重启应用。暂不支持 OAuth、旧版 SSE、资源和提示词。"
            "连接在每次操作结束后关闭；超时不代表操作未生效，不会自动重试。"
        )
        note.setWordWrap(True)
        root.addWidget(note)
        root.addStretch(1)

        self.selector.currentIndexChanged.connect(self._select)
        self.new_button.clicked.connect(self.new_server)
        self.delete_button.clicked.connect(self.delete_server)
        self.save_button.clicked.connect(self.save)
        self.reload_button.clicked.connect(self.reload)
        self.test_button.clicked.connect(self.test_connection)
        for field in (self.name, self.command, self.cwd, self.url):
            field.textChanged.connect(self._mark_dirty)
        for text_field in (self.args, self.env_refs, self.header_refs):
            text_field.textChanged.connect(self._mark_dirty)
        self.enabled.toggled.connect(self._mark_dirty)
        self.transport.currentIndexChanged.connect(self._transport_changed)
        self.timeout.valueChanged.connect(self._mark_dirty)
        self.reload()

    def _mark_dirty(self) -> None:
        if not self._loading:
            self._dirty = True
            self.feedback.setText("有未保存的修改；保存后立即生效。")

    def _transport_changed(self) -> None:
        local = self.transport.currentData() == "stdio"
        for field in (self.command, self.args, self.cwd, self.env_refs):
            field.setEnabled(local)
        for field in (self.url, self.header_refs):
            field.setEnabled(not local)
        self._mark_dirty()

    def _fill(self, server: McpServer | None) -> None:
        self._loading = True
        self.server_id.setText(server.id if server else "mcp_" + uuid4().hex[:12])
        self.name.setText(server.name if server else "")
        self.enabled.setChecked(server.enabled if server else False)
        self.transport.setCurrentIndex(0 if server is None or server.transport == "stdio" else 1)
        self.command.setText(server.command if server else "")
        self.args.setPlainText(json.dumps(server.args if server else [], ensure_ascii=False))
        self.cwd.setText(server.cwd or "" if server else "")
        self.env_refs.setPlainText(
            json.dumps(server.env_refs if server else {}, ensure_ascii=False)
        )
        self.url.setText(server.url if server else "")
        self.header_refs.setPlainText(
            json.dumps(server.header_refs if server else {}, ensure_ascii=False)
        )
        self.timeout.setValue(server.timeout_seconds if server else 20)
        self._transport_changed()
        self._loading = False
        self._dirty = False
        self.delete_button.setEnabled(server is not None)
        self.results.clear()

    def reload(self, selected_id: str | None = None) -> None:
        try:
            self._servers = self._service.configuration().servers
        except McpError as exc:
            self.feedback.setText(str(exc))
            self.form_widget.setEnabled(False)
            self.save_button.setEnabled(False)
            self.test_button.setEnabled(False)
            self.delete_button.setEnabled(False)
            return
        self.form_widget.setEnabled(True)
        self.save_button.setEnabled(True)
        self.test_button.setEnabled(True)
        self._switching = True
        self.selector.clear()
        for server in self._servers:
            self.selector.addItem(
                f"{server.name} · {'已启用' if server.enabled else '已停用'}", server.id
            )
        index = next((i for i, s in enumerate(self._servers) if s.id == selected_id), 0)
        self._selected = index if self._servers else -1
        self.selector.setCurrentIndex(self._selected)
        self._switching = False
        self._fill(self._servers[self._selected] if self._selected >= 0 else None)
        self.feedback.setText("未连接任何服务器。可新增、修改或显式测试连接。")

    def _select(self, index: int) -> None:
        if self._switching:
            return
        if self._dirty:
            self._switching = True
            self.selector.setCurrentIndex(self._selected)
            self._switching = False
            self.feedback.setText("请先保存，或点击「放弃未保存修改」后切换服务器。")
            return
        self._selected = index
        self._fill(self._servers[index] if index >= 0 else None)

    def new_server(self) -> None:
        if self._dirty:
            self.feedback.setText("请先保存或放弃当前修改，再新增服务器。")
            return
        self._switching = True
        self.selector.setCurrentIndex(-1)
        self._switching = False
        self._selected = -1
        self._fill(None)
        self.name.setFocus()

    def _read_form(self) -> McpServer:
        local = self.transport.currentData() == "stdio"
        return McpServer(
            id=self.server_id.text(),
            name=self.name.text().strip(),
            enabled=self.enabled.isChecked(),
            transport=self.transport.currentData(),
            command=self.command.text().strip() if local else "",
            args=json.loads(self.args.toPlainText() or "[]") if local else [],
            cwd=(self.cwd.text().strip() or None) if local else None,
            env_refs=json.loads(self.env_refs.toPlainText() or "{}") if local else {},
            url=self.url.text().strip() if not local else "",
            header_refs=json.loads(self.header_refs.toPlainText() or "{}") if not local else {},
            timeout_seconds=self.timeout.value(),
        )

    def save(self) -> None:
        try:
            server = self._read_form()
            current = self._service.configuration()
            servers = [s for s in current.servers if s.id != server.id] + [server]
            self._service.save(McpConfiguration(servers=servers))
        except ValidationError as exc:
            errors = exc.errors(include_input=False, include_url=False, include_context=False)
            self.feedback.setText("配置未保存：" + "; ".join(e["msg"] for e in errors))
            return
        except (ValueError, McpError):
            self.feedback.setText(
                "配置未保存：请检查 JSON 数组/对象和服务器配置；不会显示输入内容。"
            )
            return
        except OSError:
            self.feedback.setText("MCP 配置写入失败；原配置保持不变。")
            return
        self.reload(server.id)
        self.feedback.setText("服务器已保存，立即生效；尚未建立连接。")

    def delete_server(self) -> None:
        if self._selected < 0:
            return
        server_id = self.server_id.text()

        def remove(accepted: bool) -> None:
            if not accepted:
                return
            try:
                config = self._service.configuration()
                self._service.save(
                    McpConfiguration(servers=[s for s in config.servers if s.id != server_id])
                )
                self.reload()
                self.feedback.setText("服务器已删除；不会再用于新的调用。")
            except (McpError, OSError):
                self.feedback.setText("删除失败，配置未更新。")

        self._panel = ask_confirm(
            self,
            "删除 MCP 服务器？",
            "此操作删除配置，不会卸载外部程序。",
            remove,
            confirm_text="删除",
            danger=True,
        )

    def test_connection(self) -> None:
        if self._dirty or self._selected < 0:
            self.feedback.setText("请先保存服务器，再测试连接。")
            return
        server_id = self.server_id.text()
        try:
            self._service.resolve(server_id)
        except McpError as exc:
            self.feedback.setText(str(exc))
            return
        self._panel = ask_confirm(
            self,
            "连接并发现 MCP 工具？",
            "这会运行已配置的本机程序或连接远程地址，仅发现工具，不调用业务工具。请确认信任该服务器。",
            lambda accepted: self._run_test(server_id) if accepted else None,
            confirm_text="连接并发现",
        )

    def _run_test(self, server_id: str) -> None:
        if self._testing:
            return
        self._testing = True
        self.setEnabled(False)
        self.feedback.setText("正在连接并读取工具目录…界面不会阻塞。")

        def success(result: dict[str, Any]) -> None:
            self._testing = False
            self.setEnabled(True)
            tools = result.get("tools", [])
            self.feedback.setText(f"本次连接成功，发现 {len(tools)} 个工具；连接现已关闭。")
            self.results.setPlainText("\n".join(str(t.get("name", "")) for t in tools))

        def failed(error: Exception) -> None:
            self._testing = False
            self.setEnabled(True)
            self.feedback.setText(
                str(error)
                if isinstance(error, McpError)
                else "MCP 测试失败；请检查服务器配置与环境变量。"
            )

        self._jobs.submit(lambda: self._service.execute(server_id), success, failed)

    def resolve_pending_changes(self) -> bool:
        if self._dirty:
            self.feedback.setText("MCP 配置尚未保存，请先保存或放弃修改。")
            return False
        return True
