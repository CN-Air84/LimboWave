import { test, expect } from "@playwright/test";
import { startBackend, stopBackend } from "./backend";
let backend: Awaited<ReturnType<typeof startBackend>>;
test.beforeAll(async () => {
  backend = await startBackend();
});
test.afterAll(async () => {
  await stopBackend(backend);
});
test("real API: pair, new session, SSE, history, stop and 320px safety", async ({
  page,
  context,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const remote: string[] = [];
  page.on("request", (request) => {
    if (!request.url().startsWith("http://127.0.0.1:8877"))
      remote.push(request.url());
  });
  await page.goto(`/#ticket=${backend.ticket}`);
  await expect(page).toHaveURL("http://127.0.0.1:8877/");
  await page
    .getByLabel("资料库密码", { exact: true })
    .fill("test-vault-password");
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "核验密码 ↗" }).click();
  await expect(
    page.getByRole("button", { name: "开启新对话 ↗" }),
  ).toBeEnabled();
  await expect(page.locator(".topbar .brand-logo")).toBeVisible();
  await expect(page.locator(".topbar .brand")).not.toContainText("LimboWave");
  const cookies = await context.cookies();
  expect(cookies.find((c) => c.name === "lw_session")).toMatchObject({
    httpOnly: true,
    sameSite: "Strict",
  });
  await page.getByRole("button", { name: "开启新对话 ↗" }).click();
  await expect(page.getByRole("button", { name: "发送消息" })).toBeVisible();
  await page.getByRole("textbox", { name: "消息" }).fill("hello");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(
    page.getByText("Hello from LimboWave.", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "停止生成" })).toHaveCount(0);
  // Exercise actual stable cursor pagination, not only the UI fixture's mock page.
  const conversationsResponse = await context.request.get(
    "/api/v1/conversations?limit=50",
  );
  expect(conversationsResponse.status()).toBe(200);
  const conversation = (await conversationsResponse.json()).items[0];
  const historyPath =
    "/api/v1/conversations/" +
    conversation.id +
    "/messages?branch_id=" +
    conversation.branch_id +
    "&limit=1";
  const latestResponse = await context.request.get(historyPath);
  expect(latestResponse.status()).toBe(200);
  const latest = await latestResponse.json();
  expect(latest.items[0].role).toBe("assistant");
  expect(latest.next_cursor).toBeTruthy();
  const olderResponse = await context.request.get(
    historyPath + "&cursor=" + encodeURIComponent(latest.next_cursor),
  );
  expect(olderResponse.status()).toBe(200);
  expect((await olderResponse.json()).items[0].content).toBe("hello");
  await page.reload();
  await page.getByRole("button", { name: "打开会话列表" }).click();
  await page
    .getByRole("navigation", { name: "会话列表" })
    .getByRole("button")
    .first()
    .click();
  await expect(
    page.getByText("Hello from LimboWave.", { exact: true }),
  ).toBeVisible();
  await page.getByRole("textbox", { name: "消息" }).fill("slow");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByRole("button", { name: "停止生成" })).toBeEnabled();
  await expect
    .poll(async () => {
      const response = await context.request.get("/api/v1/state");
      return (await response.json()).stream?.tools?.[0]?.status;
    })
    .toBe("completed");
  await expect(page.locator(".tools").last()).toBeVisible();
  // Reload while still generating: metadata must come from state.stream, not old SSE memory.
  await page.reload();
  await expect(page.locator(".tools").last()).toBeVisible();
  await page.locator(".tools").last().locator("summary").click();
  await expect(page.locator(".tools").last()).toContainText("Fixture lookup");
  await expect(page.locator(".tools").last()).toContainText("已完成");
  await context.setOffline(true);
  await page.waitForTimeout(300);
  await context.setOffline(false);
  await expect(page.getByRole("button", { name: "停止生成" })).toBeEnabled();
  await page.getByRole("button", { name: "停止生成" }).click();
  await expect(page.getByRole("button", { name: "停止生成" })).toHaveCount(0);
  await expect
    .poll(async () => {
      const response = await context.request.get(historyPath);
      return (await response.json()).items[0]?.status;
    })
    .toBe("partial");
  await page.reload();
  await page.getByRole("button", { name: "打开会话列表" }).click();
  await page
    .getByRole("navigation", { name: "会话列表" })
    .getByRole("button")
    .first()
    .click();
  await expect(page.getByText("已停止生成", { exact: true })).toBeVisible();
  await page.locator(".tools").last().locator("summary").click();
  await expect(page.locator(".tools").last()).toContainText("Fixture lookup");
  await expect(page.locator(".tools").last()).toContainText("已完成");
  const restoredResponse = await context.request.get(historyPath);
  const restored = (await restoredResponse.json()).items[0];
  expect(restored.run_id).toBeTruthy();
  expect(restored.tools[0]).toEqual({
    tool_id: "fixture-tool",
    name: "Fixture lookup",
    status: "completed",
  });
  for (const width of [320, 390, 430, 844]) {
    await page.setViewportSize({ width, height: width === 844 ? 390 : 740 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  }
  expect(
    await page.evaluate(() => ({
      local: localStorage.length,
      session: sessionStorage.length,
    })),
  ).toEqual({ local: 0, session: 0 });
  expect(remote).toEqual([]);
  expect(errors).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "test-results/mobile-chat.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "断开此设备" }).click();
  await expect(page.getByLabel("一次性配对码")).toBeVisible();
});
