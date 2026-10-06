import { BrandLogo } from "./components/BrandLogo";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";
import { ChatController, draftKey } from "./state/controller";
import { inTarget, mergeMessages } from "./state/chat";
import { Composer } from "./components/Composer";
import { PasswordPage } from "./components/PasswordPage";
import { PairingPage } from "./components/PairingPage";
import { SessionDrawer } from "./components/SessionDrawer";
// The pairing/password/welcome screen needs no Markdown parser or chat rows.
const MessageList = lazy(() =>
  import("./components/MessageList").then((module) => ({
    default: module.MessageList,
  })),
);

export function App({
  controller,
  ticket,
}: {
  controller: ChatController;
  ticket: string | null;
}) {
  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
  );
  const [drawer, setDrawer] = useState(false);
  const moreHistory = useCallback(() => controller.moreHistory(), [controller]);
  useEffect(() => {
    void controller.initialize(ticket);
    const resume = () => {
      if (document.visibilityState === "visible") void controller.sync();
    };
    const online = () => void controller.sync();
    document.addEventListener("visibilitychange", resume);
    window.addEventListener("online", online);
    return () => {
      controller.stop();
      document.removeEventListener("visibilitychange", resume);
      window.removeEventListener("online", online);
    };
  }, [controller, ticket]);
  const messages = useMemo(
    () =>
      mergeMessages(
        state.history,
        state.chat.messages.filter((m) => inTarget(m, state.target)),
      ),
    [state.history, state.chat.messages, state.target],
  );
  const locked =
    state.busy ||
    !!state.pending ||
    state.connection !== "online" ||
    !state.session?.capabilities.chat;
  const title =
    state.conversations.find(
      (c) => c.conversation_id === state.target?.conversation_id,
    )?.title || "新的可能，从这里开始";
  return (
    <>
      {state.error && (
        <div className="notice" role="alert">
          <span>{state.error}</span>
          <button
            aria-label="关闭提示"
            onClick={() => controller.dismissError()}
          >
            ×
          </button>
        </div>
      )}
      {state.phase === "loading" ? (
        <main className="loading">
          <BrandLogo variant="panel" />
          <p>正在建立安全连接…</p>
        </main>
      ) : state.phase === "pairing" ? (
        <PairingPage
          busy={state.busy}
          phrase={state.phrase}
          onPair={(code) => void controller.pair({ code })}
          onRetry={() => void controller.initialize()}
        />
      ) : state.phase === "password" ? (
        <PasswordPage
          busy={state.busy}
          transportSecure={!!state.session?.transport_secure}
          onVerify={(password, accepted) =>
            void controller.verifyPassword(password, accepted)
          }
          onDisconnect={() => void controller.logout()}
        />
      ) : (
        <div className="app-shell">
          <header className="topbar">
            <button
              className="icon-button"
              aria-label="打开会话列表"
              onClick={() => setDrawer(true)}
            >
              ☰
            </button>
            <div className="brand">
              <BrandLogo />
              <small>你的想法，不止于此</small>
            </div>
            <button
              className="icon-button"
              aria-label="新建会话"
              disabled={locked || !!state.chat.active_run}
              onClick={() => void controller.newSession()}
            >
              ＋
            </button>
          </header>
          <div className="conversation-heading">
            <h1>{title}</h1>
            <button
              className={`connection ${state.connection}`}
              onClick={() => void controller.sync()}
            >
              <i />
              {
                { online: "已连接", offline: "重新连接", connecting: "连接中" }[
                  state.connection
                ]
              }
            </button>
          </div>
          {state.pending && (
            <div className="pending" role="status">
              正在确认命令结果
              <button
                disabled={state.busy}
                onClick={() => void controller.checkPending()}
              >
                查询回执
              </button>
            </div>
          )}
          {state.target ? (
            <Suspense
              fallback={
                <div className="message-region">
                  <p className="muted" role="status">
                    正在准备聊天记录…
                  </p>
                </div>
              }
            >
              <MessageList
                key={draftKey(state.target)}
                messages={messages}
                hasMore={!!state.historyCursor}
                loading={state.loadingHistory}
                onMore={moreHistory}
              />
            </Suspense>
          ) : (
            <main className="welcome">
              <BrandLogo variant="hero" />
              <span className="eyebrow">A LITTLE ROOM FOR BIG IDEAS</span>
              <h2>
                把思绪，<em>轻轻展开。</em>
              </h2>
              <p>
                灵感、疑问，或一个尚未成形的想法。
                <br />
                这里是你与电脑共享的安静角落。
              </p>
              <button
                className="primary"
                disabled={locked || !!state.chat.active_run || !state.model}
                onClick={() => void controller.newSession()}
              >
                开启新对话 ↗
              </button>
            </main>
          )}
          <Composer
            draft={state.drafts[draftKey(state.target)] ?? ""}
            onDraft={(text) => controller.setDraft(text)}
            models={state.models}
            model={state.model}
            disabled={locked || !state.target}
            busy={locked}
            running={!!state.chat.active_run}
            onSend={() => void controller.send()}
            onAbort={() => void controller.abort()}
            onModel={(model) => void controller.selectModel(model)}
          />
          <footer className="app-footer">
            <span>
              {state.session?.capabilities.remote_agent
                ? "工具操作仍受电脑端权限限制"
                : "远程工具默认关闭"}
            </span>
            <button onClick={() => void controller.logout()}>断开此设备</button>
          </footer>
          <SessionDrawer
            open={drawer}
            conversations={state.conversations}
            target={state.target}
            hasMore={!!state.conversationCursor}
            onMore={() => void controller.moreConversations()}
            onSelect={(c) => void controller.select(c)}
            onClose={() => setDrawer(false)}
            onNew={() => void controller.newSession()}
            disabled={locked || !!state.chat.active_run}
          />
        </div>
      )}
    </>
  );
}
