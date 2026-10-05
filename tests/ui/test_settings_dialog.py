"""站点与模型管理对话框的 Qt 验收。

锁住的交互语义：
- 保存端点/模型真的经服务层落盘（重开对话框能看到）；
- 校验失败弹错误框且配置不变（不写半截配置）；
- 删除仍被绑定的端点被拒绝并提示；
- 导入的实际模型**不会**变成逻辑模型；逻辑模型由用户建立，保存时按 ID 自动匹配；
- 手动配对跳过已绑定到别的逻辑模型的实际模型（一个实际模型只绑一个逻辑模型）；
- 路由预览展示确定性路由结果，不发网络请求。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QFormLayout, QLineEdit, QPushButton
from pytestqt.qtbot import QtBot

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.models import LogicalModel
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.infrastructure.configuration.json_config_repository import (
    JsonConfigRepository,
)
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.ui.settings_dialog import SettingsDialog


@pytest.fixture(autouse=True)
def _no_modal_messageboxes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """把悬浮窗提示打桩成非阻塞记录器——模态弹窗在无人点击时会挂死测试。

    settings_dialog 的提示已从 QMessageBox 换成 ``ask_alert``（内建悬浮窗），
    打桩对象随之改变。返回记录列表，测试可断言「确实弹了错误框」。
    """
    shown: list[str] = []
    monkeypatch.setattr(
        "limbowave.ui.floating.ask_alert",
        lambda _parent, _title, detail: shown.append(str(detail)),
    )
    return shown


@pytest.fixture
def services(tmp_path: Path, vault_key: VaultKey):
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    return settings, credentials


def _endpoint_config(endpoint_id: str = "relay-a", name: str = "A") -> EndpointConfig:
    return EndpointConfig(
        id=endpoint_id,
        name=name,
        base_url=f"https://{endpoint_id}.example.com/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
    )


def _import(settings: SettingsService, endpoint_id: str, model_id: str, **caps: object) -> None:
    """模拟「实际模型」页导入并检测了一个远端模型。"""
    fields: dict[str, object] = {
        "default_thinking_level": None,
        "thinking_level_locked": False,
        "supports_thinking": None,
        "supports_tools": False,
    }
    fields.update(caps)
    settings.record_probed_model(
        endpoint_id=endpoint_id, model_id=model_id, display_name=model_id,
        **fields,  # type: ignore[arg-type]
    )


def test_binding_thinking_controls_share_compact_row(qtbot: QtBot, services) -> None:
    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    dialog.show()

    tab = dialog._models_tab
    tab._subtabs.setCurrentIndex(1)
    qtbot.waitUntil(lambda: tab._binding_page.isVisible())
    combo = tab._bind_thinking
    button = next(
        button for button in tab.findChildren(QPushButton)
        if button.text() == "保存默认思考强度"
    )
    combo_pos = combo.mapTo(tab, QPoint(0, 0))
    button_pos = button.mapTo(tab, QPoint(0, 0))

    assert abs(combo_pos.y() - button_pos.y()) <= 2
    assert button_pos.x() >= combo_pos.x() + combo.width()
    assert combo.width() < 220
    assert button.width() < 220


def test_add_endpoint_persists(qtbot: QtBot, services) -> None:
    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)

    tab = dialog._endpoints_tab
    tab._on_new()
    tab._name.setText("中转站 A")
    tab._base_url.setText("https://relay-a.example.com/v1")
    assert "zhong-zhuan-zhan-a" in tab._identity_hint.text()
    tab._on_save()

    config = settings.load()
    assert [e.id for e in config.endpoints] == ["zhong-zhuan-zhan-a"]
    assert config.endpoints[0].name == "中转站 A"
    assert config.endpoints[0].credential_ref is None  # 没设密钥 → 不带引用

    # 再次保存是更新，不派生 -2
    tab._name.setText("中转站 A（改名）")
    tab._on_save()
    config = settings.load()
    assert [e.id for e in config.endpoints] == ["zhong-zhuan-zhan-a"]
    assert config.endpoints[0].name == "中转站 A（改名）"


def test_endpoint_credential_ref_follows_derived_id(qtbot: QtBot, services) -> None:
    settings, credentials = services
    credentials.store_secret("relay", "sk-test")
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)

    tab = dialog._endpoints_tab
    tab._on_new()
    tab._name.setText("Relay")
    tab._base_url.setText("https://relay.example.com/v1")
    tab._on_save()

    endpoint = settings.load().endpoints[0]
    assert endpoint.id == "relay"
    assert endpoint.credential_ref == "relay"

    # 同名新端点自动避让 id
    tab._on_new()
    tab._name.setText("Relay")
    tab._base_url.setText("https://relay-2.example.com/v1")
    tab._on_save()
    assert [e.id for e in settings.load().endpoints] == ["relay", "relay-2"]


def test_endpoint_secret_input_is_below_base_url(qtbot: QtBot, services) -> None:
    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._endpoints_tab
    form = tab._base_url.parentWidget().layout()
    assert isinstance(form, QFormLayout)
    url_row, url_role = form.getWidgetPosition(tab._base_url)
    secret_row, secret_role = form.getWidgetPosition(tab._secret)
    assert secret_row == url_row + 1
    assert secret_role == url_role == QFormLayout.ItemRole.FieldRole
    assert form.labelForField(tab._secret).text() == "密钥"
    assert tab._secret.echoMode() == QLineEdit.EchoMode.Password
    assert not any("设置密钥" in button.text() for button in tab.findChildren(QPushButton))


def test_inline_secret_is_encrypted_on_save_then_discovers(
    qtbot: QtBot, services, tmp_path: Path
) -> None:
    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._endpoints_tab
    tab._name.setText("Relay")
    tab._base_url.setText("https://relay.example.com/v1")
    tab._secret.setText("  sk-inline-test  ")
    assert credentials.list_refs() == []

    with qtbot.waitSignal(tab.discovery_requested, timeout=1500) as discovery:
        tab._on_save()

    endpoint = settings.load().endpoints[0]
    assert endpoint.credential_ref == endpoint.id == "relay"
    assert credentials.resolve("relay") == "sk-inline-test"
    assert discovery.args == [endpoint]
    assert tab._secret.text() == ""
    assert "已设置密钥" in tab._secret.placeholderText()
    assert "sk-inline-test" not in (tmp_path / "config.json").read_text(encoding="utf-8")
    assert "sk-inline-test" not in (tmp_path / "vault" / "secrets.json").read_text(
        encoding="utf-8"
    )

    dialog.reload()
    assert tab._secret.text() == ""  # 重载也不回填已保存的密钥


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_inline_secret_keeps_existing_secret(qtbot: QtBot, services, value: str) -> None:
    settings, credentials = services
    endpoint = _endpoint_config().model_copy(update={"credential_ref": "legacy-ref"})
    settings.upsert_endpoint(endpoint)
    credentials.store_secret("legacy-ref", "sk-original")
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._endpoints_tab
    tab._list.setCurrentRow(0)
    assert tab._secret.text() == ""
    assert "已设置密钥" in tab._secret.placeholderText()
    tab._secret.setText(value)
    tab._name.setText("Renamed")
    tab._on_save()

    assert credentials.resolve("legacy-ref") == "sk-original"
    assert settings.load().endpoints[0].credential_ref == "legacy-ref"
    assert settings.load().endpoints[0].name == "Renamed"


@pytest.mark.parametrize("existing_ref", [None, "legacy-ref"])
def test_inline_secret_updates_existing_endpoint(
    qtbot: QtBot, services, existing_ref: str | None
) -> None:
    settings, credentials = services
    endpoint = _endpoint_config().model_copy(update={"credential_ref": existing_ref})
    settings.upsert_endpoint(endpoint)
    if existing_ref:
        credentials.store_secret(existing_ref, "sk-original")
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._endpoints_tab
    tab._list.setCurrentRow(0)
    tab._secret.setText("sk-replacement")
    tab._on_save()

    ref = existing_ref or endpoint.id
    assert len(settings.load().endpoints) == 1
    assert settings.load().endpoints[0].credential_ref == ref
    assert credentials.resolve(ref) == "sk-replacement"
    assert credentials.list_refs() == [ref]
    assert tab._secret.text() == ""


@pytest.mark.parametrize("new_endpoint", [False, True])
def test_switching_endpoint_clears_unsaved_secret(
    qtbot: QtBot, services, new_endpoint: bool
) -> None:
    settings, credentials = services
    settings.upsert_endpoint(_endpoint_config())
    settings.upsert_endpoint(_endpoint_config("relay-b", "B"))
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._endpoints_tab
    tab._list.setCurrentRow(0)
    tab._secret.setText("sk-unsaved")
    if new_endpoint:
        tab._on_new()
        tab._name.setText("C")
        tab._base_url.setText("https://c.example.com/v1")
    else:
        tab._list.setCurrentRow(1)
    assert tab._secret.text() == ""
    tab._on_save()
    assert credentials.list_refs() == []
    assert all(endpoint.credential_ref is None for endpoint in settings.load().endpoints)


def test_invalid_endpoint_does_not_save_inline_secret(
    qtbot: QtBot, services, _no_modal_messageboxes
) -> None:
    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._endpoints_tab
    tab._name.setText("Relay")
    tab._base_url.setText("not-a-url")
    tab._secret.setText("sk-unsaved")
    tab._on_save()

    assert _no_modal_messageboxes
    assert settings.load().endpoints == []
    assert credentials.list_refs() == []
    assert tab._secret.text() == "sk-unsaved"  # 校验失败允许修改表单后重试


def test_inline_secret_storage_error_is_sanitized(
    qtbot: QtBot, services, _no_modal_messageboxes, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._endpoints_tab
    tab._name.setText("Relay")
    tab._base_url.setText("https://relay.example.com/v1")
    tab._secret.setText("sk-sensitive")

    def fail_store(_ref: str, secret: str) -> None:
        raise OSError(f"cannot store {secret}")

    monkeypatch.setattr(credentials, "store_secret", fail_store)
    tab._on_save()
    assert _no_modal_messageboxes == ["密钥保存失败，请重试。"]
    assert settings.load().endpoints == []
    assert tab._secret.text() == "sk-sensitive"


def test_unsaved_secret_does_not_discover_with_old_credentials(qtbot: QtBot, services) -> None:
    settings, credentials = services
    credentials.store_secret("relay", "sk-old")
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._endpoints_tab
    tab._name.setText("Relay")
    tab._base_url.setText("https://relay.example.com/v1")
    tab._secret.setText("sk-new")
    with qtbot.assertNotEmitted(tab.discovery_requested):
        tab._request_discovery_if_ready()
    assert credentials.resolve("relay") == "sk-old"


def test_advanced_panel_edits_param_rules(qtbot: QtBot, services) -> None:
    from PySide6.QtWidgets import QLineEdit

    from limbowave.ui.floating import FloatingPanel

    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    dialog.show()

    tab = dialog._endpoints_tab
    tab._on_new()
    tab._name.setText("A")
    tab._base_url.setText("https://a.example.com/v1")
    tab._advanced_btn.click()
    panel = tab.findChild(FloatingPanel)
    assert panel is not None
    _whitelist, strip = panel.findChildren(QLineEdit)
    ok = next(b for b in panel.findChildren(QPushButton) if b.text() == "确定")

    strip.setText("model")  # 关键参数不许删 → 面板内报错，草稿不变
    ok.click()
    assert tab._strip_params == ()
    strip.setText("temperature, top_p")
    ok.click()
    assert tab._strip_params == ("temperature", "top_p")
    assert tab._advanced_btn.text() == "高级（2）…"

    tab._on_save()
    assert settings.load().endpoints[0].strip_params == ("temperature", "top_p")


def test_invalid_endpoint_shows_error_and_keeps_config(
    qtbot: QtBot, services, _no_modal_messageboxes: list[str]
) -> None:
    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)

    tab = dialog._endpoints_tab
    tab._on_new()
    tab._name.setText("bad")
    tab._base_url.setText("not-a-url")
    tab._on_save()

    assert _no_modal_messageboxes, "校验失败应弹错误框"
    assert settings.load().endpoints == []  # 配置未被污染


def test_delete_bound_endpoint_refused(qtbot: QtBot, services) -> None:
    settings, credentials = services
    settings.upsert_endpoint(_endpoint_config())
    _import(settings, "relay-a", "deepseek-chat")
    settings.create_model(LogicalModel(id="deepseek-chat", name="DeepSeek"))
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)

    etab = dialog._endpoints_tab
    etab._list.setCurrentRow(0)
    etab._on_delete()  # 弹错误框，配置不变
    assert [e.id for e in settings.load().endpoints] == ["relay-a"]


def test_imported_actual_models_do_not_appear_as_logical_models(
    qtbot: QtBot, services
) -> None:
    settings, credentials = services
    settings.upsert_endpoint(_endpoint_config())
    for model_id in ("gpt-5", "grok-4.7", "z-ai/glm-5.3"):
        _import(settings, "relay-a", model_id)
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab
    assert tab._list.count() == 0
    # 但它们可以被引用：出现在 ID 补全候选里
    assert tab._id_candidates.stringList() == ["gpt-5", "grok-4.7", "z-ai/glm-5.3"]


def test_reference_candidates_exclude_bound_actual_model_ids(
    qtbot: QtBot, services
) -> None:
    settings, credentials = services
    settings.upsert_endpoint(_endpoint_config())
    for model_id in ("gpt-5", "grok-4.7", "z-ai/glm-5.3"):
        _import(settings, "relay-a", model_id)
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab

    tab._on_new()
    tab._id.setText("gpt-5")
    tab._on_save()  # gpt-5 落盘并自动绑定了 relay-a 上的同名实际模型

    assert tab._current_model() is not None
    assert tab._id_candidates.stringList() == ["grok-4.7", "z-ai/glm-5.3"]

    # 候选被清空时点「引用实际模型」给提示，而不是弹空的补全框
    settings.create_model(LogicalModel(id="grok-4.7", name="Grok"))
    settings.create_model(LogicalModel(id="z-ai/glm-5.3", name="GLM"))
    tab.reload()
    assert tab._id_candidates.stringList() == []
    tab._on_new()
    tab._on_reference()
    assert "都被现有逻辑模型绑定" in tab._match_hint.text()


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("my-chat-model", "My Chat Model"),
        ("chat-5.3-mini", "Chat 5.3 Mini"),
        ("  MY-chat-model  ", "My Chat Model"),
        ("model", "Model"),
        ("glm-5.3", "GLM 5.3"),
        ("gpt-5-mini", "GPT 5 Mini"),
        ("z-ai/glm-5.3", "Z Ai/GLM 5.3"),
    ],
)
def test_new_model_name_follows_typed_id(
    qtbot: QtBot, services, model_id: str, expected: str
) -> None:
    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab
    tab._on_new()

    qtbot.keyClicks(tab._id, model_id)
    assert tab._name.text() == expected
    tab._on_save()
    saved = settings.load().models[0]
    assert saved.id == model_id.strip()
    assert saved.name == expected


def test_auto_model_name_updates_and_clears_with_id(qtbot: QtBot, services) -> None:
    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab

    tab._id.setText("first-model")
    assert tab._name.text() == "First Model"
    tab._id.setText("second-model")
    assert tab._name.text() == "Second Model"
    tab._id.clear()
    assert tab._name.text() == ""


def test_auto_model_name_preserves_manual_name_and_resets_on_new(qtbot: QtBot, services) -> None:
    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab

    tab._id.setText("first-model")
    tab._name.setText("自定义名称")
    tab._id.setText("second-model")
    assert tab._name.text() == "自定义名称"
    tab._on_save()
    assert settings.load().models[0].name == "自定义名称"
    tab.reload()
    assert tab._name.text() == "自定义名称"

    tab._on_new()
    assert tab._name.text() == ""
    tab._id.setText("third-model")
    assert tab._name.text() == "Third Model"
    tab._select_model("second-model")
    assert tab._name.text() == "自定义名称"


def test_new_model_previews_and_applies_auto_match(qtbot: QtBot, services) -> None:
    settings, credentials = services
    settings.upsert_endpoint(_endpoint_config("relay-a", "A"))
    settings.upsert_endpoint(_endpoint_config("relay-b", "B"))
    _import(settings, "relay-a", "deepseek-chat", supports_tools=True)
    _import(settings, "relay-b", "DeepSeek-Chat-free")
    _import(settings, "relay-b", "glm-4.6")
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab
    tab._on_new()
    tab._id.setText("deepseek-chat")
    assert "保存后将自动绑定 2 个" in tab._match_hint.text()
    assert settings.load().models == []  # 只是预告，没落盘

    tab._on_save()
    saved = settings.load().models[0]
    assert saved.id == "deepseek-chat"
    assert saved.name == "Deepseek Chat"  # 引用实际模型时也按逻辑 ID 生成显示名
    assert [(b.endpoint_id, b.model_id) for b in saved.bindings] == [
        ("relay-a", "deepseek-chat"),
        ("relay-b", "DeepSeek-Chat-free"),
    ]
    assert "已自动绑定 2 个" in tab._match_hint.text()
    assert tab._current_model() is not None
    assert tab._bindings.count() == 2
    assert "自动匹配" in tab._bindings.item(0).text()


def test_manual_pair_skips_models_owned_by_other_logical_model(
    qtbot: QtBot, services
) -> None:
    settings, credentials = services
    settings.upsert_endpoint(_endpoint_config())
    _import(settings, "relay-a", "claude-latest")
    _import(settings, "relay-a", "gpt-5")
    settings.create_model(LogicalModel(id="gpt-5", name="GPT-5"))  # 自动绑定 gpt-5
    settings.create_model(LogicalModel(id="claude-opus", name="Claude Opus"))
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab
    tab._select_model("claude-opus")
    combo = tab._pair_combo
    texts = [combo.itemText(i) for i in range(combo.count())]
    assert any("gpt-5（已绑定：gpt-5）" in text for text in texts)
    assert tab._pair_choice() == ("relay-a", "claude-latest")  # 默认落在可选项上

    tab._on_pair()
    model = next(m for m in settings.load().models if m.id == "claude-opus")
    assert [(b.model_id, b.auto_matched) for b in model.bindings] == [("claude-latest", False)]

    # 置灰的项选不中：一个实际模型只能绑定一个逻辑模型
    texts = [combo.itemText(i) for i in range(combo.count())]
    owned = next(i for i, text in enumerate(texts) if "已绑定" in text)
    combo.setCurrentIndex(owned)
    assert tab._pair_choice() is None


def test_unbind_leaves_logical_model_with_honest_preview(qtbot: QtBot, services) -> None:
    settings, credentials = services
    settings.upsert_endpoint(_endpoint_config("relay-a", "中转站 A"))
    _import(settings, "relay-a", "deepseek-chat")
    settings.create_model(LogicalModel(id="deepseek-chat", name="DeepSeek"))
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab
    tab._list.setCurrentRow(0)
    assert "中转站 A" in tab._preview.text()
    assert "默认绑定" in tab._preview.text()

    tab._bindings.setCurrentRow(0)
    tab._on_unbind()
    assert settings.load().models[0].bindings == []
    assert "还没有绑定实际模型" in tab._preview.text()
    assert tab._list.item(0).text() == "DeepSeek"
    assert "未绑定" in tab._list.item(0).toolTip()


def test_probe_from_logical_tab_emits_task_and_reports_result(qtbot: QtBot, services) -> None:
    from limbowave.application.services.model_probe import (
        ModelProbeResult,
        StreamProbeResult,
        ThinkingProbeResult,
        ToolProbeResult,
    )

    settings, credentials = services
    settings.upsert_endpoint(_endpoint_config())
    _import(settings, "relay-a", "m")
    settings.create_model(LogicalModel(id="m", name="M"))
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab
    tab._list.setCurrentRow(0)
    tab._bindings.setCurrentRow(0)
    with qtbot.waitSignal(tab.probe_requested, timeout=1000) as blocker:
        tab._on_probe()
    task = blocker.args[0]
    assert (task.endpoint.id, task.model_id) == ("relay-a", "m")
    assert "正在检测" in tab._probe_status.text()

    # 应用层把结果写进实际模型目录后通知本页
    _import(settings, "relay-a", "m", supports_tools=True)
    tab.apply_probe_result(
        "relay-a",
        ModelProbeResult(
            "m", StreamProbeResult(True, "ok"),
            ThinkingProbeResult(False, None, "no", inconclusive=True),
            ToolProbeResult(True, True, "ok"),
        ),
    )
    assert "检测完成" in tab._probe_status.text()
    assert "工具" in tab._bindings.item(0).text()


def test_default_thinking_level_saves_to_actual_model(qtbot: QtBot, services) -> None:
    settings, credentials = services
    settings.upsert_endpoint(_endpoint_config())
    _import(settings, "relay-a", "m", default_thinking_level="high")
    settings.create_model(LogicalModel(id="m", name="M"))
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab
    tab._list.setCurrentRow(0)
    tab._bindings.setCurrentRow(0)
    assert tab._bind_thinking.currentData() == "high"
    tab._bind_thinking.setCurrentIndex(tab._bind_thinking.findData("low"))
    tab._on_apply_binding_caps()
    actual = settings.load().actual_model("relay-a", "m")
    assert actual is not None
    assert actual.default_thinking_level == "low"


def test_editing_existing_model_keeps_bindings(qtbot: QtBot, services) -> None:
    settings, credentials = services
    settings.upsert_endpoint(_endpoint_config())
    _import(settings, "relay-a", "first")
    settings.create_model(LogicalModel(id="first", name="First"))
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab
    tab._list.setCurrentRow(0)
    assert tab._id.isReadOnly()
    tab._name.setText("改名")
    tab._context_window.setValue(64000)
    tab._on_save()
    model = settings.load().models[0]
    assert (model.name, model.context_window) == ("改名", 64000)
    assert [b.model_id for b in model.bindings] == ["first"]

    tab._on_new()
    assert tab._current_model() is None
    tab._id.setText("second")
    tab._on_save()
    assert {m.id for m in settings.load().models} == {"first", "second"}


def test_multi_select_delete_asks_then_removes(
    qtbot: QtBot, services, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, credentials = services
    settings.upsert_endpoint(_endpoint_config())
    for model_id in ("keep", "a", "b"):
        settings.create_model(LogicalModel(id=model_id, name=model_id))
    monkeypatch.setattr(
        "limbowave.ui.floating.ask_confirm",
        lambda _parent, _title, _detail, on_answer, **_kw: on_answer(True),
    )
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab
    # 显示名已自动排序，按稳定 ID 选择目标，不再依赖配置插入行号。
    for row in range(tab._list.count()):
        item = tab._list.item(row)
        item.setSelected(item.data(Qt.ItemDataRole.UserRole) in {"a", "b"})
    tab._on_delete()
    assert [m.id for m in settings.load().models] == ["keep"]


def test_numeric_fields_keep_compact_width_and_arrow_margin(qtbot: QtBot, services) -> None:
    from PySide6.QtWidgets import QLineEdit

    from limbowave.ui import theme

    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    dialog.setStyleSheet(theme.app_stylesheet())
    dialog.resize(1100, 680)
    dialog.show()
    spin = dialog._models_tab._context_window
    spin.setFocus()
    qtbot.wait(30)
    edit = spin.findChild(QLineEdit)
    assert edit is not None
    assert spin.width() <= 260
    assert edit.geometry().left() >= 10
    assert spin.width() - edit.geometry().right() >= 26
    assert spin.height() >= edit.height() + 12


@pytest.mark.parametrize(
    ("field", "label", "expected"),
    [
        ("context_window", "256K", 256_000),
        ("context_window", "262K", 262_144),
        ("context_window", "1M", 1_000_000),
        ("max_tokens", "128000", 128_000),
        ("max_tokens", "65536", 65_536),
        ("max_tokens", "16384", 16_384),
    ],
)
def test_token_limit_presets_persist(qtbot: QtBot, services, field, label, expected) -> None:
    from PySide6.QtWidgets import QComboBox

    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab
    tab._on_new()
    tab._id.setText("preset-model")
    combo = getattr(tab, f"_{field}")
    assert isinstance(combo, QComboBox)
    assert combo.isEditable()
    assert combo.insertPolicy() == QComboBox.InsertPolicy.NoInsert
    combo.setCurrentIndex(combo.findText(label))
    tab._on_save()
    assert getattr(settings.load().models[0], field) == expected
    tab._list.setCurrentRow(0)
    assert combo.currentText() == label


@pytest.mark.parametrize(
    ("field", "label", "expected"),
    [("context_window", "262K", 262_144), ("max_tokens", "65536", 65_536)],
)
def test_token_limit_double_click_opens_presets(
    qtbot: QtBot, services, field, label, expected
) -> None:
    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    dialog.show()
    tab = dialog._models_tab
    tab._on_new()
    tab._id.setText("double-click-model")
    combo = getattr(tab, f"_{field}")
    edit = combo.lineEdit()
    assert edit is not None
    qtbot.waitUntil(edit.isVisible)

    qtbot.mouseClick(edit, Qt.MouseButton.LeftButton)
    assert not combo.view().isVisible()
    edit.selectAll()
    qtbot.keyClicks(edit, "23456")
    assert combo.value() == 23456
    assert not combo.view().isVisible()

    qtbot.mouseDClick(edit, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(combo.view().isVisible)
    assert combo.currentText() == "23456"
    qtbot.keyClick(combo.view(), Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not combo.view().isVisible())
    assert combo.value() == 23456

    qtbot.mouseDClick(edit, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(combo.view().isVisible)
    index = combo.model().index(combo.findText(label), combo.modelColumn())
    qtbot.mouseClick(
        combo.view().viewport(), Qt.MouseButton.LeftButton,
        pos=combo.view().visualRect(index).center(),
    )
    qtbot.waitUntil(lambda: not combo.view().isVisible())
    assert combo.currentText() == label
    assert combo.value() == expected
    tab._on_save()
    assert getattr(settings.load().models[0], field) == expected


def test_token_limits_custom_values_reload_and_default(qtbot: QtBot, services) -> None:
    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab
    tab._on_new()
    tab._id.setText("custom-model")
    tab._context_window.setEditText("345678")
    tab._max_tokens.setEditText("23456")
    tab._on_save()
    model = settings.load().models[0]
    assert (model.context_window, model.max_tokens) == (345678, 23456)
    tab._list.setCurrentRow(0)
    assert tab._context_window.currentText() == "345678"
    assert tab._max_tokens.currentText() == "23456"
    tab.reload()
    assert tab._context_window.value() == 345678
    tab._context_window.setEditText("")
    tab._max_tokens.setEditText("0")
    tab._on_save()
    model = settings.load().models[0]
    assert model.context_window is None
    assert model.max_tokens is None
    tab._on_new()
    assert tab._context_window.currentIndex() == 0
    assert tab._max_tokens.currentIndex() == 0


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("context_window", "-1"),
        ("context_window", "1.5"),
        ("context_window", "not-a-number"),
        ("context_window", "10000001"),
        ("max_tokens", "1000001"),
    ],
)
def test_invalid_token_limit_does_not_save(
    qtbot: QtBot, services, _no_modal_messageboxes, field, text
) -> None:
    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab
    tab._on_new()
    tab._id.setText("invalid-model")
    getattr(tab, f"_{field}").setEditText(text)
    tab._on_save()
    assert not settings.load().models
    assert _no_modal_messageboxes


def test_binding_list_has_record_list_style_and_tooltips(qtbot: QtBot, services) -> None:
    from PySide6.QtWidgets import QAbstractItemView

    from limbowave.ui import theme

    settings, credentials = services
    settings.upsert_endpoint(_endpoint_config())
    _import(settings, "relay-a", "model")
    settings.create_model(LogicalModel(id="model", name="model"))
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    dialog.setStyleSheet(theme.app_stylesheet())
    tab = dialog._models_tab
    tab._list.setCurrentRow(0)
    bindings = tab._bindings
    assert bindings.objectName() == "modelBindingsList"
    assert bindings.minimumHeight() >= 120
    assert bindings.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection
    assert bindings.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert bindings.item(0).toolTip() == bindings.item(0).text()
    bindings.setCurrentRow(0)
    assert tab._selected_binding_endpoint() == "relay-a"


def test_model_subtabs_keep_sidebar_outside_pages(qtbot: QtBot, services) -> None:
    settings, credentials = services
    settings.upsert_model(LogicalModel(id="first", name="First"))
    settings.upsert_model(LogicalModel(id="second", name="Second"))
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab
    dialog.show()
    tab._list.setCurrentRow(0)
    assert [tab._subtabs.tabText(i) for i in range(2)] == ["逻辑模型设置", "模型绑定"]
    assert not tab._stack.isAncestorOf(tab._list)
    assert tab._list.parentWidget().parentWidget() == tab._stack.parentWidget().parentWidget()
    assert tab._configuration_page.isAncestorOf(tab._id)
    for text in ("保存", "设为默认模型", "删除"):
        button = next(b for b in tab.findChildren(QPushButton) if b.text() == text)
        assert tab._configuration_page.isAncestorOf(button)
    for widget in (tab._bindings, tab._pair_combo, tab._bind_thinking, tab._probe_btn):
        assert tab._binding_page.isAncestorOf(widget)
    tab._subtabs.setCurrentIndex(1)
    qtbot.waitUntil(lambda: tab._binding_page.isVisible())
    assert tab._list.isVisible()
    tab._list.setCurrentRow(1)
    assert tab._selected_id() == "second"
    assert tab._id.text() == "second"
    assert tab._subtabs.currentIndex() == 1
    tab._on_new()
    qtbot.waitUntil(lambda: tab._configuration_page.isVisible())
    assert tab._subtabs.currentIndex() == 0
    assert tab._selected_id() is None


@pytest.mark.parametrize("existing", [False, True])
def test_model_save_opens_bindings(qtbot: QtBot, services, existing: bool) -> None:
    settings, credentials = services
    settings.upsert_endpoint(_endpoint_config())
    _import(settings, "relay-a", "test-model")
    if existing:
        settings.create_model(LogicalModel(id="test-model", name="Old name"))
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    dialog.show()
    tab = dialog._models_tab
    if existing:
        tab._list.setCurrentRow(0)
    else:
        tab._on_new()
        tab._id.setText("test-model")
    tab._name.setText("New name")
    save = next(b for b in tab.findChildren(QPushButton) if b.text() == "保存")
    save.click()
    qtbot.waitUntil(lambda: tab._binding_page.isVisible())
    assert tab._subtabs.currentIndex() == 1
    assert tab._selected_id() == "test-model"
    assert tab._bindings.count() == 1
    assert settings.load().models[0].name == "New name"
    assert tab._list.isVisible()


def test_invalid_model_save_stays_on_configuration(
    qtbot: QtBot, services, _no_modal_messageboxes
) -> None:
    settings, credentials = services
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab
    tab._on_new()
    tab._on_save()
    assert _no_modal_messageboxes
    assert tab._subtabs.currentIndex() == 0
    assert tab._stack.current_index == 0
    assert not settings.load().models


def test_pair_dropdown_cascades_by_site_and_ranks_model_ids(qtbot: QtBot, services) -> None:
    settings, credentials = services
    for site_id, name in (("relay-a", "站点甲"), ("relay-b", "站点乙"), ("empty", "空站点")):
        settings.upsert_endpoint(_endpoint_config(site_id, name))
    for model_id in ("unrelated", "gpt-5.1", "gpt-5-free", "GPT-5", "gpt-5"):
        _import(settings, "relay-a", model_id)
    _import(settings, "relay-b", "gpt-5")
    settings.create_model(LogicalModel(id="gpt-5", name="GPT"))
    settings.unbind_endpoint("gpt-5", "relay-a")
    settings.unbind_endpoint("gpt-5", "relay-b")
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._models_tab
    tab._select_model("gpt-5")
    sites, models = tab._pair_site_combo, tab._pair_combo
    assert [sites.itemText(i) for i in range(sites.count())] == ["站点甲", "站点乙", "空站点"]
    assert [models.itemText(i) for i in range(models.count())] == [
        "gpt-5", "GPT-5", "gpt-5-free", "gpt-5.1", "unrelated",
    ]
    sites.setCurrentIndex(sites.findData("relay-b"))
    assert models.count() == 1
    assert models.currentText() == "gpt-5"
    assert tab._pair_choice() == ("relay-b", "gpt-5")
    tab._on_pair()
    saved = settings.load().models[0]
    assert [(b.endpoint_id, b.model_id) for b in saved.bindings] == [("relay-b", "gpt-5")]
    assert sites.currentData() == "relay-b"
    assert tab._pair_choice() is None
    assert not tab._pair_btn.isEnabled()
    sites.setCurrentIndex(sites.findData("empty"))
    assert tab._pair_choice() is None
    assert not tab._pair_btn.isEnabled()
    sites.setCurrentIndex(sites.findData("relay-a"))
    assert tab._pair_btn.isEnabled()
    tab._on_new()
    assert not sites.isEnabled()
    assert tab._pair_choice() is None
    assert not tab._pair_btn.isEnabled()


def test_settings_dialog_has_shared_about_tab(qtbot: QtBot, services) -> None:
    from PySide6.QtWidgets import QTabWidget

    from limbowave.ui.about_page import AboutPage

    dialog = SettingsDialog(*services)
    qtbot.addWidget(dialog)
    tabs = dialog.findChild(QTabWidget)
    assert tabs is not None
    assert tabs.tabText(tabs.count() - 1) == "关于"
    tabs.setCurrentIndex(tabs.count() - 1)
    assert isinstance(tabs.currentWidget(), AboutPage)
    assert tabs.currentWidget() is dialog.about_tab


def test_endpoint_rpm_save_reload_and_new_default(qtbot: QtBot, services) -> None:
    settings, credentials = services
    original = _endpoint_config("a", "A").model_copy(update={
        "rpm": 17, "timeout_seconds": 29, "headers": {"X-Test": "preserved"},
    })
    settings.upsert_endpoint(original)
    dialog = SettingsDialog(settings, credentials)
    qtbot.addWidget(dialog)
    tab = dialog._endpoints_tab
    tab._select_endpoint("a")
    assert tab._rpm.value() == 17
    assert tab._rpm.minimum() == 1
    tab._rpm.setValue(9)
    tab._on_save()
    saved = settings.load().endpoints[0]
    assert saved.rpm == 9
    assert saved.timeout_seconds == 29
    assert saved.headers == original.headers
    reopened = SettingsDialog(settings, credentials)
    qtbot.addWidget(reopened)
    reopened._endpoints_tab._select_endpoint("a")
    assert reopened._endpoints_tab._rpm.value() == 9
    tab._on_new()
    assert tab._rpm.value() == 5
