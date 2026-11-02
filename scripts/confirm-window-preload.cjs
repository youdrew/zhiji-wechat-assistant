"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 高影响删除确认窗的渲染进程桥：页面只能把一次明确的决定交给主进程，
// 看不到令牌，也发不出任何网络请求（回传由主进程经 MCP 设置通道完成）。
const { contextBridge, ipcRenderer } = require("electron");

let sent = false;
contextBridge.exposeInMainWorld("confirmHost", {
  decide(value) {
    // true/false 是用户的明确决定；null 只在倒计时归零时出现（未决定即过期）。
    if (sent || (value !== true && value !== false && value !== null)) return;
    sent = true;
    ipcRenderer.send("zhiji-confirm:decide", value);
  },
});
