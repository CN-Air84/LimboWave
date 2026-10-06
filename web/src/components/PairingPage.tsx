import { BrandLogo } from "./BrandLogo";
import { useState } from "react";
export function PairingPage({
  busy,
  phrase,
  onPair,
  onRetry,
}: {
  busy: boolean;
  phrase: string;
  onPair: (code: string) => void;
  onRetry: () => void;
}) {
  const [code, setCode] = useState("");
  return (
    <main className="pairing">
      <div className="brand">
        <BrandLogo variant="panel" />
      </div>
      <section className="pair-card">
        <span className="eyebrow">YOUR SPACE, WITHIN REACH</span>
        <h1>
          换个屏幕，
          <br />
          接着聊。
        </h1>
        <p>连接已解锁的电脑，让思绪在同一片空间里流动。</p>
        <div className="pair-divider" />
        <h2>安全连接你的电脑</h2>
        <p>在电脑端开启「局域网访问」，扫描二维码，或输入 8 位配对码。</p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (/^\d{8}$/.test(code)) {
              onPair(code);
              setCode("");
            }
          }}
        >
          <label htmlFor="pair-code">一次性配对码</label>
          <input
            id="pair-code"
            inputMode="numeric"
            autoComplete="off"
            pattern="[0-9]{8}"
            maxLength={8}
            value={code}
            onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))}
            placeholder="0000 0000"
            disabled={busy || !!phrase}
          />
          <button
            className="primary"
            disabled={busy || !!phrase || code.length !== 8}
          >
            {busy ? "正在连接…" : "配对设备 ↗"}
          </button>
        </form>
        {phrase && (
          <div role="status" className="pair-phrase">
            等待电脑确认
            <br />
            <strong>{phrase}</strong>
            <p>请核对电脑上显示的校验短语。</p>
          </div>
        )}
        <button className="text-button" onClick={onRetry}>
          已经配对？重新连接
        </button>
      </section>
      <p className="pair-foot">
        推荐可信 HTTPS 与同一局域网；HTTP 有明文与篡改风险
        <br />
        配对仅预授权，仍需核验资料库密码；不在本地存储聊天记录。
      </p>
    </main>
  );
}
