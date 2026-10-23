import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
// Local Vazirmatn weights only — the dashboard must render Persian without any CDN.
import "@fontsource/vazirmatn/400.css";
import "@fontsource/vazirmatn/500.css";
import "@fontsource/vazirmatn/600.css";
import "@fontsource/vazirmatn/700.css";
import "./theme.css";

const container = document.getElementById("root");
if (!container) throw new Error("mount point #root is missing from index.html");

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
