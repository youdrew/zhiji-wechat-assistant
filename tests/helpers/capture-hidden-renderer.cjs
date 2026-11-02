"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const assert = require("node:assert/strict");
const { nativeImage } = require("electron");

// CDP captures the complete offscreen surface independently of the desktop's
// physical monitor bounds. Keep the native window hidden throughout the capture.
async function captureHiddenRenderer(window) {
  assert.equal(window.isVisible(), false, "The synthetic renderer must stay hidden");
  const contents = window.webContents;
  await contents.executeJavaScript("document.fonts.ready");
  await contents.executeJavaScript("Promise.all(document.getAnimations().filter(value => value.effect.getComputedTiming().iterations !== Infinity).map(value => value.finished.catch(() => {})))");
  const viewport = await contents.executeJavaScript("({width:innerWidth,height:innerHeight})");
  const attached = !contents.debugger.isAttached();
  if (attached) contents.debugger.attach("1.3");
  try {
    await contents.debugger.sendCommand("Emulation.setDeviceMetricsOverride", {
      ...viewport, deviceScaleFactor: 1, mobile: false,
    });
    await contents.executeJavaScript("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
    const capture = await contents.debugger.sendCommand("Page.captureScreenshot", {
      format: "png", captureBeyondViewport: true,
      clip: { x: 0, y: 0, ...viewport, scale: 1 },
    });
    const image = nativeImage.createFromBuffer(Buffer.from(capture.data, "base64"));
    assert.deepEqual(image.getSize(), viewport, "Capture must include the complete renderer viewport");
    assert.equal(window.isVisible(), false, "Capturing must keep the window hidden");
    return image;
  } finally {
    await contents.debugger.sendCommand("Emulation.clearDeviceMetricsOverride");
    if (attached) contents.debugger.detach();
  }
}

module.exports = { captureHiddenRenderer };
