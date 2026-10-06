import { render, fireEvent, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { PasswordPage } from "./PasswordPage";
it("HTTP requires explicit risk acknowledgement and clears password on submit without storage", () => {
  const onVerify = vi.fn();
  render(
    <PasswordPage
      busy={false}
      transportSecure={false}
      onVerify={onVerify}
      onDisconnect={vi.fn()}
    />,
  );
  expect(screen.getByRole("alert")).toHaveTextContent(
    "加密密码信封不防主动攻击",
  );
  const input = screen.getByLabelText("资料库密码");
  fireEvent.change(input, { target: { value: "secret" } });
  expect(screen.getByRole("button", { name: "核验密码 ↗" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "核验密码 ↗" }));
  expect(onVerify).toHaveBeenCalledWith("secret", true);
  expect(input).toHaveValue("");
  expect(localStorage.length).toBe(0);
  expect(sessionStorage.length).toBe(0);
});
it("HTTPS still requires a password, but not HTTP risk confirmation", () => {
  const onVerify = vi.fn();
  render(
    <PasswordPage
      busy={false}
      transportSecure
      onVerify={onVerify}
      onDisconnect={vi.fn()}
    />,
  );
  expect(screen.queryByRole("checkbox")).toBeNull();
  expect(screen.getByRole("button", { name: "核验密码 ↗" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("资料库密码"), {
    target: { value: "secret" },
  });
  fireEvent.click(screen.getByRole("button", { name: "核验密码 ↗" }));
  expect(onVerify).toHaveBeenCalledWith("secret", false);
});
