import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { CloudSyncProvider } from "./cloud/CloudSyncProvider";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <CloudSyncProvider>
      <App />
    </CloudSyncProvider>
  </React.StrictMode>,
);
