"""配置服务：加载与保存应用权威配置。

配置是普通 JSON（不含密钥本体）。仓库实现可替换（测试用内存实现）。
"""

from __future__ import annotations

from typing import Protocol

from limbowave.domain.configuration import AppConfiguration


class ConfigurationRepository(Protocol):
    """配置仓库抽象。infrastructure 提供 JSON 实现，测试用内存实现。"""

    def load(self) -> AppConfiguration: ...

    def save(self, config: AppConfiguration) -> None: ...


class ConfigurationService:
    """应用配置的读写入口。"""

    def __init__(self, repository: ConfigurationRepository) -> None:
        self._repository = repository

    def load(self) -> AppConfiguration:
        return self._repository.load()

    def save(self, config: AppConfiguration) -> None:
        self._repository.save(config)

    def current_model_id(self, config: AppConfiguration) -> str | None:
        """当前应使用的逻辑模型：默认模型，否则第一个。"""
        if config.default_model_id is not None:
            return config.default_model_id
        return config.models[0].id if config.models else None
