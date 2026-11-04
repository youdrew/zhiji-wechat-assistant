"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// MCP 配对窗的渲染进程桥：页面只能把一次明确的选择交给主进程，发不出任何网络请求
// （选择经主进程 stdout 交回 Core，见 bridge/core_mcp_pairing_window.py）。
const { contextBridge, ipcRenderer } = require("electron");

let sent = false;
contextBridge.exposeInMainWorld("pairingHost", {
  decide(value) {
    // {approved, accounts, capabilities} 是用户的明确决定；null 只在倒计时归零时出现。
    if (sent || (value !== null && (typeof value !== "object" || typeof value.approved !== "boolean"))) return;
    sent = true;
    ipcRenderer.send("zhiji-pairing:decide", value);
  },
});
