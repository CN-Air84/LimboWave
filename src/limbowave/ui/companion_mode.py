"""Adapter between an existing ChatView and the optional companion window.

Private ChatView presentation details are intentionally contained in this adapter;
there is no second controller, message store, draft store, or execution path.
"""
from __future__ import annotations

from collections.abc import Iterator
from itertools import chain
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QMimeData, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QScreen
from PySide6.QtWidgets import QApplication, QFileDialog

from limbowave.domain.files import ATTACHMENT_FILE_FILTER
from limbowave.ui.chat_view import ChatView, _BubbleRow
from limbowave.ui.companion_hotkey import CompanionHotkey
from limbowave.ui.companion_window import CompanionWindow
from limbowave.ui.floating import FloatingPanel

if TYPE_CHECKING:
    from limbowave.ui.main_window import MainWindow


class CompanionMode(QObject):
    def __init__(self, main: MainWindow) -> None:
        super().__init__(main)
        self._main = main
        self._window: CompanionWindow | None = None
        self._active = False
        self._shutdown = False
        self._source: ChatView | None = None
        self._shortcut = "Ctrl+Alt+Space"
        self._global_enabled = True
        self._hotkey = CompanionHotkey(self.summon)
        self._shortcut_notice = ""
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self.refresh)
        self._action = QAction("迷你陪伴模式", main)
        self._action.setShortcut("Ctrl+Shift+M")
        self._action.setAutoRepeat(False)
        self._action.triggered.connect(self.toggle)
        main.addAction(self._action)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            # qasync's bootstrap run_until_complete() also emits aboutToQuit.
            # Release native registrations, but only an accepted MainWindow
            # close may permanently disable this controller via shutdown().
            app.aboutToQuit.connect(self._on_event_loop_stopped)
            if isinstance(app, QApplication):
                app.screenRemoved.connect(self._screens_changed)
                app.screenAdded.connect(self._screen_added)
            for screen in QApplication.screens():
                self._screen_added(screen)
        main._chat_stack.currentChanged.connect(self.rebind)
        main.destroyed.connect(self._hotkey.close)

    @property
    def window(self) -> CompanionWindow | None:
        return self._window

    @property
    def active(self) -> bool:
        return self._active

    def _current_chat(self) -> ChatView:
        current = self._main._chat_stack.currentWidget()
        return current if isinstance(current, ChatView) else self._main.chat

    def toggle(self) -> None:
        if self.active:
            self.restore()
        else:
            self.open()

    def summon(self) -> None:
        if (self.active and self._window is not None and self._window.isActiveWindow()
                and not self._window.collapsed):
            self._window.set_collapsed(True)
        else:
            self.open()

    def open(self) -> None:
        if (self._shutdown or self._main.showing_full_page
                or QApplication.activeModalWidget() is not None
                or QApplication.activePopupWidget() is not None):
            return
        # Do not bury a permission/confirmation card already awaiting a decision.
        if any(panel.isVisible() for panel in self._main.findChildren(FloatingPanel)):
            return
        if self._window is None:
            self._window = CompanionWindow(self._main)
            self._window.restore_requested.connect(self.restore)
            self._window.submit_requested.connect(self.submit)
            self._window.stop_requested.connect(self.stop)
            self._window.attach_requested.connect(self.choose_files)
            self._window.files_dropped.connect(self.attach_files)
            self._window._input.attachment_pasted.connect(self.paste_attachment)
            self._window._attachments.attachment_removed.connect(self.remove_attachment)
            self._window.shortcut_requested.connect(self.configure_shortcut)
            self._window.global_shortcut_toggled.connect(self.set_global_shortcut_enabled)
            local_action = QAction("返回完整界面", self._window)
            local_action.setShortcut("Ctrl+Shift+M")
            local_action.setAutoRepeat(False)
            local_action.triggered.connect(self.restore)
            self._window.addAction(local_action)
            self._window._input.textChanged.connect(self.refresh)
            origin = self._main.frameGeometry().topRight()
            self._window.move(origin - QPoint(self._window.width(), -36))
        self._active = True
        self.rebind()
        self.configure_shortcut(self._shortcut)
        self._main.hide()
        self._window.set_collapsed(False)
        self._window.showNormal()
        self._window.ensure_on_screen()
        self._window.raise_()
        self._window.activateWindow()
        self._window._input.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._timer.start()
        self.refresh()

    def rebind(self) -> None:
        if self._window is None or not self.active:
            return
        self._source = self._current_chat()
        self._window.bind_document(self._source._input.document())
        self.refresh()

    def restore(self, *, activate: bool = True) -> None:
        if not self.active:
            return
        self._active = False
        self._timer.stop()
        # Show before hiding the last visible window: never trigger implicit quit.
        self._main.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not activate)
        if self._main.isMinimized():
            self._main.setWindowState(self._main.windowState() & ~Qt.WindowState.WindowMinimized)
        self._main.show()
        self._main.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        if self._window is not None:
            self._window.hide()
            self._window.clear_projection()
        self._source = None
        if activate:
            self._main.raise_()
            self._main.activateWindow()
            self._current_chat()._input.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def suspend(self) -> None:
        """Full-page/lock navigation must never leave a sensitive mini view open."""
        self.restore(activate=False)
        self._timer.stop()
        self._hotkey.close()
        self._source = None
        if self._window is not None:
            self._window.hide()
            self._window.clear_projection()

    def _on_event_loop_stopped(self) -> None:
        # Keep the signal receiver QObject-owned so destruction disconnects it.
        self._hotkey.close()

    def shutdown(self) -> None:
        self._shutdown = True
        self._active = False
        self._timer.stop()
        self._hotkey.close()
        if self._window is not None:
            self._window.hide()
            self._window.clear_projection()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def submit(self) -> None:
        if self.active:
            self._current_chat()._on_send()
            self.refresh()

    def _can_attach(self) -> bool:
        chat = self._current_chat()
        return (self.active and chat is self._main.chat
                and chat._input.attachments_enabled and chat._attach_btn.isEnabled())

    def choose_files(self) -> None:
        if not self._can_attach() or self._window is None:
            return
        source = self._current_chat()
        generation = source.attachments.generation
        paths, _ = QFileDialog.getOpenFileNames(
            self._window, "添加附件", "", ATTACHMENT_FILE_FILTER)
        # Native dialogs run their own event loop. Do not import a selection
        # into a replacement conversation/draft that became active meanwhile.
        if (source is self._current_chat() and self._can_attach()
                and source.attachments.generation == generation):
            self.attach_files(paths)

    def attach_files(self, paths: list[str]) -> None:
        if paths and self._can_attach():
            self._current_chat().files_dropped.emit(paths)
            self.refresh()

    def paste_attachment(self, mime: QMimeData) -> None:
        if self._can_attach():
            # Reuse original encoding, generation checks and pending-paste guard.
            # Async callbacks remain owned by that source even if views switch.
            self._current_chat()._input.insertFromMimeData(mime)
            self.refresh()

    def remove_attachment(self, attachment_id: str) -> None:
        if self.active:
            self._current_chat().attachments._on_remove(attachment_id)
            self.refresh()

    def stop(self) -> None:
        if self.active:
            # History preview has no run wiring; the live ChatView owns generation.
            chat = self._main.chat
            if chat._busy or chat.compressing:
                chat.stop_requested.emit()
            self.refresh()

    def set_global_shortcut_enabled(self, enabled: bool) -> None:
        self._global_enabled = enabled
        if self._window is not None:
            self._window._shortcut_action.setEnabled(enabled)
        if not enabled:
            self._hotkey.close()
        self.configure_shortcut(self._shortcut)

    def configure_shortcut(self, sequence: str) -> None:
        if not self._global_enabled:
            self._shortcut_notice = ""
            if self._window is not None:
                self._window.set_shortcut_status(
                    self._shortcut, "全局呼出已关闭；应用内 Ctrl+Shift+M 仍可用。")
            self.refresh()
            return
        if any(action.shortcut() == QKeySequence(sequence)
               for action in self._main.findChildren(QAction)):
            success, status = False, "该快捷键已用于应用内操作，请选择其他组合。"
        else:
            success, status = self._hotkey.register(sequence)
        if success:
            self._shortcut = sequence
        elif self._hotkey._id is not None:
            status += f" 原快捷键 {self._shortcut} 仍可用。"
        self._shortcut_notice = "" if success else status
        if self._window is not None:
            self._window.set_shortcut_status(self._shortcut, status)
        self.refresh()

    def refresh(self) -> None:
        mini = self._window
        if not self.active or mini is None or mini.isMinimized():
            return
        chat = self._current_chat()
        if self._source is not chat:
            self.rebind()
            return
        mini.restyle()
        messages = tuple((row._role, self._row_preview(row))
                         for row in chat._rows[-3:])
        mini.show_messages(messages)
        status = "正在加载会话…" if chat.history_loading else chat.status_text
        if self._main.chat._busy and status == "就绪":
            status = "正在回应…"
        mini._status.setText(status)
        mini._status.setVisible(bool(status) and not status.startswith("就绪"))
        mini._collapse.setToolTip(("展开" if mini.collapsed else "收起") + " · " + status)
        notices = []
        if chat._editing_id is not None:
            notices.append("正在编辑旧消息；发送会创建分支，可返回主界面取消。")
        attachments = chat.attachments.snapshot()
        mini.show_attachments(attachments)
        mini.set_attachments_enabled(self._can_attach())
        names = [item.title for item in attachments]
        if chat._pending_pastes:
            notices.append("正在处理粘贴的图片…")
        if names:
            notices.append(f"将一并发送 {len(names)} 个附件")
        if not chat._queue_panel.isHidden():
            notices.append(chat._queue_panel._summary.text() + "；主界面可管理队列。")
        if chat._draft_only:
            notices.append(chat._draft_notice.text())
        if self._shortcut_notice:
            notices.append(self._shortcut_notice)
        mini._notice.setText("\n".join(notices))
        mini._notice.setVisible(bool(notices))
        mini._input.setEnabled(chat._input.isEnabled() and chat._composer.isEnabled())
        mini._send.setText("排队" if chat._queue_mode else "发送")
        mini._send.setAccessibleName("加入发送队列" if chat._queue_mode else "发送")
        mini._send.setEnabled(chat._can_queue() if chat._queue_mode else chat._can_send())
        mini._send.setToolTip(chat._send_btn.toolTip())
        running = self._main.chat._busy or self._main.chat.compressing
        mini._stop.setEnabled(running)
        mini._stop.setVisible(running)

    @staticmethod
    def _row_preview(row: _BubbleRow) -> str:
        # Hidden ChatViews pause their stream renderer. Read pending fragments
        # without flushing/consuming them or rendering the full hidden transcript.
        parts: Iterator[str]
        if row._role == "assistant":
            segments = (prefix + segment.raw_text[:1201] for prefix, segment in
                        (("\n\n" if i else "", segment)
                         for i, segment in enumerate(row._segments)))
            pending = row._stream_buffer._pending if row._stream_buffer is not None else ()
            parts = chain(segments, pending)
        else:
            parts = iter((row.content_text(),))
        text = ""
        for part in parts:
            text += part[:1201 - len(text)]
            if len(text) > 1200:
                text = text[:1200] + "\n…（返回主界面查看全文）"
                break
        if row._run_error:
            text += "\n⚠ " + row._run_error[:400]
        elif not text and row._run_state_text:
            text = row._run_state_text[:400]
        return text

    def _screen_added(self, screen: QScreen) -> None:
        screen.availableGeometryChanged.connect(self._screens_changed)
        self._screens_changed()

    def _screens_changed(self, *_args: object) -> None:
        if self._window is not None and self.active:
            self._window.ensure_on_screen()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (self.active and self._source is not None
                and event.type() == QEvent.Type.EnabledChange
                and watched in (self._source._input, self._source._composer,
                                self._source._attach_btn, self._source._send_btn)):
            self.refresh()
        if (self.active and isinstance(watched, FloatingPanel)
                and event.type() in (QEvent.Type.Show, QEvent.Type.ShowToParent)
                and self._main.isAncestorOf(watched)):
            # A permission or confirmation cannot live behind a hidden main window.
            # Showing without activation keeps background requests from stealing focus.
            self.restore(activate=False)
        return super().eventFilter(watched, event)
