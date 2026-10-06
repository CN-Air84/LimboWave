import { useRef } from "react";
import type { Model } from "../api/dto";
export function Composer({
  draft,
  models,
  model,
  disabled,
  running,
  busy,
  onDraft,
  onSend,
  onAbort,
  onModel,
}: {
  draft: string;
  models: Model[];
  model: string;
  disabled: boolean;
  running: boolean;
  busy: boolean;
  onDraft: (value: string) => void;
  onSend: () => void;
  onAbort: () => void;
  onModel: (id: string) => void;
}) {
  const composing = useRef(false);
  return (
    <div className="composer-wrap">
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          if (!disabled && !running && draft.trim() && !composing.current)
            onSend();
        }}
      >
        <label className="sr-only" htmlFor="message-input">
          消息
        </label>
        <textarea
          id="message-input"
          value={draft}
          rows={2}
          maxLength={32768}
          placeholder="写下你的想法…"
          onChange={(event) => onDraft(event.target.value)}
          onCompositionStart={() => {
            composing.current = true;
          }}
          onCompositionEnd={() => {
            composing.current = false;
          }}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing &&
              !composing.current &&
              event.keyCode !== 229 &&
              window.matchMedia("(pointer: fine)").matches
            ) {
              event.preventDefault();
              if (!disabled && !running && draft.trim()) onSend();
            }
          }}
        />
        <div className="composer-actions">
          <label className="model-picker">
            <span aria-hidden="true">◈</span>
            <span className="sr-only">逻辑模型</span>
            <select
              aria-label="逻辑模型"
              value={model}
              onChange={(event) => onModel(event.target.value)}
              disabled={disabled || running}
            >
              <option value="" disabled>
                选择模型
              </option>
              {models.map((m) => (
                <option key={m.model_id} value={m.model_id}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>
          {running ? (
            <button
              type="button"
              className="stop"
              onClick={onAbort}
              disabled={busy}
              aria-label="停止生成"
            >
              ■ 停止
            </button>
          ) : (
            <button
              className="send"
              type="submit"
              disabled={disabled || !draft.trim() || !model}
              aria-label="发送消息"
            >
              ↑
            </button>
          )}
        </div>
      </form>
      <p className="composer-note">
        {running
          ? "生成中 · 切换页面不会中断"
          : "仅在本页保留草稿 · 内容由 AI 生成，请留意核实"}
      </p>
    </div>
  );
}
