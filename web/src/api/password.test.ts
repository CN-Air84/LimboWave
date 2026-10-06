// @vitest-environment node
import { afterEach, expect, it, vi } from "vitest";
import {
  constants,
  generateKeyPairSync,
  privateDecrypt,
  createDecipheriv,
} from "node:crypto";
import { encryptPassword } from "./password";
const { publicKey, privateKey } = generateKeyPairSync("rsa", {
  modulusLength: 2048,
});
export const challenge = {
  challenge_id: "challenge-核验",
  public_key: publicKey.export({ type: "spki", format: "pem" }).toString(),
  expires_in: 120 as const,
};
afterEach(() => vi.unstubAllGlobals());
it("decrypts forge envelopes with independent Node RSA OAEP SHA256 + AESGCM and UTF8 AAD", () => {
  const subtle = vi.spyOn(crypto, "subtle", "get").mockImplementation(() => {
    throw new Error("No subtle on HTTP");
  });
  const weakRandom = vi.spyOn(Math, "random").mockImplementation(() => {
    throw new Error("No weak entropy");
  });
  const envelope = encryptPassword("密码🔒 test-vault-password", challenge);
  const key = privateDecrypt(
    {
      key: privateKey,
      padding: constants.RSA_PKCS1_OAEP_PADDING,
      oaepHash: "sha256",
    },
    Buffer.from(envelope.encrypted_key, "base64"),
  );
  expect(key.length).toBe(32);
  const iv = Buffer.from(envelope.iv, "base64");
  expect(iv.length).toBe(12);
  const sealed = Buffer.from(envelope.ciphertext, "base64");
  const cipher = createDecipheriv("aes-256-gcm", key, iv);
  cipher.setAAD(Buffer.from(challenge.challenge_id, "utf8"));
  cipher.setAuthTag(sealed.subarray(-16));
  expect(
    Buffer.concat([
      cipher.update(sealed.subarray(0, -16)),
      cipher.final(),
    ]).toString("utf8"),
  ).toBe("密码🔒 test-vault-password");
  expect(subtle).not.toHaveBeenCalled();
  expect(weakRandom).not.toHaveBeenCalled();
});
it("fails closed without getRandomValues", () => {
  vi.stubGlobal("crypto", undefined);
  expect(() => encryptPassword("secret", challenge)).toThrow("安全随机源");
});
it("uses fresh key, iv and OAEP seed on every attempt", () => {
  expect(encryptPassword("secret", challenge)).not.toEqual(
    encryptPassword("secret", challenge),
  );
});
