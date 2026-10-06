import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { ChatController, draftKey } from "./controller";
import { ApiClient, ApiError } from "../api/client";
import type { ChatEvent } from "../api/dto";
vi.mock("../api/password", () => ({
  encryptPassword: vi.fn((_password, challenge) => ({
    challenge_id: challenge.challenge_id,
    encrypted_key: "wrapped",
    iv: "nonce",
    ciphertext: "sealed",
  })),
}));
const session = {
  authenticated: true as const,
  server_epoch: "e",
  csrf_token: "csrf",
  password_required: false,
  transport_secure: true,
  device_name: "phone",
  capabilities: { chat: true, remote_agent: false },
};
const target = {
  conversation_id: "c",
  branch_id: "b",
  revision: 1,
  model_id: "m",
  title: "会话",
  updated_at: "2026-10-06",
};
let api: ApiClient, controller: ChatController, emit: (e: ChatEvent) => void;
beforeEach(async () => {
  api = new ApiClient();
  vi.spyOn(api, "session").mockResolvedValue(session);
  vi.spyOn(api, "state").mockResolvedValue({
    server_epoch: "e",
    revision: 1,
    seq: 0,
    active_run: null,
    messages: [],
  });
  vi.spyOn(api, "models").mockResolvedValue({
    items: [{ model_id: "m", label: "Model" }],
  });
  vi.spyOn(api, "conversations").mockResolvedValue({
    items: [target],
    next_cursor: null,
  });
  vi.spyOn(api, "messages").mockResolvedValue({ items: [], next_cursor: null });
  controller = new ChatController(
    api,
    async (_epoch, _seq, signal, callback) => {
      emit = callback;
      await new Promise<void>((resolve) =>
        signal.addEventListener("abort", () => resolve()),
      );
    },
  );
  await controller.initialize();
  await controller.select(target);
  controller.setDraft("保留草稿");
});
afterEach(() => controller.stop());
it("409 preserves draft and never queues or retries a POST", async () => {
  const command = vi
    .spyOn(api, "command")
    .mockRejectedValue(new ApiError(409, "busy", "电脑正在生成"));
  await controller.send();
  expect(command).toHaveBeenCalledOnce();
  expect(controller.getSnapshot().drafts[draftKey(target)]).toBe("保留草稿");
  expect(controller.getSnapshot().pending).toBeNull();
  expect(controller.getSnapshot().error).toContain("电脑");
});
it("network ambiguity reads receipt, never repeats send", async () => {
  const command = vi
    .spyOn(api, "command")
    .mockRejectedValue(new TypeError("offline"));
  vi.spyOn(api, "receipt").mockImplementation(async (id) => ({
    client_command_id: id,
    status: "accepted",
  }));
  await controller.send();
  expect(command).toHaveBeenCalledOnce();
  expect(api.receipt).toHaveBeenCalledOnce();
  expect(controller.getSnapshot().drafts[draftKey(target)]).toBe("");
});
it("pending receipt blocks repeated sends and preserves edited draft", async () => {
  const command = vi.spyOn(api, "command").mockImplementation(async (c) => ({
    client_command_id: c.client_command_id,
    status: "pending",
  }));
  await controller.send();
  controller.setDraft("新的草稿");
  await controller.send();
  expect(command).toHaveBeenCalledOnce();
  vi.spyOn(api, "receipt").mockImplementation(async (id) => ({
    client_command_id: id,
    status: "accepted",
  }));
  await controller.checkPending();
  expect(controller.getSnapshot().drafts[draftKey(target)]).toBe("新的草稿");
});
it("epoch change clears ambiguous command, preserves draft and never posts", async () => {
  const command = vi.spyOn(api, "command").mockImplementation(async (c) => ({
    client_command_id: c.client_command_id,
    status: "pending",
  }));
  await controller.send();
  vi.mocked(api.state).mockResolvedValue({
    server_epoch: "next",
    revision: 0,
    seq: 0,
    active_run: null,
    messages: [],
  });
  vi.mocked(api.session).mockResolvedValue({
    ...session,
    server_epoch: "next",
  });
  await controller.sync();
  await vi.waitFor(() =>
    expect(controller.getSnapshot().chat.server_epoch).toBe("next"),
  );
  expect(command).toHaveBeenCalledOnce();
  expect(controller.getSnapshot().pending).toBeNull();
  expect(controller.getSnapshot().drafts[draftKey(target)]).toBe("保留草稿");
});
it("resync restores unpersisted output and does not resend", async () => {
  const command = vi.spyOn(api, "command");
  vi.mocked(api.state).mockResolvedValue({
    server_epoch: "e",
    revision: 2,
    seq: 100,
    active_run: {
      conversation_id: "c",
      branch_id: "b",
      run_id: "r",
      status: "running",
    },
    messages: [
      {
        message_id: "live",
        conversation_id: "c",
        branch_id: "b",
        run_id: "r",
        role: "assistant",
        content: "恢复的内容",
        thinking: "",
        tools: [],
        status: "streaming",
      },
    ],
  });
  emit({
    protocol_version: 1,
    server_epoch: "e",
    seq: 100,
    kind: "resync_required",
    conversation_id: null,
    branch_id: null,
    message_id: null,
    run_id: null,
    payload: {},
  });
  await vi.waitFor(() =>
    expect(controller.getSnapshot().chat.messages[0].content).toBe(
      "恢复的内容",
    ),
  );
  expect(command).not.toHaveBeenCalled();
});
it("slow history from an earlier selection cannot overwrite the current view", async () => {
  let release!: (page: { items: []; next_cursor: null }) => void;
  vi.mocked(api.messages)
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    )
    .mockResolvedValueOnce({ items: [], next_cursor: "new-cursor" });
  const first = controller.select(target);
  await controller.select({ ...target, conversation_id: "other" });
  release({ items: [], next_cursor: null });
  await first;
  expect(controller.getSnapshot().target?.conversation_id).toBe("other");
  expect(controller.getSnapshot().historyCursor).toBe("new-cursor");
});
it("authoritative snapshot tools replace stale same-page events", async () => {
  const active_run = {
    conversation_id: "c",
    branch_id: "b",
    run_id: "r",
    status: "running" as const,
  };
  const message = {
    message_id: "live",
    conversation_id: "c",
    branch_id: "b",
    run_id: "r",
    role: "assistant" as const,
    content: "正文",
    thinking: "",
    tools: [],
    status: "streaming" as const,
  };
  vi.mocked(api.state).mockResolvedValue({
    server_epoch: "e",
    revision: 2,
    seq: 0,
    active_run,
    messages: [message],
  });
  await controller.sync();
  emit({
    protocol_version: 1,
    server_epoch: "e",
    seq: 1,
    kind: "tool.updated",
    conversation_id: "c",
    branch_id: "b",
    run_id: "r",
    message_id: "live",
    payload: { tool_id: "t", name: "工具", status: "waiting", summary: "" },
  });
  vi.mocked(api.state).mockResolvedValue({
    server_epoch: "e",
    revision: 2,
    seq: 1,
    active_run,
    messages: [message],
  });
  await controller.sync();
  expect(controller.getSnapshot().chat.messages[0].tools).toEqual([]);
});

