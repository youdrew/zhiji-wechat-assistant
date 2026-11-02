"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Local media references only; images/playback load on action, animated stickers lazily.
// Modified by Know Your Friends, 2026-10-10: independent image windows and local stickers.
(function () {
  let playback = null;
  let preview = null;
  let observer = null;
  let imageEpoch = 0;
  const imageWindows = new Map();
  const kinds = new Set(["image", "sticker", "voice", "video"]);
  const labels = { image: "图片", sticker: "表情", voice: "语音", video: "视频" };
  const node = (tag, className, text) => {
    const result = document.createElement(tag);
    result.className = className || "";
    if (text !== undefined) result.textContent = text;
    return result;
  };
  function seconds(value) {
    return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.ceil(value / 1000) : 0;
  }
  function duration(value, clock = false) {
    const total = seconds(value);
    if (!total) return "";
    return clock ? `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}` : `${total}″`;
  }
  function mediaURL(scope, id, variant) {
    if (![scope?.account, scope?.user, id].every(value => typeof value === "string" && value &&
        value.length <= 4096 && !/[\u0000-\u001f\u007f]/u.test(value))) return null;
    return `/api/media?${new URLSearchParams({ account: scope.account, user: scope.user, id, variant })}`;
  }
  function icon(kind) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", kind === "voice" ? "M5 10v4m4-7v10m4-13v16m4-13v10m4-7v4" : "M9 5l11 7-11 7z");
    svg.appendChild(path);
    return svg;
  }
  function release(media) {
    if (!media) return;
    media.pause();
    media.removeAttribute("src");
    media.load();
  }
  function stopPlayback() {
    const previous = playback;
    playback = null;
    if (!previous) return;
    previous.button?.classList.remove("is-playing");
    previous.button?.setAttribute("aria-pressed", "false");
    release(previous.media);
  }
  function closePreview() {
    const previous = preview;
    preview = null;
    if (!previous) return;
    if (previous.media.tagName === "VIDEO") release(previous.media);
    else previous.media.removeAttribute("src");
    if (previous.dialog.open) previous.dialog.close();
    previous.dialog.remove();
  }
  function stopInline() {
    stopPlayback();
    closePreview();
  }
  function reset() {
    stopInline();
    imageEpoch++;
    try { window.desktopHost?.closeImagePreviews?.(); } catch {}
    for (const popup of imageWindows.values()) {
      try { popup.close(); } catch {}
    }
    imageWindows.clear();
  }
  function imageScope(scope, id) {
    if (![scope?.account, scope?.user].every(value => typeof value === "string" && value &&
        value.length <= 4096 && !/[\u0000-\u001f\u007f]/u.test(value)) || typeof id !== "string" || !/^[a-f0-9]{64}$/u.test(id)) return null;
    return { account: scope.account, user: scope.user, id };
  }
  async function showImage(scope, id, current, failed) {
    const selected = imageScope(scope, id);
    if (!selected || !current()) return;
    stopInline();
    const epoch = imageEpoch;
    if (typeof window.desktopHost?.openImagePreview === "function") {
      let opened = false;
      try { opened = await window.desktopHost.openImagePreview(selected); } catch {}
      if (!opened && epoch === imageEpoch && current()) failed();
      return;
    }
    const key = JSON.stringify(selected);
    const previous = imageWindows.get(key);
    if (previous && !previous.closed) {
      try { previous.focus(); return; } catch { imageWindows.delete(key); }
    }
    try {
      const popup = window.open(`/image-viewer.html#${new URLSearchParams(selected)}`, "_blank", "popup,width=960,height=760");
      if (popup) imageWindows.set(key, popup);
      else failed();
    } catch { failed(); }
  }
  function observeRemoval() {
    if (observer || typeof MutationObserver !== "function") return;
    const root = document.getElementById("chatMessages");
    if (!root) return;
    observer = new MutationObserver(() => {
      if (playback && (!playback.owner.isConnected || !playback.current())) stopPlayback();
      if (preview && (!preview.owner.isConnected || !preview.current())) closePreview();
    });
    observer.observe(root, { childList: true, subtree: true });
  }
  function showVideoPreview(url, owner, current) {
    if (!current()) return;
    stopInline();
    const dialog = node("dialog", "chat-media-dialog");
    dialog.setAttribute("aria-label", "播放视频");
    const close = node("button", "chat-media-close", "×");
    close.type = "button";
    close.setAttribute("aria-label", "关闭预览");
    close.addEventListener("click", closePreview);
    dialog.addEventListener("cancel", event => { event.preventDefault(); closePreview(); });
    dialog.addEventListener("click", event => { if (event.target === dialog) closePreview(); });
    const media = node("video", "chat-media-original");
    const error = node("p", "chat-media-preview-error", "视频暂不可用");
    error.hidden = true;
    error.setAttribute("role", "status");
    media.controls = true; media.playsInline = true; media.preload = "none";
    const selected = { dialog, media, owner, current };
    preview = selected;
    media.addEventListener("error", () => {
      if (preview !== selected || !current()) return;
      media.hidden = true;
      error.hidden = false;
    });
    dialog.append(close, media, error);
    document.body.appendChild(dialog);
    dialog.showModal();
    media.src = url;
    void media.play().catch(() => {
      // Keep native controls available if the browser requires another gesture.
      if (preview === selected && !current()) closePreview();
    });
    observeRemoval();
  }
  function voiceBubble(media, scope, id, current) {
    const wrap = node("div", "chat-voice-wrap");
    const button = node("button", "msg-bubble chat-voice-bubble");
    button.type = "button";
    button.setAttribute("aria-label", `播放语音${seconds(media.durationMs) ? `，${seconds(media.durationMs)}秒` : ""}`);
    button.setAttribute("aria-pressed", "false");
    button.style.width = `${Math.min(240, 96 + seconds(media.durationMs) * 2)}px`;
    button.append(icon("voice"), node("span", "chat-voice-duration", duration(media.durationMs) || "语音"));
    const error = node("span", "chat-media-unavailable", "语音暂不可用");
    error.hidden = true;
    error.setAttribute("role", "status");
    wrap.append(button, error);
    button.addEventListener("click", () => {
      if (!current()) return;
      if (playback?.button === button) { stopPlayback(); return; }
      const url = mediaURL(scope, id, "playback");
      if (!url) return;
      stopInline();
      error.hidden = true;
      const audio = document.createElement("audio");
      audio.preload = "none";
      const selected = { media: audio, button, owner: wrap, current };
      playback = selected;
      const failed = () => {
        if (playback !== selected || !current()) return;
        stopPlayback();
        error.hidden = false;
      };
      audio.addEventListener("error", failed);
      audio.addEventListener("ended", () => { if (playback === selected) stopPlayback(); });
      audio.addEventListener("play", () => {
        if (playback !== selected || !current()) { release(audio); return; }
        button.classList.add("is-playing");
        button.setAttribute("aria-pressed", "true");
      });
      audio.src = url;
      void audio.play().catch(failed);
      observeRemoval();
    });
    if (typeof media.transcript === "string" && media.transcript.trim() && media.transcriptSource === "wechat") {
      const transcript = node("div", "chat-voice-transcript");
      transcript.append(node("span", "chat-voice-transcript-label", "微信转文字"), node("p", "", media.transcript));
      wrap.appendChild(transcript);
    }
    return wrap;
  }
  function visualBubble(media, scope, id, current) {
    const kind = media.kind;
    const isSticker = kind === "sticker";
    const opensImage = kind === "image" || isSticker;
    const button = node("button", `msg-bubble chat-media-tile chat-media-${kind}`);
    button.type = "button";
    button.setAttribute("aria-label", isSticker ? "查看表情" : opensImage ? "查看图片" : "播放视频");
    const width = Number(media.width), height = Number(media.height);
    const ratio = Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0 ?
      Math.max(0.5, Math.min(2, width / height)) : isSticker ? 1 : 4 / 3;
    button.style.width = `${Math.round(isSticker ? Math.min(144, 144 * ratio) : Math.min(224, 240 * ratio))}px`;
    button.style.aspectRatio = String(ratio);
    const image = node("img", "chat-media-thumbnail");
    image.alt = isSticker ? "聊天表情" : opensImage ? "聊天图片" : "视频封面";
    image.loading = "lazy";
    image.decoding = "async";
    image.referrerPolicy = "no-referrer";
    const unavailable = node("span", "chat-media-tile-status", `${labels[kind]}暂不可用`);
    unavailable.hidden = true;
    unavailable.setAttribute("role", "status");
    // Original GIF/WebP bytes keep animation; never render message-supplied remote URLs.
    const thumbnailURL = mediaURL(scope, id, isSticker ? "original" : kind === "image" ? "thumbnail" : "frame");
    image.addEventListener("error", () => {
      if (!current()) return;
      image.hidden = true;
      // A missing frame does not imply the original video is unavailable.
      if (opensImage) unavailable.hidden = false;
    });
    if (thumbnailURL) image.src = thumbnailURL;
    else { image.hidden = true; unavailable.hidden = false; button.disabled = true; }
    button.append(image, unavailable);
    if (kind === "video") {
      const play = node("span", "chat-video-play");
      play.appendChild(icon("video"));
      button.append(play, node("span", "chat-video-duration", duration(media.durationMs, true)));
    }
    button.addEventListener("click", () => {
      if (opensImage) {
        void showImage(scope, id, current, () => { unavailable.hidden = false; });
        return;
      }
      const url = mediaURL(scope, id, "playback");
      if (url && current()) showVideoPreview(url, button, current);
    });
    return button;
  }
  function render(message, scope, isCurrent) {
    const media = message?.media;
    if (!media || !kinds.has(media.kind)) return null;
    const captured = { account: scope.account, user: scope.user, generation: scope.generation };
    const current = () => isCurrent(captured);
    return media.kind === "voice" ? voiceBubble(media, captured, String(message.id), current) :
      visualBubble(media, captured, String(message.id), current);
  }
  window.ChatMedia = Object.freeze({ render, reset });
})();
