import { test, expect } from "@playwright/test";
import { startBackend, stopBackend } from "./backend";
let backend: Awaited<ReturnType<typeof startBackend>>;
test.beforeAll(async () => {
  backend = await startBackend();
});
test.afterAll(async () => {
  await stopBackend(backend);
});

test("pairing and password screen do not preload chat or crypto bundles", async ({
  page,
}) => {
  const scripts: string[] = [];
  page.on("request", (request) => {
    if (request.resourceType() === "script") scripts.push(request.url());
  });
  await page.goto("/");
  await expect(page.getByLabel("一次性配对码")).toBeVisible();
  expect(
    scripts.filter((url) => /markdown-|passwordCrypto-|MessageList-/.test(url)),
  ).toEqual([]);
  await page.goto(`/#ticket=${backend.ticket}`);
  await page.reload();
  await expect(page.getByLabel("资料库密码", { exact: true })).toBeVisible();
  expect(
    scripts.filter((url) => /markdown-|passwordCrypto-|MessageList-/.test(url)),
  ).toEqual([]);
  await page
    .getByLabel("资料库密码", { exact: true })
    .fill("test-vault-password");
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "核验密码 ↗" }).click();
  await expect(
    page.getByRole("button", { name: "开启新对话 ↗" }),
  ).toBeVisible();
  expect(scripts.some((url) => /passwordCrypto-/.test(url))).toBe(true);
  expect(scripts.filter((url) => /markdown-|MessageList-/.test(url))).toEqual(
    [],
  );
  await page.getByRole("button", { name: "开启新对话 ↗" }).click();
  await expect(page.getByLabel("聊天记录")).toBeVisible();
  expect(scripts.some((url) => /markdown-/.test(url))).toBe(true);
});
