// @vitest-environment node
import { expect, it, vi, afterEach } from "vitest";
import { secureCommandId } from "./random";
afterEach(() => vi.unstubAllGlobals());
it("builds a secure UUIDv4 without randomUUID or Math.random on HTTP", () => {
  vi.stubGlobal("crypto", {
    getRandomValues: crypto.getRandomValues.bind(crypto),
  });
  const weak = vi.spyOn(Math, "random").mockImplementation(() => {
    throw new Error("weak");
  });
  expect(secureCommandId()).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
  expect(secureCommandId()).not.toBe(secureCommandId());
  expect(weak).not.toHaveBeenCalled();
});
it("cannot create a command id without secure randomness", () => {
  vi.stubGlobal("crypto", undefined);
  expect(secureCommandId).toThrow("安全随机源");
});
