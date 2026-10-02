import "@mantine/core/styles.css";

import { MantineProvider } from "@mantine/core";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { createApi } from "./api/client";
import { createAppRouter } from "./router";

const api = createApi();
const router = createAppRouter();
const root = document.getElementById("root");
if (!root) throw new Error("index.html has no #root element");

createRoot(root).render(
  <StrictMode>
    <MantineProvider defaultColorScheme="auto">
      <App api={api} router={router} />
    </MantineProvider>
  </StrictMode>,
);
