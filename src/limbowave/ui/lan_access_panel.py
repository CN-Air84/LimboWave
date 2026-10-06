"""Desktop-only controls for the opt-in LAN listener; never store pairing secrets."""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from limbowave.ui import theme


def lan_addresses() -> list[tuple[str, str]]:
    """Offer explicit RFC1918 adapter addresses, never wildcard/public bindings."""
    private = tuple(
        ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    )
    result: list[tuple[str, str]] = []
    for adapter in QNetworkInterface.allInterfaces():
        if not adapter.flags() & QNetworkInterface.InterfaceFlag.IsUp:
            continue
        for entry in adapter.addressEntries():
            if entry.ip().protocol() != QAbstractSocket.NetworkLayerProtocol.IPv4Protocol:
                continue
            host = entry.ip().toString()
            if any(ipaddress.ip_address(host) in net for net in private):
                result.append((host, adapter.humanReadableName()))
    return sorted(set(result))


class LanAccessPanel(QWidget):
    start_requested = Signal(object)
    stop_requested = Signal()
    pairing_requested = Signal()
    revoke_requested = Signal(str)
    approve_requested = Signal(str)
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("lanAccessPanel")
        layout = QVBoxLayout(self)
        heading = QLabel("局域网访问")
        heading.setStyleSheet(f"font-size: {theme.FS_TITLE}px; font-weight: 600")
        layout.addWidget(heading)
        hint = QLabel(
            "电脑保持运行并解锁后，手机可在同一 Wi-Fi 下聊天。默认关闭；"
            "所有配对设备可查看本资料库全部会话。手机不能执行主机工具。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.host = QComboBox()
        for address, adapter in lan_addresses():
            self.host.addItem(f"{address} · {adapter}", address)
        self.host.addItem("127.0.0.1 · 仅本机开发 HTTP", "127.0.0.1")
        form.addRow("监听网卡", self.host)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(8765)
        form.addRow("端口", self.port)
        self.certificate = QLineEdit()
        self.private_key = QLineEdit()
        for title, field in (("HTTPS 证书", self.certificate), ("证书私钥", self.private_key)):
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.addWidget(field)
            browse = QPushButton("选择…")
            browse.clicked.connect(lambda _checked=False, edit=field: self._browse(edit))
            line.addWidget(browse)
            form.addRow(title, row)
        layout.addLayout(form)
        self.allow_http = QCheckBox("允许无证书 HTTP（我已了解明文聊天及主动篡改风险）")
        self.allow_http.setChecked(False)
        layout.addWidget(self.allow_http)
        tls = QLabel(
            "推荐 HTTPS：证书须覆盖所选 IP，手机须信任签发 CA。也可明确确认后使用 HTTP，"
            "但聊天和设备 Cookie 不受传输加密保护；密码信封不防主动攻击。"
            "手机必须配对并核验资料库密码。不会自动修改防火墙或开放公网端口。"
        )
        tls.setWordWrap(True)
        layout.addWidget(tls)
        actions = QHBoxLayout()
        self.start_button = QPushButton("开启局域网访问")
        self.stop_button = QPushButton("关闭并撤销全部设备")
        self.pair_button = QPushButton("生成两分钟配对邀请")
        self.start_button.clicked.connect(self._start)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self.pair_button.clicked.connect(self.pairing_requested.emit)
        for button in (self.start_button, self.stop_button, self.pair_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.address = QLabel()
        self.address.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.address)
        self.invitation = QLabel()
        self.invitation.setWordWrap(True)
        self.invitation.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.invitation)
        self.qr = QLabel()
        layout.addWidget(self.qr)
        self._device_snapshot: tuple[tuple[str, str, str], ...] = ()
        self.device_rows = QVBoxLayout()
        layout.addLayout(self.device_rows)
        refresh = QPushButton("刷新设备与待确认申请")
        refresh.clicked.connect(self.refresh_requested.emit)
        layout.addWidget(refresh)
        layout.addStretch(1)
        self.set_running(False)

    def _browse(self, field: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 PEM 文件", "", "PEM (*.pem *.crt *.key)")
        if path:
            field.setText(path)

    def _start(self) -> None:
        self.start_requested.emit(
            {
                "host": self.host.currentData(),
                "port": self.port.value(),
                "certfile": self.certificate.text().strip() or None,
                "keyfile": self.private_key.text().strip() or None,
                "allow_insecure_http": self.allow_http.isChecked(),
            }
        )

    def set_pending(self) -> None:
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.pair_button.setEnabled(False)
        self.status.setText("正在切换局域网服务…")

    def set_running(self, running: bool, address: str = "", error: str = "") -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.pair_button.setEnabled(running)
        for control in (self.host, self.port, self.certificate, self.private_key, self.allow_http):
            control.setEnabled(not running)
        self.status.setText(
            error
            or ("已开启 · 关闭服务不会中断电脑端生成" if running else "已关闭 · 当前没有局域网监听")
        )
        self.address.setText(address)
        if not running:
            self.clear_invitation()
            self.set_devices([], [])

    def clear_invitation(self) -> None:
        self.invitation.clear()
        self.qr.clear()

    def set_invitation(self, url: str, code: str) -> None:
        import qrcode
        from qrcode.image.pil import PilImage

        image = qrcode.make(url, image_factory=PilImage).get_image().convert("RGB")
        data = image.tobytes()
        qt_image = QImage(
            data, image.width, image.height, image.width * 3, QImage.Format.Format_RGB888
        ).copy()
        self.qr.setPixmap(
            QPixmap.fromImage(qt_image).scaled(
                220,
                220,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        )
        self.invitation.setText(
            f"请在两分钟内扫码。备用手输码：{code}\n手输码申请必须在电脑核对校验短语并确认。"
        )

    def set_devices(
        self, devices: Sequence[dict[str, Any]], pending: Sequence[dict[str, Any]]
    ) -> None:
        snapshot = tuple(
            (str(device.get("name", "已配对设备")), "撤销", str(device["device_id"]))
            for device in devices
        ) + tuple(
            (f"待确认：{request.get('name', '')} · 校验短语 {request.get('phrase', '')}",
             "核对一致，允许配对", str(request["request_id"]))
            for request in pending
        )
        if snapshot == self._device_snapshot:
            return
        self._device_snapshot = snapshot
        while self.device_rows.count():
            item = self.device_rows.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        for device in devices:
            self._device_row(
                str(device.get("name", "已配对设备")),
                "撤销",
                str(device["device_id"]),
                self.revoke_requested,
            )
        for request in pending:
            label = f"待确认：{request.get('name', '')} · 校验短语 {request.get('phrase', '')}"
            self._device_row(
                label, "核对一致，允许配对", str(request["request_id"]), self.approve_requested
            )

    def _device_row(self, text: str, action: str, identifier: str, signal: Any) -> None:
        row = QWidget()
        layout = QHBoxLayout(row)
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        layout.addWidget(label, 1)
        button = QPushButton(action)
        button.clicked.connect(lambda: signal.emit(identifier))
        layout.addWidget(button)
        self.device_rows.addWidget(row)
