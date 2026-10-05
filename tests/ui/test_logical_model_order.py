"""Both model pickers sort display names, without altering routing/configuration order."""

import pytest
from PySide6.QtCore import Qt

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.models import LogicalModel
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.ui.logical_models_tab import LogicalModelsTab
from limbowave.ui.model_selector import ModelSelector, ModelSite

ENTRIES = [
    ("gpt-first", "Zulu"),
    ("gpt-second", "beta"),
    ("gpt-third", "Alpha"),
    ("gpt-fourth", "阿尔法"),
    ("gpt-fifth", "深度模型"),
]
EXPECTED_IDS = ["gpt-fourth", "gpt-third", "gpt-second", "gpt-fifth", "gpt-first"]


@pytest.fixture
def settings_tab(qtbot, tmp_path):
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    for model_id, name in ENTRIES:
        settings.upsert_model(LogicalModel(id=model_id, name=name))
    settings.set_default_model("gpt-first")
    tab = LogicalModelsTab(settings)
    qtbot.addWidget(tab)
    return settings, tab


def _ids(tab):
    return [tab._list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(tab._list.count())]


def test_settings_order_uses_names_not_ids_or_default_marker(settings_tab):
    settings, tab = settings_tab
    assert _ids(tab) == EXPECTED_IDS
    assert [model.id for model in settings.load().models] == [value[0] for value in ENTRIES]
    assert settings.load().default_model_id == "gpt-first"


def test_reload_reorders_new_models_without_losing_selection_or_unsaved_name(settings_tab):
    settings, tab = settings_tab
    tab._select_model("gpt-first")
    tab._name.setText("未保存的名称")
    settings.upsert_model(LogicalModel(id="new", name="aardvark"))
    tab.reload()
    assert _ids(tab) == ["new", *EXPECTED_IDS]
    assert tab._selected_id() == "gpt-first"
    assert tab._name.text() == "未保存的名称"
    assert settings.load().models[0].name == "Zulu"


def test_saved_rename_moves_row_and_keeps_selection(settings_tab):
    settings, tab = settings_tab
    tab._select_model("gpt-first")
    tab._name.setText("aardvark")
    tab._on_save()
    assert _ids(tab) == ["gpt-first", *EXPECTED_IDS[:-1]]
    assert tab._selected_id() == "gpt-first"
    assert settings.load().default_model_id == "gpt-first"


def test_new_model_inserts_into_sorted_position(settings_tab):
    _settings, tab = settings_tab
    tab._on_new()
    tab._id.setText("new-model")
    tab._name.setText("aardvark")
    tab._on_save()
    assert _ids(tab) == ["new-model", *EXPECTED_IDS]
    assert tab._selected_id() == "new-model"


def test_automatic_acronyms_do_not_overwrite_manual_names(settings_tab):
    _settings, tab = settings_tab
    tab._on_new()
    tab._id.setText("glm-5")
    assert tab._name.text() == "GLM 5"
    tab._id.setText("gpt-5")
    assert tab._name.text() == "GPT 5"
    tab._name.setText("我自己的 Glm / Gpt")
    tab._id.setText("glm-6")
    assert tab._name.text() == "我自己的 Glm / Gpt"


def test_composer_sorts_model_actions_but_keeps_selection_and_site_identity(qtbot):
    combo = ModelSelector()
    qtbot.addWidget(combo)
    for model_id, name in ENTRIES:
        combo.addItem(name, model_id)
    combo.setCurrentIndex(0)
    combo.set_sites({"gpt-second": (ModelSite("site-b", "站点 B", "gpt-5"),)})
    combo.show()
    combo.showPopup()
    try:
        menu = combo._menu.actions()[0].menu()
        assert [a.data() for a in menu.actions()] == EXPECTED_IDS
        assert combo.currentData() == "gpt-first"
        assert next(a for a in menu.actions() if a.isChecked()).data() == "gpt-first"
        # Sorting is a presentation projection: no QComboBox row or config mutation.
        assert [combo.itemData(i) for i in range(combo.count())] == [v[0] for v in ENTRIES]
        action = next(a for a in menu.actions() if a.data() == "gpt-second")
        with qtbot.waitSignal(combo.currentIndexChanged):
            action.trigger()
        assert combo.currentData() == "gpt-second"
        assert combo._sites["gpt-second"][0].endpoint_id == "site-b"
    finally:
        combo.hidePopup()
