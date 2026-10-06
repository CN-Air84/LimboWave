import { useEffect, useRef } from "react";
import type { Conversation, Target } from "../api/dto";
export function SessionDrawer({
  open,
  conversations,
  target,
  hasMore,
  onMore,
  onSelect,
  onClose,
  onNew,
  disabled,
}: {
  open: boolean;
  conversations: Conversation[];
  target: Target | null;
  hasMore: boolean;
  onMore: () => void;
  onSelect: (c: Conversation) => void;
  onClose: () => void;
  onNew: () => void;
  disabled: boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (open && !dialog.current?.open) dialog.current?.showModal();
    else if (!open && dialog.current?.open) dialog.current.close();
  }, [open]);
  return (
    <dialog
      ref={dialog}
      className="session-drawer"
      onCancel={onClose}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="drawer-inner">
        <header>
          <h2>你的会话</h2>
          <button aria-label="关闭会话列表" onClick={onClose}>
            ×
          </button>
        </header>
        <button
          className="primary"
          disabled={disabled}
          onClick={() => {
            onNew();
            onClose();
          }}
        >
          ＋ 开启新对话
        </button>
        <p className="eyebrow">RECENT CONVERSATIONS</p>
        <nav aria-label="会话列表">
          {conversations.length === 0 && (
            <p className="muted">还没有会话。留下一点新想法吧。</p>
          )}
          {conversations.map((c) => (
            <button
              key={c.conversation_id}
              className={`session-item ${target?.conversation_id === c.conversation_id ? "selected" : ""}`}
              onClick={() => {
                onSelect(c);
                onClose();
              }}
            >
              <span>{c.title || "未命名对话"}</span>
              <small>
                {new Date(c.updated_at).toLocaleDateString("zh-CN", {
                  month: "short",
                  day: "numeric",
                })}
              </small>
            </button>
          ))}
        </nav>
        {hasMore && <button onClick={onMore}>加载更多会话</button>}
        <footer>与电脑共享会话 · 独立浏览历史</footer>
      </div>
    </dialog>
  );
}
