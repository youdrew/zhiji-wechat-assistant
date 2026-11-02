// Copyright 2026 Zhiji contributors. SPDX-License-Identifier: Apache-2.0
"use strict";

function chatLinkUrl(value) {
  if (typeof value !== "string" || !value || value.length > 8192 ||
      /[\s\u0000-\u001f\u007f\\]/u.test(value) || !/^https?:\/\//i.test(value)) return null;
  try {
    const target = new URL(value);
    if (!["https:", "http:"].includes(target.protocol) || !target.hostname || target.username || target.password)
      return null;
    return target.href;
  } catch { return null; }
}

function attachChatLinks({ ipcMain, trustedMainFrame, shell, available = () => true }) {
  ipcMain.handle("real-client:open-chat-link", async (event, value, activated) => {
    if (!trustedMainFrame(event) || activated !== true || !available()) return false;
    const target = chatLinkUrl(value);
    if (!target) return false;
    try { await shell.openExternal(target); return true; } catch { return false; }
  });
}

module.exports = { chatLinkUrl, attachChatLinks };
