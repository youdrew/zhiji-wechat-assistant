"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// An isolated local viewer: no preload, external locations, or message body rendering.
(function () {
  const image = document.getElementById("previewImage"), viewport = document.getElementById("imageViewport");
  const canvas = document.getElementById("imageCanvas"), status = document.getElementById("imageStatus");
  const zoomOut = document.getElementById("zoomOut"), zoomFit = document.getElementById("zoomFit"), zoomIn = document.getElementById("zoomIn");
  const controls = [zoomOut, zoomFit, zoomIn];
  let loaded = false, scale = 1, fitting = true;
  try { window.opener = null; } catch {}

  function failure(message) {
    loaded = false;
    image.hidden = true;
    image.removeAttribute("src");
    controls.forEach(button => { button.disabled = true; });
    status.textContent = message;
    status.hidden = false;
  }
  function fitScale() {
    return Math.min(1, Math.max(1, viewport.clientWidth - 32) / image.naturalWidth,
      Math.max(1, viewport.clientHeight - 32) / image.naturalHeight);
  }
  function paint(next, fit = false) {
    if (!loaded) return;
    const width = viewport.clientWidth, height = viewport.clientHeight;
    const oldWidth = Math.max(width, image.naturalWidth * scale), oldHeight = Math.max(height, image.naturalHeight * scale);
    const centerX = (viewport.scrollLeft + width / 2 - (oldWidth - image.naturalWidth * scale) / 2) / scale;
    const centerY = (viewport.scrollTop + height / 2 - (oldHeight - image.naturalHeight * scale) / 2) / scale;
    scale = next;
    fitting = fit;
    const imageWidth = image.naturalWidth * scale, imageHeight = image.naturalHeight * scale;
    const canvasWidth = Math.max(width, imageWidth), canvasHeight = Math.max(height, imageHeight);
    image.style.width = `${imageWidth}px`;
    image.style.height = `${imageHeight}px`;
    canvas.style.width = `${canvasWidth}px`;
    canvas.style.height = `${canvasHeight}px`;
    viewport.scrollLeft = fit ? 0 : Math.max(0, centerX * scale + (canvasWidth - imageWidth) / 2 - width / 2);
    viewport.scrollTop = fit ? 0 : Math.max(0, centerY * scale + (canvasHeight - imageHeight) / 2 - height / 2);
  }
  function fit() { if (loaded) paint(fitScale(), true); }
  function zoom(factor) {
    if (loaded) paint(Math.max(Math.min(.02, fitScale()), Math.min(8, scale * factor)));
  }
  zoomOut.addEventListener("click", () => zoom(1 / 1.25));
  zoomFit.addEventListener("click", fit);
  zoomIn.addEventListener("click", () => zoom(1.25));
  viewport.addEventListener("wheel", event => {
    if (!loaded || !event.deltaY) return;
    event.preventDefault();
    zoom(event.deltaY > 0 ? 1 / 1.1 : 1.1);
  }, { passive: false });
  image.addEventListener("dblclick", () => {
    if (loaded) { if (fitting) paint(1); else fit(); }
  });
  image.addEventListener("load", () => {
    if (!image.getAttribute("src")) return;
    if (!(image.naturalWidth > 0 && image.naturalHeight > 0)) { failure("图片暂不可用"); return; }
    loaded = true;
    image.hidden = false;
    status.hidden = true;
    controls.forEach(button => { button.disabled = false; });
    fit();
  });
  image.addEventListener("error", () => { if (image.getAttribute("src")) failure("图片暂不可用"); });
  window.addEventListener("resize", () => { if (fitting) fit(); else if (loaded) paint(scale); });
  window.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); window.close(); }
  });
  window.addEventListener("pagehide", () => { loaded = false; image.removeAttribute("src"); });

  const parameters = new URLSearchParams(window.location.hash.slice(1));
  const scope = { account: parameters.get("account"), user: parameters.get("user"), id: parameters.get("id") };
  const validName = value => typeof value === "string" && value.length > 0 && value.length <= 4096 && !/[\u0000-\u001f\u007f]/u.test(value);
  const keys = [...parameters.keys()];
  if (keys.length !== 3 || new Set(keys).size !== 3 || keys.some(key => !["account", "user", "id"].includes(key)) ||
      !validName(scope.account) || !validName(scope.user) || !/^[a-f0-9]{64}$/u.test(scope.id || "")) {
    failure("图片信息无效");
    return;
  }
  image.referrerPolicy = "no-referrer";
  image.decoding = "async";
  image.src = `/api/media?${new URLSearchParams({ ...scope, variant: "original" })}`;
})();
