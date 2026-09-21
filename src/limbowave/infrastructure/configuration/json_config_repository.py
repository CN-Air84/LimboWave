"""JSON 配置仓库：把 AppConfiguration 落盘为普通 JSON。

这是普通配置，**绝不含密钥本体**（密钥只以 credential_ref 引用存在）。
文件缺失或为空时返回空配置（优雅降级的数据来源）。
"""

from __future__ import annotations

import json
from pathlib import Path

from limbowave.domain.configuration import AppConfiguration


class JsonConfigRepository:
    """把配置读写为 <path>/config.json。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> AppConfiguration:
        if not self._path.is_file():
            return AppConfiguration()
        text = self._path.read_text(encoding="utf-8").strip()
        if not text:
            return AppConfiguration()
        data = json.loads(text)
        return AppConfiguration.model_validate(data)

    def save(self, config: AppConfiguration) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = config.model_dump(mode="json")
        self._path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
