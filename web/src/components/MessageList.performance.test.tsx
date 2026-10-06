import { createElement, type ComponentProps } from "react";
import Markdown from "react-markdown";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { Message } from "../api/dto";
import { MessageList } from "./MessageList";

vi.mock("react-markdown", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-markdown")>();
  return {
    ...actual,
    default: vi.fn((props: ComponentProps<typeof actual.default>) =>
      createElement(actual.default, props),
    ),
  };
});
beforeEach(() => vi.mocked(Markdown).mockClear());
function message(id: string, extra: Partial<Message> = {}): Message {
  return {
    message_id: id,
    conversation_id: "c",
    branch_id: "b",
    run_id: id,
    role: "assistant",
    content: `answer-${id}`,
    thinking: "",
    tools: [],
    status: "completed",
    ...extra,
  };
}
const more = async () => {};
function list(messages: Message[], loading = false) {
  return (
    <MessageList
      messages={messages}
      hasMore={false}
      loading={loading}
      onMore={more}
    />
  );
}
function parsed() {
  return vi.mocked(Markdown).mock.calls.map(([props]) => props.children);
}
it("does not reparse unchanged history for each streaming delta", () => {
  const history = message("old");
  const live = message("live", { status: "streaming" });
  const view = render(list([history, live]));
  expect(parsed()).toEqual(["answer-old", "answer-live"]);
  view.rerender(list([history, { ...live, content: "answer-live-more" }]));
  expect(parsed()).toEqual(["answer-old", "answer-live", "answer-live-more"]);
});
it("reuses markdown even when a snapshot replaces equal message objects", () => {
  const first = message("same");
  const view = render(list([first]));
  view.rerender(list([{ ...first }], true));
  expect(parsed()).toEqual(["answer-same"]);
});
it("does not parse collapsed thinking until it is opened", () => {
  const first = message("one", { thinking: "**private reasoning**" });
  const view = render(list([first]));
  expect(parsed()).toEqual(["answer-one"]);
  const details = view.container.querySelector(
    "details.thinking",
  ) as HTMLDetailsElement;
  details.open = true;
  fireEvent(details, new Event("toggle"));
  expect(parsed()).toEqual(["answer-one", "**private reasoning**"]);
  expect(screen.getByText("private reasoning")).toBeInTheDocument();
  details.open = false;
  fireEvent(details, new Event("toggle"));
  view.rerender(list([{ ...first, thinking: "updated reasoning" }]));
  expect(parsed()).not.toContain("updated reasoning");
  details.open = true;
  fireEvent(details, new Event("toggle"));
  expect(screen.getByText("updated reasoning")).toBeInTheDocument();
});
it("keeps a reader's scroll position when new content arrives", () => {
  const first = message("one");
  const view = render(list([first]));
  const scroller = screen.getByLabelText("聊天记录");
  Object.defineProperty(scroller, "scrollHeight", {
    configurable: true,
    value: 1000,
  });
  Object.defineProperty(scroller, "clientHeight", {
    configurable: true,
    value: 100,
  });
  scroller.scrollTop = 300;
  fireEvent.scroll(scroller);
  view.rerender(list([first, message("two")]));
  expect(scroller.scrollTop).toBe(300);
  fireEvent.click(screen.getByRole("button", { name: "↓ 有新内容" }));
  expect(scroller.scrollTop).toBe(1000);
});
