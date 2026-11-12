import "@fontsource-variable/inter";
import "./index.css";

import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router-dom";

import { AppProviders } from "./app/providers";
import { createAppRouter } from "./app/router";

createRoot(document.getElementById("root")!).render(
  <AppProviders>
    <RouterProvider router={createAppRouter()} future={{ v7_startTransition: true }} />
  </AppProviders>,
);
