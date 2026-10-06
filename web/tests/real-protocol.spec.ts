import { test, expect } from "@playwright/test";
import { startBackend, stopBackend } from "./backend";
let backend: Awaited<ReturnType<typeof startBackend>>;
test.beforeAll(async () => {
  backend = await startBackend();
});
test.afterAll(async () => {
  await stopBackend(backend);
});
test("real WebServer protocol: cookie pair/new/send/SSE replay/stop without mocked routes", async ({
  page,
  context,
}) => {
  await page.goto(`/#ticket=${backend.ticket}`);
  await expect(page.getByLabel("资料库密码", { exact: true })).toBeVisible();
  for (const path of [
    "state",
    "models",
    "events",
    "conversations",
    "commands/no-receipt",
  ]) {
    expect((await context.request.get(`/api/v1/${path}`)).status()).toBe(403);
  }
  await page
    .getByLabel("资料库密码", { exact: true })
    .fill("test-vault-password");
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "核验密码 ↗" }).click();
  await expect(
    page.getByRole("button", { name: "开启新对话 ↗" }),
  ).toBeEnabled();
  const result = await page.evaluate(async (ticket) => {
    const read = async (path: string) => {
      const r = await fetch(`/api/v1/${path}`, { cache: "no-store" });
      if (!r.ok) throw new Error(`${path}: ${r.status}`);
      return r.json();
    };
    const bootstrap = await read("pair/status");
    const post = async (path: string, body: unknown, csrf: string) => {
      const response = await fetch(`/api/v1/${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(`${path}: ${response.status}`);
      return response.json();
    };
    const pair = { status: "paired" }; // Already paired and password-verified by the real UI.
    const session = await read("session");
    const state = await read("state");
    const draft = await post(
      "commands",
      {
        type: "new_session",
        client_command_id: crypto.randomUUID(),
        server_epoch: session.server_epoch,
        expected_revision: state.revision,
      },
      session.csrf_token,
    );
    const before = await read("state");
    const command = {
      type: "send",
      client_command_id: crypto.randomUUID(),
      server_epoch: session.server_epoch,
      expected_revision: before.revision,
      conversation_id: draft.conversation_id,
      branch_id: draft.branch_id,
      model_id: "fake",
      text: "slow",
    };
    const sent = await post("commands", command, session.csrf_token);
    const observe = async (seq: number) => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 8000);
      const response = await fetch("/api/v1/events", {
        headers: { "Last-Event-ID": `${session.server_epoch}:${seq}` },
        signal: controller.signal,
      });
      if (!response.ok || !response.body) throw new Error("SSE failed");
      const reader = response.body.getReader(),
        decoder = new TextDecoder();
      let buffer = "";
      try {
        while (true) {
          const { value, done } = await reader.read();
          if (done) throw new Error("SSE ended without delta");
          buffer += decoder.decode(value, { stream: true });
          let end: number;
          while ((end = buffer.indexOf("\n\n")) >= 0) {
            const frame = buffer.slice(0, end);
            buffer = buffer.slice(end + 2);
            const data = frame
              .split("\n")
              .find((line) => line.startsWith("data: "));
            if (!data) continue;
            const event = JSON.parse(data.slice(6));
            if (event.kind === "assistant_delta") return event;
          }
        }
      } finally {
        clearTimeout(timer);
        controller.abort();
        await reader.cancel().catch(() => {});
      }
    };
    const first = await observe(before.seq);
    // Explicitly disconnect and resume from the last delivered sequence; no POST replay.
    const resumed = await observe(first.seq);
    const active = await read("state");
    const stopped = await post(
      "commands",
      {
        type: "abort",
        client_command_id: crypto.randomUUID(),
        server_epoch: session.server_epoch,
        expected_revision: active.revision,
        run_id: sent.run_id,
      },
      session.csrf_token,
    );
    const receipt = await read(`commands/${command.client_command_id}`);
    return {
      paired: pair.status,
      draft,
      sent,
      first,
      resumed,
      stopped,
      receipt,
      final: await read("state"),
    };
  }, backend.ticket);
  expect(result.paired).toBe("paired");
  expect(result.draft.status).toBe("accepted");
  expect(result.sent.status).toBe("accepted");
  expect(result.first.payload.text).toBeTruthy();
  expect(result.resumed.seq).toBeGreaterThan(result.first.seq);
  expect(result.stopped.status).toBe("accepted");
  expect(result.final.busy).toBe(false);
  expect(result.receipt.run_id).toBe(result.sent.run_id);
  expect(
    (await context.cookies()).find((c) => c.name === "lw_session"),
  ).toMatchObject({ httpOnly: true, sameSite: "Strict" });
});

test("real short-code pairing waits for desktop confirmation", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByLabel("一次性配对码").fill(backend.code);
  await page.getByRole("button", { name: "配对设备 ↗" }).click();
  await expect(page.getByRole("status")).toContainText("等待电脑确认");
  backend.process.stdin?.write("approve\n");
  await page
    .getByLabel("资料库密码", { exact: true })
    .fill("test-vault-password");
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "核验密码 ↗" }).click();
  await expect(
    page.getByRole("button", { name: "开启新对话 ↗" }),
  ).toBeEnabled();
});
