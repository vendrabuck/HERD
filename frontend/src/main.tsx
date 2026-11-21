// index.css comes FIRST, before App pulls in the topology page and its
// "@xyflow/react/dist/style.css". Since @xyflow/react 13 that stylesheet sits
// in `@layer xyflow`, and cascade layers rank by the order they are first
// declared: Tailwind's layers (theme, base, components, utilities) must be
// declared before xyflow's, or Tailwind's preflight (`margin: 0`, `padding: 0`,
// `border: 0 solid` on every element) outranks React Flow's own panel,
// Controls, and handle styles. v12's stylesheet was unlayered and beat every
// Tailwind layer; declaring xyflow's layer last keeps that.
// src/test/lib/reactFlowDefaults.test.ts pins the order.
import "./index.css";
import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import App from "./App";
import { queryClient } from "./queryClient";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
      <ReactQueryDevtools initialIsOpen={false} />
    </QueryClientProvider>
  </React.StrictMode>
);
