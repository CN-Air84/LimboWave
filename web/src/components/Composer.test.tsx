import { render, fireEvent, screen } from "@testing-library/react";
import { it, expect, vi, beforeEach } from "vitest";
import { Composer } from "./Composer";
const defaults = {
  draft: "你好",
  models: [{ model_id: "m", label: "模型" }],
  model: "m",
  disabled: false,
  running: false,
  busy: false,
  onDraft: vi.fn(),
  onSend: vi.fn(),
  onAbort: vi.fn(),
  onModel: vi.fn(),
};
beforeEach(() => {
  vi.clearAllMocks();
  window.matchMedia = vi.fn().mockReturnValue({ matches: true });
});
it("IME composition enter and keyCode 229 never submit", () => {
  render(<Composer {...defaults} />);
  const input = screen.getByRole("textbox");
  fireEvent.compositionStart(input);
  fireEvent.keyDown(input, { key: "Enter" });
  fireEvent.compositionEnd(input);
  fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
  expect(defaults.onSend).not.toHaveBeenCalled();
  fireEvent.keyDown(input, { key: "Enter" });
  expect(defaults.onSend).toHaveBeenCalledTimes(1);
});
it("shift enter and touch keyboard enter insert lines, tap sends", () => {
  window.matchMedia = vi.fn().mockReturnValue({ matches: false });
  render(<Composer {...defaults} />);
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
  fireEvent.keyDown(screen.getByRole("textbox"), {
    key: "Enter",
    shiftKey: true,
  });
  expect(defaults.onSend).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  expect(defaults.onSend).toHaveBeenCalledOnce();
});
it("keeps draft editable while busy and allows only run stop", () => {
  render(<Composer {...defaults} running disabled />);
  expect(screen.getByRole("textbox")).not.toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "停止生成" }));
  expect(defaults.onAbort).toHaveBeenCalledOnce();
  expect(
    screen.queryByRole("button", { name: "发送消息" }),
  ).not.toBeInTheDocument();
});
