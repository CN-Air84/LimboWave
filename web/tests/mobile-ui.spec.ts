import { test, expect } from "@playwright/test";
import { startBackend, stopBackend } from "./backend";
let backend: Awaited<ReturnType<typeof startBackend>>;
test.beforeAll(async () => {
  backend = await startBackend();
});
test.afterAll(async () => {
  await stopBackend(backend);
});
test("UI contract fixture: conflicts, history, markdown safety, mobile and IME", async ({
  page,
}) => {
  let revision = 1,
    conflict = true,
    selected = false;
  const conversations = [
    {
      id: "c",
      title: "让想法慢慢清晰",
      created_at: "2026-10-06T10:00:00Z",
      branches: [{ id: "b", title: "Main" }],
    },
  ];
  const history = [
    {
      id: "m",
      role: "assistant",
      content:
        '## 留一点空间给灵感\n\n你好，这里是 **LimboWave**。\n\n![远程图片](https://untrusted.invalid/pixel)\n\n```ts\nconst longLine = "' +
        "x".repeat(200) +
        '";\n```\n\n<script>alert(1)</script>',
      status: "completed",
      branch_id: "b",
      created_at: "2026-10-06",
    },
  ];
  let posts = 0;
  const remote: string[] = [];
  page.on("request", (request) => {
    if (request.url().startsWith("https://untrusted.invalid"))
      remote.push(request.url());
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(),
      url = new URL(request.url()),
      path = url.pathname;
    if (path.endsWith("/events")) return; // Hold the connection; this test is explicitly UI-only.
    const ok = (body: unknown, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    if (path.endsWith("/session"))
      return ok({
        server_epoch: "e",
        device_id: "test",
        csrf_token: "csrf",
        password_required: false,
        transport_secure: true,
        permissions: { chat: true, tools: false },
      });
    if (path.endsWith("/state"))
      return ok({
        server_epoch: "e",
        revision,
        seq: 0,
        available: true,
        busy: false,
        conversation_id: selected ? "c" : null,
        branch_id: selected ? "b" : null,
        run_id: null,
        stream: null,
      });
    if (path.endsWith("/models"))
      return ok([{ id: "model", name: "Limbo · 轻思考" }]);
    if (path.endsWith("/conversations"))
      return ok({ items: conversations, next_cursor: null });
    if (path.endsWith("/messages"))
      return ok({
        items: url.searchParams.has("cursor")
          ? [{ ...history[0], id: "old", content: "更早的灵感记录" }]
          : history,
        next_cursor: url.searchParams.has("cursor") ? null : "older",
      });
    if (path.endsWith("/commands")) {
      const command = request.postDataJSON();
      posts++;
      expect(request.headers()["x-csrf-token"]).toBe("csrf");
      if (command.type === "send" && conflict) {
        conflict = false;
        return ok(
          {
            code: "runtime_busy",
            message: "电脑正在生成，草稿已保留",
            request_id: "r",
            retryable: false,
          },
          409,
        );
      }
      selected = true;
      revision++;
      if (command.type === "send")
        history.push({
          ...history[0],
          id: "user",
          role: "user",
          content: command.text,
        });
      return ok({
        client_command_id: command.client_command_id,
        server_epoch: "e",
        status: "accepted",
        conversation_id: "c",
        branch_id: "b",
        revision,
        run_id: null,
      });
    }
    return ok({ ok: true });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "开启新对话 ↗" }).click();
  await expect(
    page.getByRole("heading", { name: "留一点空间给灵感" }),
  ).toBeVisible();
  await expect(page.locator(".prose img,.prose script")).toHaveCount(0);
  await page.getByRole("button", { name: "↑ 更早的消息" }).click();
  await expect(page.getByText("更早的灵感记录")).toBeVisible();
  const input = page.getByRole("textbox", { name: "消息" });
  await input.fill("未完成的想法");
  await input.dispatchEvent("compositionstart");
  await input.press("Enter");
  await input.dispatchEvent("compositionend");
  expect(posts).toBe(1);
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByRole("alert")).toContainText("草稿已保留");
  await expect(input).toHaveValue(/未完成的想法/);
  expect(posts).toBe(2);
  await page.getByRole("button", { name: "关闭提示" }).click();
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(input).toHaveValue("");
  expect(posts).toBe(3);
  await page.getByRole("button", { name: "打开会话列表" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).not.toBeVisible();
  for (const [width, height] of [
    [320, 740],
    [390, 844],
    [430, 850],
    [844, 390],
  ]) {
    await page.setViewportSize({ width, height });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const bounds = await input.boundingBox();
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
  }
  expect(remote).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "test-results/mobile-ui.png", fullPage: true });
});
