"""请求日志查询（Task 3.3：先实现日志，再调试真实站点）。

把一轮 run 的应用意图快照与传输快照装配成一份**可解释的**日志视图：
每个最终参数能追到来源（应用意图 / Pi 注入 / 站点默认值），这是设计计划
§四.4「不允许运行时私自注入来源不明的字段」的用户可见面。

密钥红线：``TransportSnapshot`` 在落库前已脱敏（源端 + 应用纵深两道），
本服务**只读快照，永远接触不到密钥本体**。
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from limbowave.application.repositories import UnitOfWork, UnitOfWorkFactory
from limbowave.domain.run import RunLogSummary, RunRecord
from limbowave.domain.snapshots import RequestIntentSnapshot, TransportSnapshot


@dataclass(frozen=True, slots=True)
class RunLogEntry:
    """一轮请求的完整日志视图。"""

    run: RunRecord
    intent: RequestIntentSnapshot | None
    transports: list[TransportSnapshot] = field(default_factory=list)


REQUEST_LOG_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class RunLogPage:
    entries: list[RunLogSummary]
    has_more: bool


class RequestLogService:
    """按会话/分支组织请求日志。只读。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory
        self._reader = ThreadPoolExecutor(max_workers=2, thread_name_prefix="request-log")

    def list_summaries(
        self, conversation_id: str, *, offset: int = 0,
        limit: int = REQUEST_LOG_PAGE_SIZE,
    ) -> RunLogPage:
        """Bounded list query: never materialize/decrypt snapshots just to count them."""
        if offset < 0 or not 1 <= limit <= REQUEST_LOG_PAGE_SIZE:
            raise ValueError("请求日志分页参数超出范围")
        with self._uow_factory() as uow:
            rows = uow.runs.list_log_summaries(conversation_id, limit=limit + 1, offset=offset)
        return RunLogPage(rows[:limit], has_more=len(rows) > limit)

    def load_page_async(self, conversation_id: str, *, offset: int = 0) -> Future[RunLogPage]:
        return self._reader.submit(self.list_summaries, conversation_id, offset=offset)

    def load_run_async(self, run_id: str) -> Future[RunLogEntry | None]:
        return self._reader.submit(self.get_for_run, run_id)

    def close(self) -> None:
        """Drain readers before closing the database/vault (call off the GUI thread)."""
        self._reader.shutdown(wait=True, cancel_futures=True)

    def list_for_conversation(self, conversation_id: str) -> list[RunLogEntry]:
        """一个会话的全部运行日志，按时间正序。"""
        with self._uow_factory() as uow:
            return self._assemble(uow, conversation_id)

    def list_for_branch(self, conversation_id: str, branch_id: str) -> list[RunLogEntry]:
        """一个分支的运行日志（设计计划 §三.2：分支专属请求记录）。"""
        entries = self.list_for_conversation(conversation_id)
        return [e for e in entries if e.run.branch_id == branch_id]

    def get_for_run(self, run_id: str) -> RunLogEntry | None:
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id)
            if run is None:
                return None
            return RunLogEntry(
                run=run,
                intent=uow.snapshots.get_intent(run_id),
                transports=uow.snapshots.list_transport(run_id),
            )

    # ---------- 内部 ----------

    @staticmethod
    def _assemble(uow: UnitOfWork, conversation_id: str) -> list[RunLogEntry]:
        entries = []
        for run in uow.runs.list_for_conversation(conversation_id):
            entries.append(
                RunLogEntry(
                    run=run,
                    intent=uow.snapshots.get_intent(run.id),
                    transports=uow.snapshots.list_transport(run.id),
                )
            )
        entries.sort(key=lambda e: e.run.created_at)
        return entries
