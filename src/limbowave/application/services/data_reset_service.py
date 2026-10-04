"""数据重置：双重验证、不可跳过的等待和一次性删除授权。

只处理应用拥有的固定路径，不递归删除整个数据根目录；备份、导出和用户工作文件保留。
调用方必须先停掉后台任务并关闭数据库工厂，再执行已确认的删除。
"""

from __future__ import annotations

import shutil
import time
from enum import StrEnum
from pathlib import Path

from limbowave.domain.platform_capabilities import DATA_RESET_UNAVAILABLE
from limbowave.infrastructure.crypto.recovery import HelloGate, hello_gate
from limbowave.infrastructure.crypto.vault import Vault

RESET_WAIT_SECONDS = 5
DATABASE_FILES = ("limbowave.db", "limbowave.db-wal", "limbowave.db-shm", "limbowave.db-journal")
# 主密钥最后删：前面的删除失败时，尚未删除的加密内容仍有解锁途径。
RESET_DIRECTORIES = ("blobs", "runtime", "themes", "vault")
RESET_FILES = ("config.json", "preferences.json", "vault.tmp", "vault.json")


class ResetScope(StrEnum):
    DATABASE = "database"
    ALL = "all"


class DataResetError(Exception):
    """验证、确认或删除未完成；不得当作重置成功。"""


class DataResetService:
    """每次尝试独立的授权。取消后不能复用，也不保留密码或主密钥。"""

    def __init__(self, data_root: Path, scope: ResetScope) -> None:
        self._root = data_root.resolve()
        self._scope = ResetScope(scope)
        self._started = False
        self._verified = False
        self._deadline: float | None = None
        self._confirmed = False
        self._cancelled = False
        self._consumed = False

    @property
    def scope(self) -> ResetScope:
        return self._scope

    def authenticate(self, password: str, *, gate: HelloGate | None = None) -> None:
        """复用密码找回的 Hello 接口，但绝不允许账户级免验证降级。

        在工作线程调用。现在复用本机的系统 PIN 弹窗；HelloGate 保留未来扩展点，
        本服务不读取 PIN，也不自行实现或宣称验证了某一种生物特征。
        """
        if self._started or self._cancelled:
            raise DataResetError("本次重置已结束，请重新发起并验证身份")
        self._started = True
        vault = Vault(self._root / "vault.json")
        try:
            vault.unlock(password)  # 即使当前应用已解锁，也必须重新验证主密码。
        finally:
            vault.lock()
        if self._cancelled:
            raise DataResetError("已取消重置")
        active_gate = gate if gate is not None else hello_gate()
        if active_gate.name == "unavailable":
            raise DataResetError(DATA_RESET_UNAVAILABLE)
        if active_gate.name != "windows-hello" or not active_gate.available():
            raise DataResetError("Windows Hello 不可用，请先在 Windows 设置中配置 PIN")
        if active_gate.verify("验证身份以删除 LimboWave 数据") is not True:
            raise DataResetError("Windows Hello 验证未通过或已取消，未执行删除")
        if self._cancelled:
            raise DataResetError("已取消重置")
        self._verified = True

    def begin_confirmation(self) -> None:
        """身份验证结束、确认界面显示时开始计时，不把输入 PIN 的时间算进去。"""
        if not self._verified or self._cancelled or self._deadline is not None:
            raise DataResetError("请重新验证主密码和 Windows Hello")
        self._deadline = time.monotonic() + RESET_WAIT_SECONDS

    def confirm(self) -> None:
        """最终点击确认；界面禁用按钮之外，服务层也检查实际经过的时间。"""
        if (
            not self._verified or self._cancelled or self._confirmed or self._consumed
            or self._deadline is None
        ):
            raise DataResetError("本次重置尚未验证或已结束")
        if time.monotonic() < self._deadline:
            raise DataResetError("必须等待完整 5 秒后才能确认删除")
        self._confirmed = True

    def cancel(self) -> None:
        self._cancelled = True
        self._verified = False
        self._confirmed = False

    def execute(self) -> None:
        """同步直接删除，不走回收站、不留待下次启动，也不自动生成备份。

        文件系统没有多文件删除事务；中途失败会报错并列明已删除的路径。
        授权无论成功或失败都只使用一次，不能在后台自动重试。
        """
        if not self._confirmed or self._cancelled or self._consumed:
            raise DataResetError("缺少有效的最终确认，拒绝删除")
        self._consumed = True
        targets = [(self._root / name, False) for name in DATABASE_FILES]
        if self._scope is ResetScope.ALL:
            targets += [(self._root / name, True) for name in RESET_DIRECTORIES]
            targets += [(self._root / name, False) for name in RESET_FILES]
        # 在删任何内容之前核对全部目标，避免错误路径或链接扩大删除范围。
        for path, directory in targets:
            if path.is_symlink() or path.is_junction():
                raise DataResetError(f"拒绝删除链接路径：{path.name}（未执行删除）")
            if path.exists() and path.is_dir() != directory:
                raise DataResetError(f"数据路径类型异常：{path.name}（未执行删除）")
        removed: list[str] = []
        for path, directory in targets:
            try:
                if not path.exists():
                    continue
                if directory:
                    shutil.rmtree(path)
                else:
                    path.unlink()
                removed.append(path.name)
            except OSError as exc:
                detail = "、".join(removed) if removed else "无"
                partial = "失败的目录也可能已部分删除。" if directory else ""
                raise DataResetError(
                    f"无法删除 {path.name}；已删除：{detail}。{partial}"
                    "重置未完成，请关闭占用文件的程序后检查数据目录。"
                ) from exc
