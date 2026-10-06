import { createRoot } from "react-dom/client";
import { App } from "./App";
import { consumePairingFragment } from "./api/client";
import { ChatController } from "./state/controller";
import "./styles/tokens.css";
import "./styles/layout.css";
const ticket = consumePairingFragment(window.location, window.history);
createRoot(document.getElementById("root")!).render(
  <App controller={new ChatController()} ticket={ticket} />,
);
