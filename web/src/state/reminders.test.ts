import { expect, it } from "vitest";
import { adaptEvent } from "../api/adapter";
import { applyEvent, emptyChat } from "./chat";

const notice = (seq = 1) =>
  adaptEvent({
    protocol_version: 1,
    server_epoch: "epoch",
    seq,
    kind: "reminder",
    conversation_id: "old-chat",
    branch_id: "old-branch",
    run_id: null,
    message_id: "notice_reminder_123",
    payload: {
      text: "⏰ 系统定时提醒\n喝水",
      created_at: "2026-10-25T14:00:00Z",
    },
  });

it("adapts a timer to a completed standalone message rather than a model run", () => {
  const event = notice();
  expect(event.kind).toBe("message.updated");
  expect(event.run_id).toBeNull();
  expect(event.payload).toMatchObject({
    role: "assistant",
    status: "completed",
    content: "⏰ 系统定时提醒\n喝水",
  });
});

it("appends and deduplicates timer notices without changing a live run", () => {
  const run = {
    run_id: "live",
    conversation_id: "other-chat",
    branch_id: "other-branch",
    status: "running" as const,
  };
  const initial = { ...emptyChat, server_epoch: "epoch", active_run: run };
  const next = applyEvent(initial, notice());
  expect(next.resync).toBe(false);
  expect(next.active_run).toEqual(run);
  expect(next.messages).toHaveLength(1);
  expect(next.messages[0].conversation_id).toBe("old-chat");
  const repeated = applyEvent(next, notice(2));
  expect(repeated.messages).toHaveLength(1);
  expect(repeated.active_run).toEqual(run);
});

it("does not allow runless deltas to pretend to be persisted reminders", () => {
  const event = {
    ...notice(),
    kind: "message.delta" as const,
    payload: { text: "bad" },
  };
  expect(
    applyEvent({ ...emptyChat, server_epoch: "epoch" }, event).resync,
  ).toBe(true);
});