it.each([true, false])(
  "enters password gate before any chat reads on secure=%s",
  async (secure) => {
    vi.mocked(api.session).mockResolvedValue({
      ...session,
      password_required: true,
      transport_secure: secure,
    });
    for (const method of [
      api.state,
      api.models,
      api.conversations,
      api.messages,
    ])
      vi.mocked(method).mockClear();
    await controller.initialize();
    expect(controller.getSnapshot().phase).toBe("password");
    expect(controller.getSnapshot().chat.messages).toEqual([]);
    await controller.sync();
    await controller.select(target);
    await controller.moreHistory();
    await controller.moreConversations();
    const command = vi.spyOn(api, "command");
    await controller.newSession();
    for (const method of [
      api.state,
      api.models,
      api.conversations,
      api.messages,
      command,
    ])
      expect(method).not.toHaveBeenCalled();
  },
);
it("HTTP cannot request a challenge without risk acceptance", async () => {
  vi.mocked(api.session).mockResolvedValue({
    ...session,
    password_required: true,
    transport_secure: false,
  });
  await controller.initialize();
  const challenge = vi.spyOn(api, "passwordChallenge");
  await controller.verifyPassword("secret");
  expect(challenge).not.toHaveBeenCalled();
  expect(controller.getSnapshot().error).toContain("HTTP");
});
it("verify POST alone cannot unlock chat; session must confirm verification", async () => {
  vi.mocked(api.session).mockResolvedValue({
    ...session,
    password_required: true,
  });
  await controller.initialize();
  vi.spyOn(api, "passwordChallenge").mockResolvedValue({
    challenge_id: "c",
    public_key: "pem",
    expires_in: 120,
  });
  vi.spyOn(api, "verifyPassword").mockResolvedValue({ ok: true });
  vi.mocked(api.state).mockClear();
  await controller.verifyPassword("secret");
  expect(controller.getSnapshot().phase).toBe("password");
  expect(api.state).not.toHaveBeenCalled();
  expect(controller.getSnapshot().busy).toBe(false);
});
it("verification refreshes session then initializes chat, never persists a password", async () => {
  vi.mocked(api.session)
    .mockResolvedValueOnce({ ...session, password_required: true })
    .mockResolvedValue(session);
  await controller.initialize();
  vi.spyOn(api, "passwordChallenge").mockResolvedValue({
    challenge_id: "c",
    public_key: "pem",
    expires_in: 120,
  });
  const verify = vi
    .spyOn(api, "verifyPassword")
    .mockResolvedValue({ ok: true });
  await controller.verifyPassword("secret");
  expect(verify).toHaveBeenCalledWith({
    challenge_id: "c",
    encrypted_key: "wrapped",
    iv: "nonce",
    ciphertext: "sealed",
  });
  expect(controller.getSnapshot().phase).toBe("ready");
  expect(JSON.stringify(controller.getSnapshot())).not.toContain("secret");
  expect(localStorage.length).toBe(0);
  expect(sessionStorage.length).toBe(0);
});
it("wrong-password 401 stays gated; next submission gets a new challenge", async () => {
  vi.mocked(api.session).mockResolvedValue({
    ...session,
    password_required: true,
  });
  await controller.initialize();
  const challenge = vi
    .spyOn(api, "passwordChallenge")
    .mockResolvedValue({
      challenge_id: "c",
      public_key: "pem",
      expires_in: 120,
    });
  vi.spyOn(api, "verifyPassword").mockRejectedValue(
    new ApiError(401, "vault_password_failed", "secret"),
  );
  await controller.verifyPassword("secret");
  expect(controller.getSnapshot().phase).toBe("password");
  expect(controller.getSnapshot().error).not.toContain("secret");
  await controller.verifyPassword("secret");
  expect(challenge).toHaveBeenCalledTimes(2);
});
it("revoked pairing after password failure returns to pairing", async () => {
  vi.mocked(api.session)
    .mockResolvedValueOnce({ ...session, password_required: true })
    .mockResolvedValue({ authenticated: false, csrf_token: "csrf" });
  await controller.initialize();
  vi.spyOn(api, "passwordChallenge").mockResolvedValue({
    challenge_id: "c",
    public_key: "pem",
    expires_in: 120,
  });
  vi.spyOn(api, "verifyPassword").mockRejectedValue(
    new ApiError(401, "vault_password_failed", "secret"),
  );
  await controller.verifyPassword("secret");
  expect(controller.getSnapshot().phase).toBe("pairing");
});
