import { describe, it, expect } from "vitest";
import { applyEvent, emptyChat, fromSnapshot, mergeMessages } from "./chat";
import type { ChatEvent, Message } from "../api/dto";
const start = { ...emptyChat, server_epoch: "epoch" };
const event = (
  seq: number,
  kind: ChatEvent["kind"] = "message.delta",
  payload: unknown = { text: "你好" },
): ChatEvent => ({
  protocol_version: 1,
  server_epoch: "epoch",
  seq,
  kind,
  conversation_id: "c",
  branch_id: "b",
  run_id: "r",
  message_id: "m",
  payload,
});
describe("ordered chat reducer", () => {
  it("deduplicates sequence and batches text without duplicating a message", () => {
    const one = applyEvent(start, event(1));
    expect(applyEvent(one, event(1))).toBe(one);
    expect(applyEvent(one, event(2)).messages[0].content).toBe("你好你好");
  });
  it("requires a snapshot on gaps, epoch changes and resync even at the same seq", () => {
    expect(applyEvent(start, event(3)).resync).toBe(true);
    expect(
      applyEvent(start, { ...event(1), server_epoch: "next" }).resync,
    ).toBe(true);
    expect(applyEvent(start, event(0, "resync_required")).resync).toBe(true);
  });
  it("rejects cross-branch collisions", () => {
    const one = applyEvent(start, event(1));
    expect(applyEvent(one, { ...event(2), branch_id: "other" }).resync).toBe(
      true,
    );
  });
  it("ignores deltas after run terminal state", () => {
    let state = applyEvent(start, event(1));
    state = applyEvent(
      state,
      event(2, "run.updated", {
        run_id: "r",
        conversation_id: "c",
        branch_id: "b",
        status: "completed",
      }),
    );
    state = applyEvent(state, event(3));
    expect(state.messages[0].content).toBe("你好");
    expect(state.active_run).toBeNull();
    expect(state.seq).toBe(3);
  });
  it("does not treat a completed user message as a terminal run", () => {
    const user = {
      ...applyEvent(start, event(1)).messages[0],
      role: "user",
      status: "completed",
    } as Message;
    const state = fromSnapshot({
      ...start,
      messages: [user],
      active_run: {
        run_id: "r",
        conversation_id: "c",
        branch_id: "b",
        status: "running",
      },
    });
    expect(
      applyEvent(state, { ...event(1), message_id: "assistant" }).messages,
    ).toHaveLength(2);
  });
  it("deduplicates stable history ids with live snapshot taking precedence", () => {
    const m = applyEvent(start, event(1)).messages[0];
    expect(mergeMessages([m], [{ ...m, content: "最终结果" }])).toEqual([
      { ...m, content: "最终结果" },
    ]);
  });
  it("keeps thinking and tool payload scoped", () => {
    let s = applyEvent(start, event(1, "thinking.delta", { text: "考虑" }));
    s = applyEvent(
      s,
      event(2, "tool.updated", {
        tool_id: "t",
        name: "查找",
        status: "waiting",
      }),
    );
    expect(s.messages[0].thinking).toBe("考虑");
    expect(s.messages[0].tools[0].status).toBe("waiting");
  });
});

it("persisted partial/failed beats a settled snapshot captured before finalization", () => {
  const message = applyEvent(start, event(1)).messages[0];
  for (const status of ["aborted", "failed"] as const) {
    expect(
      mergeMessages(
        [{ ...message, status }],
        [{ ...message, status: "completed" }],
      )[0].status,
    ).toBe(status);
  }
  expect(
    mergeMessages(
      [{ ...message, status: "failed" }],
      [{ ...message, run_id: "new-run", status: "completed" }],
    )[0].status,
  ).toBe("completed");
});
