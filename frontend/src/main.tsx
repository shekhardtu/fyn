import "@fontsource-variable/dm-sans";
import "@fontsource-variable/manrope";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router/dom";
import { appRouter } from "@/app/router";
import { setupServiceWorker } from "@/lib/service-worker";
import "@/app/globals.css";

setupServiceWorker();

const root = document.getElementById("root");
if (!root) throw new Error("fyn AI could not find its application root.");

createRoot(root).render(
  <StrictMode>
    <RouterProvider router={appRouter} />
  </StrictMode>,
);
