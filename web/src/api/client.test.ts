import { describe, it, expect, vi } from "vitest";
import { ApiClient, consumePairingFragment } from "./client";
import { wireCommand, WireState, adaptEvent, historySchema } from "./adapter";
const session = {
  server_epoch: "e",
  csrf_token: "csrf",
  password_required: false,
  transport_secure: true,
  device_id: "d",
  permissions: { chat: true, tools: false },
};
describe("security and wire adapter", () => {
  it("clears ticket immediately without storing it", () => {
    const replaceState = vi.fn();
    expect(
      consumePairingFragment(
        { hash: "#ticket=private", pathname: "/", search: "" },
        { replaceState },
      ),
    ).toBe("private");
    expect(replaceState).toHaveBeenCalledWith(null, "", "/");
    expect(localStorage.length).toBe(0);
  });
  it("uses bootstrap csrf and same-origin no-store, never an auth header", async () => {
    const transport = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(session)))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ status: "paired" })),
      );
    const api = new ApiClient(transport);
    await api.session();
    await api.pair({ ticket: "ticket" });
    expect(transport.mock.calls[1][1]).toMatchObject({
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      headers: { "X-CSRF-Token": "csrf" },
    });
    expect(JSON.stringify(transport.mock.calls[1][1].headers)).not.toContain(
      "Authorization",
    );
  });
  it("bootstraps unauthenticated pairing through pair/status", async () => {
    const transport = vi
      .fn()
      .mockResolvedValueOnce(new Response("{}", { status: 401 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ status: "unpaired", csrf_token: "bootstrap" }),
        ),
      );
    const api = new ApiClient(transport);
    expect(await api.session()).toEqual({
      authenticated: false,
      csrf_token: "bootstrap",
    });
    expect(transport.mock.calls[1][0]).toBe("/api/v1/pair/status");
  });
  it("refuses writes before csrf bootstrap", async () => {
    const transport = vi.fn();
    await expect(
      new ApiClient(transport).pair({ code: "12345678" }),
    ).rejects.toThrow("安全会话");
    expect(transport).not.toHaveBeenCalled();
  });
  it("sends the strict Python command union and global revision", () => {
    expect(
      wireCommand(
        {
          kind: "new-session",
          model_id: "m",
          client_command_id: "id",
          server_epoch: "e",
        },
        7,
      ),
    ).toEqual({
      type: "new_session",
      client_command_id: "id",
      server_epoch: "e",
      expected_revision: 7,
    });
  });
  it("restores live content with cursor from authoritative state", () => {
    const s = WireState.parse({
      server_epoch: "e",
      revision: 4,
      seq: 12,
      available: true,
      busy: true,
      conversation_id: "c",
      branch_id: "b",
      run_id: "r",
      stream: {
        message_id: "m",
        conversation_id: "c",
        branch_id: "b",
        run_id: "r",
        text: "尚未落库",
        thinking: "思考",
      },
    });
    expect(s.messages[0].content).toBe("尚未落库");
    expect(s.seq).toBe(12);
    expect(s.active_run?.run_id).toBe("r");
  });
  it("normalizes backend delta and sparse resync frames", () => {
    expect(
      adaptEvent({ server_epoch: "e", seq: 3, kind: "resync_required" }).kind,
    ).toBe("resync_required");
    expect(
      adaptEvent({
        protocol_version: 1,
        server_epoch: "e",
        seq: 2,
        kind: "assistant_delta",
        payload: { delta: "文字" },
      }).payload,
    ).toEqual({ text: "文字" });
  });
});
it("preserves historical partial status and restores segment boundaries", () => {
  const snapshot = WireState.parse({
    server_epoch: "e",
    revision: 1,
    seq: 2,
    available: true,
    busy: true,
    conversation_id: "c",
    branch_id: "b",
    run_id: "r",
    stream: {
      conversation_id: "c",
      branch_id: "b",
      run_id: "r",
      message_id: "m",
      text: "",
      thinking: "",
      segments: [{ content: "工具前", thinking: "考虑" }],
    },
  });
  expect(snapshot.messages[0].content).toBe("工具前\n\n");
  expect(
    adaptEvent({
      server_epoch: "e",
      seq: 3,
      kind: "assistant_end",
      payload: { text: "工具前" },
    }).kind,
  ).toBe("resync_required");
  expect(
    adaptEvent({
      server_epoch: "e",
      seq: 4,
      kind: "assistant_start",
      payload: {},
    }).kind,
  ).toBe("resync_required");
});

it("restores partial/failed history and tool metadata without private fields", () => {
  const schema = historySchema("c", "b");
  const tool = {
    tool_id: "t",
    name: "Lookup",
    status: "completed",
    args: { secret: "private" },
    result: "private",
    error: "private",
    summary: "private",
  };
  const page = schema.parse({
    items: [
      {
        id: "m",
        role: "assistant",
        content: "partial output",
        status: "partial",
        run_id: "r",
        tools: [tool],
      },
      { id: "failed", role: "assistant", content: "", status: "failed" },
    ],
    next_cursor: null,
  });
  expect(page.items[0]).toMatchObject({
    status: "aborted",
    run_id: "r",
    tools: [{ tool_id: "t", name: "Lookup", status: "completed", summary: "" }],
  });
  expect(JSON.stringify(page)).not.toContain("private");
  expect(page.items[1]).toMatchObject({ status: "failed", tools: [] });
  expect(() =>
    schema.parse({
      items: [{ id: "unknown", role: "assistant" }],
      next_cursor: null,
    }),
  ).toThrow();
});
it("restores stream tools on a cold snapshot and preserves terminal status", () => {
  const input = {
    server_epoch: "e",
    revision: 1,
    seq: 2,
    available: true,
    busy: false,
    conversation_id: "c",
    branch_id: "b",
    run_id: "r",
    stream: {
      conversation_id: "c",
      branch_id: "b",
      run_id: "r",
      message_id: "m",
      text: "part",
      status: "partial",
      tools: [
        {
          tool_id: "t",
          name: "Lookup",
          status: "failed",
          args: "private",
          result: "private",
          error: "private",
        },
      ],
    },
  };
  const snapshot = WireState.parse(input);
  expect(snapshot.messages[0]).toMatchObject({
    status: "aborted",
    tools: [{ tool_id: "t", name: "Lookup", status: "failed", summary: "" }],
  });
  expect(JSON.stringify(snapshot)).not.toContain("private");
  expect(
    WireState.parse({
      ...input,
      stream: { ...input.stream, status: "failed", tools: undefined },
    }).messages[0],
  ).toMatchObject({ status: "failed", tools: [] });
});
