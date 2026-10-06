import { test, expect } from "@playwright/test";
import { startBackend, stopBackend } from "./backend";
let backend: Awaited<ReturnType<typeof startBackend>>;
test.beforeEach(async () => {
  backend = await startBackend();
});
test.afterEach(async () => {
  await stopBackend(backend);
});
test("HTTP: gates every resource, wrong password retries, no subtle/randomUUID, real forge/Python interoperability", async ({
  page,
  context,
}) => {
  await page.addInitScript(() => {
    Object.defineProperty(crypto, "subtle", {
      get: () => {
        throw new Error("WebCrypto unavailable on LAN HTTP");
      },
    });
    Object.defineProperty(crypto, "randomUUID", { value: undefined });
  });
  const protectedReads: string[] = [];
  const secretRequests: string[] = [];
  const protectedPath =
    /\/api\/v1\/(state|models|events|conversations|commands)(\/|\?|$)/;
  page.on("request", (request) => {
    if (protectedPath.test(request.url())) protectedReads.push(request.url());
    if (
      request.postData()?.includes("test-vault-password") ||
      request.postData()?.includes("wrong-vault-password")
    )
      secretRequests.push(request.url());
  });
  await page.goto(`/#ticket=${backend.ticket}`);
  const password = page.getByLabel("资料库密码", { exact: true });
  await expect(password).toBeVisible();
  const logo = page.getByRole("img", { name: "LimboWave", exact: true });
  await expect(logo).toBeVisible();
  await expect
    .poll(() =>
      logo.evaluate((node) => {
        const image = node as HTMLImageElement;
        return (
          image.complete &&
          image.naturalWidth === 1161 &&
          image.naturalHeight === 710
        );
      }),
    )
    .toBe(true);
  await expect(page.locator(".brand")).not.toContainText("LimboWave");

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(password).toHaveValue("");
  await page.screenshot({
    path: "test-results/mobile-password.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 320, height: 740 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(protectedReads).toEqual([]);
  const before = await context.request.get("/api/v1/session");
  const session = await before.json();
  expect(session).toMatchObject({
    password_required: true,
    transport_secure: false,
  });
  for (const path of [
    "state",
    "models",
    "events",
    "conversations",
    "conversations/any/messages?branch_id=any",
    "commands/any",
  ]) {
    expect((await context.request.get(`/api/v1/${path}`)).status()).toBe(403);
  }
  expect(
    (
      await context.request.post("/api/v1/commands", {
        headers: { "X-CSRF-Token": session.csrf_token, Origin: backend.url },
        data: {
          type: "new_session",
          client_command_id: "preauth",
          server_epoch: session.server_epoch,
          expected_revision: 0,
        },
      })
    ).status(),
  ).toBe(403);
  await password.fill("wrong-vault-password");
  await expect(page.getByRole("button", { name: "核验密码 ↗" })).toBeDisabled();
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "核验密码 ↗" }).click();
  await expect(password).toHaveValue("");
  await expect(page.getByRole("alert").first()).toContainText("密码不正确");
  expect(protectedReads).toEqual([]);
  // Capture only the encrypted payload so we can prove a consumed challenge cannot replay.
  let envelope: unknown;
  page.on("request", (request) => {
    if (request.url().endsWith("/password/verify"))
      envelope = request.postDataJSON();
  });
  await password.fill("test-vault-password");
  await page.getByRole("button", { name: "核验密码 ↗" }).click();
  await expect(
    page.getByRole("button", { name: "开启新对话 ↗" }),
  ).toBeEnabled();
  expect(
    (await (await context.request.get("/api/v1/session")).json())
      .password_required,
  ).toBe(false);
  expect(
    (
      await context.request.post("/api/v1/password/verify", {
        headers: { "X-CSRF-Token": session.csrf_token, Origin: backend.url },
        data: envelope,
      })
    ).status(),
  ).toBe(401);
  await page.getByRole("button", { name: "开启新对话 ↗" }).click();
  await expect(page.getByRole("textbox", { name: "消息" })).toBeVisible();
  expect(secretRequests).toEqual([]);
  expect(
    await page.evaluate(() => ({
      local: localStorage.length,
      session: sessionStorage.length,
    })),
  ).toEqual({ local: 0, session: 0 });
});
test("real gate revokes pairing after five wrong password attempts", async ({
  page,
  context,
}) => {
  await page.goto(`/#ticket=${backend.ticket}`);
  for (let attempt = 0; attempt < 5; attempt++) {
    await page
      .getByLabel("资料库密码", { exact: true })
      .fill("wrong-vault-password");
    await page.getByRole("checkbox").check();
    await page.getByRole("button", { name: "核验密码 ↗" }).click();
    if (attempt < 4) {
      await expect(page.getByRole("button", { name: "核验密码 ↗" })).toHaveText(
        "核验密码 ↗",
      );
      await expect(page.getByRole("alert").first()).toContainText("密码不正确");
    }
  }
  await expect(page.getByLabel("一次性配对码")).toBeVisible();
  expect((await context.request.get("/api/v1/session")).status()).toBe(401);
});
test("missing crypto.getRandomValues fails closed without a verify POST or chat read", async ({
  page,
}) => {
  await page.addInitScript(() => {
    Object.defineProperty(crypto, "getRandomValues", { value: undefined });
  });
  const reads: string[] = [];
  page.on("request", (request) => {
    if (
      /\/api\/v1\/(password\/verify|state|models|events|conversations|commands)/.test(
        request.url(),
      )
    )
      reads.push(request.url());
  });
  await page.goto(`/#ticket=${backend.ticket}`);
  await page
    .getByLabel("资料库密码", { exact: true })
    .fill("test-vault-password");
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "核验密码 ↗" }).click();
  await expect(page.getByRole("alert").first()).toContainText("密码核验失败");
  await expect(page.getByLabel("资料库密码", { exact: true })).toHaveValue("");
  expect(reads).toEqual([]);
});
