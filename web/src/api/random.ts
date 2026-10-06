export function secureRandomBytes(count: number): string {
  if (!globalThis.crypto?.getRandomValues)
    throw new Error(
      "浏览器缺少安全随机源，请改用支持安全随机数的浏览器或 HTTPS。",
    );
  const bytes = new Uint8Array(count);
  // getRandomValues is available on HTTP; never use subtle or an entropy fallback.
  for (let offset = 0; offset < count; offset += 65536)
    globalThis.crypto.getRandomValues(bytes.subarray(offset, offset + 65536));
  let result = "";
  for (const byte of bytes) result += String.fromCharCode(byte);
  bytes.fill(0);
  return result;
}

export function secureCommandId(): string {
  const raw = secureRandomBytes(16);
  const bytes = Array.from(raw, (char) => char.charCodeAt(0));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.map((byte) => byte.toString(16).padStart(2, "0")).join("");
  bytes.fill(0);
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
