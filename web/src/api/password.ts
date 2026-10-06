import forge from "node-forge";
import type { PasswordChallenge, PasswordEnvelope } from "./dto";

import { secureRandomBytes } from "./random";

export function encryptPassword(
  password: string,
  challenge: PasswordChallenge,
): PasswordEnvelope {
  // Override forge's random entry points AND its seed sources. No Math.random fallback.
  forge.random.getBytesSync = secureRandomBytes;
  forge.random.getBytes = ((
    count: number,
    callback?: (error: Error | null, bytes?: string) => void,
  ) => {
    if (!callback) return secureRandomBytes(count);
    try {
      callback(null, secureRandomBytes(count));
    } catch (error) {
      callback(error as Error);
    }
  }) as typeof forge.random.getBytes;
  const random = forge.random as typeof forge.random & {
    seedFileSync: (count: number) => string;
    seedFile: (
      count: number,
      callback: (error: Error | null, bytes: string) => void,
    ) => void;
  };
  random.seedFileSync = secureRandomBytes;
  random.seedFile = (count, callback) => {
    try {
      callback(null, secureRandomBytes(count));
    } catch (error) {
      callback(error as Error, "");
    }
  };
  let key = secureRandomBytes(32);
  try {
    const iv = secureRandomBytes(12);
    const publicKey = forge.pki.publicKeyFromPem(challenge.public_key);
    if (publicKey.n.bitLength() !== 2048)
      throw new Error("不支持的密码核验公钥");
    const encryptedKey = publicKey.encrypt(key, "RSA-OAEP", {
      md: forge.md.sha256.create(),
      mgf1: { md: forge.md.sha256.create() },
      seed: secureRandomBytes(32),
    });
    const cipher = forge.cipher.createCipher("AES-GCM", key);
    cipher.start({
      iv,
      additionalData: forge.util.encodeUtf8(challenge.challenge_id),
      tagLength: 128,
    });
    cipher.update(forge.util.createBuffer(forge.util.encodeUtf8(password)));
    password = "";
    if (!cipher.finish()) throw new Error("无法生成密码信封");
    return {
      challenge_id: challenge.challenge_id,
      encrypted_key: forge.util.encode64(encryptedKey),
      iv: forge.util.encode64(iv),
      ciphertext: forge.util.encode64(
        cipher.output.getBytes() + cipher.mode.tag.getBytes(),
      ),
    };
  } finally {
    // JS immutable strings cannot be securely erased; never persist them.
    key = "";
    password = "";
  }
}
