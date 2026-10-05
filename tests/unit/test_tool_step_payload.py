"""Tool display work is pure, bounded, and never changes persisted audit fields."""

from limbowave.application.tool_step_payload import prepare_tool_event, prepare_tool_step
from limbowave.domain.tool_step import ToolStatus, ToolStep, finish


def test_projection_is_transient_and_keeps_full_arguments():
    step = ToolStep("c1", "write", args={"content": "a" * 100_000})
    prepared = prepare_tool_step(step)
    assert prepared.to_json() == step.to_json()
    assert prepared == step
    assert len(prepared.display_summary) <= 201
    assert len(prepared.display_detail) < 17000
    assert "审计" in prepared.display_detail
    assert prepared.args["content"] == "a" * 100_000


def test_finish_invalidates_stale_presentation():
    started = prepare_tool_step(ToolStep("c1", "read", args={"path": "file"}))
    finished = finish(started, result="new result", duration_ms=42)
    assert finished.display_detail is None
    prepared = prepare_tool_step(finished)
    assert "new result" in prepared.display_detail
    assert prepared.summary == "new result"
    assert "42ms" in prepared.display_detail


def test_event_projection_preserves_start_and_failure_fields():
    started = prepare_tool_event("tool.start", {"toolCallId": "c", "toolName": "read",
                                 "args": {"path": "file"}}, None, 7, 0)
    done = prepare_tool_event("tool.end", {"toolCallId": "c", "isError": True,
                              "result": {"error": "not found"}}, started, 18, 11)
    assert done.status is ToolStatus.ERROR
    assert done.created_at_ms == 7
    assert done.duration_ms == 11
    assert done.args == started.args
    assert "not found" in done.display_detail
