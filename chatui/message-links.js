// Copyright 2026 Zhiji WeChat Assistant contributors. SPDX-License-Identifier: Apache-2.0
(() => {
  "use strict";

  function webUrl(value) {
    if (typeof value !== "string" || value.length > 8192 || /[\s\u0000-\u001f\u007f\\]/u.test(value) ||
        !/^https?:\/\//i.test(value)) return null;
    try {
      const url = new URL(value);
      return ["http:", "https:"].includes(url.protocol) && url.hostname && !url.username && !url.password ? url : null;
    } catch { return null; }
  }

  function node(tag, className, text) {
    const value = document.createElement(tag);
    value.className = className;
    if (text !== undefined) value.textContent = text;
    return value;
  }

  function anchor(url, className, text) {
    const value = node("a", className, text);
    value.href = url.href;
    value.target = "_blank";
    value.rel = "noopener noreferrer";
    value.referrerPolicy = "no-referrer";
    // Desktop navigation is deliberately blocked; use its gesture-checked browser opener.
    const open = event => {
      if (typeof window.desktopHost?.openChatLink !== "function") return;
      if (event.type === "auxclick" && event.button !== 1) return;
      event.preventDefault();
      try { Promise.resolve(window.desktopHost.openChatLink(url.href)).catch(() => {}); } catch { }
    };
    value.addEventListener("click", open);
    value.addEventListener("auxclick", open);
    return value;
  }

  function card(message) {
    const link = message.link;
    const url = webUrl(link.url);
    const title = typeof link.title === "string" && link.title.trim() ? link.title : message.text || "分享链接";
    const value = url ? anchor(url, "msg-bubble msg-link-card") : node("div", "msg-bubble msg-link-card");
    value.appendChild(node("span", "msg-link-title", title));
    if (typeof link.description === "string" && link.description.trim()) {
      value.appendChild(node("span", "msg-link-description", link.description));
    }
    const source = typeof link.source === "string" && link.source.trim() ? link.source : "分享链接";
    value.appendChild(node("span", "msg-link-source", `↗ ${source}`));
    value.appendChild(node("span", "msg-link-address", url ? url.hostname : "链接地址不可用"));
    if (url) value.title = url.href;
    return value;
  }

  function textLinks(text) {
    const bubble = node("div", "msg-bubble");
    let offset = 0, found = false;
    for (const match of text.matchAll(/https?:\/\/[^\s<>"'，。！？；：「」『』【】《》、…\u0000-\u001f\u007f]+/giu)) {
      let candidate = match[0].replace(/[.,!?;:，。！？；：、…》」』】]+$/u, "");
      // Keep balanced parentheses in URLs, while excluding a surrounding sentence's closing punctuation.
      for (const [left, right] of [["(", ")"], ["（", "）"], ["[", "]"]]) {
        while (candidate.endsWith(right) && candidate.split(right).length > candidate.split(left).length) {
          candidate = candidate.slice(0, -1);
        }
      }
      const url = webUrl(candidate);
      if (!url) continue;
      bubble.appendChild(document.createTextNode(text.slice(offset, match.index)));
      bubble.appendChild(anchor(url, "msg-text-link", candidate));
      offset = match.index + candidate.length;
      found = true;
    }
    if (!found) return null;
    bubble.appendChild(document.createTextNode(text.slice(offset)));
    return bubble;
  }

  window.MessageLinks = Object.freeze({
    render(message) {
      if (message.link && typeof message.link === "object" && !Array.isArray(message.link)) return card(message);
      return message.kind === "text" && typeof message.text === "string" ? textLinks(message.text) : null;
    },
  });
})();
