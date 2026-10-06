import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { BrandLogo } from "./BrandLogo";

it("uses the supplied SVG unchanged, with accessible wordmark text and its aspect ratio", () => {
  const asset = resolve("src/assets/limbowave-logo-transparent.svg");
  const original = resolve("../docs/branding/limbowave-logo-transparent.svg");
  expect(readFileSync(asset)).toEqual(readFileSync(original));
  render(<BrandLogo />);
  const logo = screen.getByRole("img", { name: "LimboWave" });
  expect(logo).toHaveAttribute(
    "src",
    expect.stringContaining("limbowave-logo-transparent.svg"),
  );
  expect(logo).toHaveAttribute("width", "1161");
  expect(logo).toHaveAttribute("height", "710");
  expect(screen.queryByText("LimboWave")).toBeNull();
});
