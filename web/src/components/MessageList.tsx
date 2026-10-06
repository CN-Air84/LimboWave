import { BrandLogo } from "./BrandLogo";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { memo, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Message } from "../api/dto";
function safeLink(href?: string) {
  if (!href) return undefined;
  try {
    const url = new URL(href, window.location.origin);
    return ["https:", "http:"].includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}
export const SafeMarkdown = memo(function SafeMarkdown({
  text,
}: {
  text: string;
}) {
  return (
    <Markdown
      remarkPlugins={[remarkGfm]}
      skipHtml
      components={{
        img: ({ alt }) => (
          <span className="blocked-image">
            [图片未加载{alt ? `：${alt}` : ""}]
          </span>
        ),
        a: ({ href, children }) =>
          safeLink(href) ? (
            <a
              href={safeLink(href)}
              target="_blank"
              rel="noopener noreferrer"
              referrerPolicy="no-referrer"
            >
              {children} ↗
            </a>
          ) : (
            <span>{children}</span>
          ),
      }}
    >
      {text}
    </Markdown>
  );
});
// Native details handles pointer and keyboard interaction; its hidden content
// must not parse an ever-growing reasoning stream in the background.
const ThinkingSection = memo(function ThinkingSection({
  text,
}: {
  text: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="thinking"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        思考过程 <span>展开查看</span>
      </summary>
      {open && (
        <div>
          <SafeMarkdown text={text} />
        </div>
      )}
    </details>
  );
});

const MessageRow = memo(function MessageRow({ message }: { message: Message }) {
  return (
    <article className={`message ${message.role}`}>
      <header>
        {message.role === "assistant" ? (
          <BrandLogo variant="message" />
        ) : (
          <>
            <span className="avatar">
              {message.role === "user" ? "你" : "系"}
            </span>
            <strong>{message.role === "user" ? "你" : "系统"}</strong>
          </>
        )}
        {message.status === "streaming" && (
          <span className="stream-dot" aria-label="正在生成" />
        )}
      </header>
      {message.thinking && <ThinkingSection text={message.thinking} />}
      <div className="prose">
        <SafeMarkdown text={message.content} />
      </div>
      {message.tools.length > 0 && (
        <details className="tools">
          <summary>工具活动 · {message.tools.length}</summary>
          <ul>
            {message.tools.map((tool) => (
              <li key={tool.tool_id}>
                <strong>{tool.name}</strong>
                <span>
                  {
                    {
                      waiting: "等待电脑确认",
                      running: "执行中",
                      completed: "已完成",
                      failed: "失败",
                    }[tool.status]
                  }
                </span>
                {tool.summary && <p>{tool.summary}</p>}
              </li>
            ))}
          </ul>
        </details>
      )}
      {["failed", "aborted"].includes(message.status) && (
        <p className="message-status">
          {message.status === "aborted"
            ? "已停止生成"
            : "生成中断，请检查电脑端状态"}
        </p>
      )}
    </article>
  );
});

export const MessageList = memo(function MessageList({
  messages,
  hasMore,
  loading,
  onMore,
}: {
  messages: Message[];
  hasMore: boolean;
  loading: boolean;
  onMore: () => Promise<void>;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const bottom = useRef(true);
  const previous = useRef("");
  const anchor = useRef<{ height: number; top: number } | null>(null);
  const [unseen, setUnseen] = useState(false);
  const signature = useMemo(
    () =>
      messages
        .map(
          (m) =>
            `${m.message_id}:${m.content.length}:${m.thinking.length}:${m.tools.map((t) => t.status).join(",")}:${m.status}`,
        )
        .join("|"),
    [messages],
  );
  useLayoutEffect(() => {
    const element = scroller.current;
    if (!element) return;
    if (anchor.current && !loading) {
      element.scrollTop =
        anchor.current.top + element.scrollHeight - anchor.current.height;
      anchor.current = null;
    } else if (previous.current !== signature) {
      if (bottom.current) element.scrollTop = element.scrollHeight;
      else setUnseen(true);
    }
    previous.current = signature;
  }, [signature, loading]);
  return (
    <div className="message-region">
      <div
        className="messages"
        ref={scroller}
        onScroll={() => {
          const el = scroller.current!;
          bottom.current =
            el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          if (bottom.current) setUnseen(false);
        }}
        aria-label="聊天记录"
        aria-busy={loading}
      >
        {hasMore && (
          <button
            className="history-more"
            disabled={loading}
            onClick={() => {
              const el = scroller.current!;
              anchor.current = { height: el.scrollHeight, top: el.scrollTop };
              void onMore();
            }}
          >
            {loading ? "正在读取…" : "↑ 更早的消息"}
          </button>
        )}
        {messages.map((message) => (
          <MessageRow key={message.message_id} message={message} />
        ))}
        {loading && (
          <p className="muted" role="status">
            正在读取历史…
          </p>
        )}
      </div>
      {unseen && (
        <button
          className="new-content"
          onClick={() => {
            scroller.current!.scrollTop = scroller.current!.scrollHeight;
            bottom.current = true;
            setUnseen(false);
          }}
        >
          ↓ 有新内容
        </button>
      )}
    </div>
  );
});
