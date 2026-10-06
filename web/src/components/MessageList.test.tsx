import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { SafeMarkdown } from "./MessageList";
it("blocks raw HTML, remote images and unsafe links", () => {
  const { container } = render(
    <SafeMarkdown
      text={
        '<script>alert(1)</script>\n\n![追踪](https://evil.test/track)\n\n[恶意](javascript:alert%281%29)\n\n[正常](https://example.com)\n\n<img src="https://evil.test/raw">'
      }
    />,
  );
  expect(container.querySelector("img,script,iframe")).toBeNull();
  expect(screen.getByText(/图片未加载/)).toBeInTheDocument();
  const links = container.querySelectorAll("a");
  expect(links).toHaveLength(1);
  expect(links[0]).toHaveAttribute("rel", "noopener noreferrer");
});
