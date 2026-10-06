import { BrandLogo } from "./BrandLogo";
import { useState } from "react";
export function PasswordPage({
  busy,
  transportSecure,
  onVerify,
  onDisconnect,
}: {
  busy: boolean;
  transportSecure: boolean;
  onVerify: (password: string, acceptedHttpRisk: boolean) => void;
  onDisconnect: () => void;
}) {
  const [password, setPassword] = useState("");
  const [accepted, setAccepted] = useState(false);
  return (
    <main className="pairing">
      <div className="brand">
        <BrandLogo variant="panel" />
      </div>
      <section className="pair-card">
        <span className="eyebrow">VERIFY YOUR VAULT</span>
        <h1>核验资料库密码</h1>
        <p>
          设备已配对，但尚未获得聊天访问权限。HTTPS 连接也必须核验资料库密码。
        </p>
        {!transportSecure && (
          <div className="transport-risk" role="alert">
            <strong>风险：当前使用 HTTP，无证书保护</strong>
            <p>
              HTTP
              不加密聊天，聊天内容可被窃听或篡改。加密密码信封不防主动攻击，攻击者可替换网页或公钥并窃取密码。推荐改用可信
              HTTPS。
            </p>
          </div>
        )}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (!password || busy || (!transportSecure && !accepted)) return;
            const submitted = password;
            setPassword("");
            onVerify(submitted, accepted);
          }}
        >
          <label htmlFor="vault-password">资料库密码</label>
          <input
            id="vault-password"
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={busy}
            required
          />
          {!transportSecure && (
            <label className="risk-confirm">
              <input
                type="checkbox"
                checked={accepted}
                disabled={busy}
                onChange={(event) => setAccepted(event.target.checked)}
              />
              我已了解 HTTP 明文与主动攻击风险，仍要继续
            </label>
          )}
          <button
            className="primary"
            disabled={busy || !password || (!transportSecure && !accepted)}
          >
            {busy ? "正在核验…" : "核验密码 ↗"}
          </button>
        </form>
        <button className="text-button" disabled={busy} onClick={onDisconnect}>
          断开此设备
        </button>
      </section>
      <p className="pair-foot">
        密码仅用于本次核验，提交后清空，不保存至本地存储。
      </p>
    </main>
  );
}
