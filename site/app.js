// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
'use strict';

const featureButtons = [...document.querySelectorAll('[data-feature]')];
const screenPanels = [...document.querySelectorAll('.screen-panel')];
function selectFeature(button, focus = false) {
  for (const item of featureButtons) {
    const selected = item === button;
    item.setAttribute('aria-selected', String(selected));
    item.tabIndex = selected ? 0 : -1;
  }
  for (const panel of screenPanels) panel.hidden = panel.id !== button.getAttribute('aria-controls');
  if (focus) button.focus();
}
featureButtons.forEach((button, index) => {
  button.addEventListener('click', () => selectFeature(button));
  button.addEventListener('keydown', event => {
    let next;
    if (event.key === 'ArrowRight') next = (index + 1) % featureButtons.length;
    if (event.key === 'ArrowLeft') next = (index + featureButtons.length - 1) % featureButtons.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = featureButtons.length - 1;
    if (next === undefined) return;
    event.preventDefault();
    selectFeature(featureButtons[next], true);
  });
});

const releaseButtons = [...document.querySelectorAll('[data-download]')];
function showReleaseFallback() {
  for (const button of releaseButtons) {
    button.dataset.pending = 'true';
    button.href = 'https://github.com/youdrew/zhiji-wechat-assistant/releases';
    button.textContent = '前往下载页 ↗';
  }
}

function trustedRelease(release) {
  if (release.state !== 'published') return false;
  if (!/^\d+\.\d+\.\d+$/.test(release.version)) return false;
  const filename = `Zhiji-WeChat-Assistant-${release.version}-Setup-x64.exe`;
  const prefix = 'https://github.com/youdrew/zhiji-wechat-assistant/releases/';
  return release.filename === filename &&
    release.downloadUrl === `${prefix}download/v${release.version}/${filename}` &&
    release.notesUrl === `${prefix}tag/v${release.version}` &&
    /^[a-f0-9]{64}$/.test(release.sha256) &&
    Number.isSafeInteger(release.sizeBytes) && release.sizeBytes > 0 &&
    typeof release.signed === 'boolean';
}

async function loadRelease() {
  showReleaseFallback();
  try {
    const response = await fetch('release.json', { cache: 'no-cache' });
    if (!response.ok) return;
    const release = await response.json();
    if (!trustedRelease(release)) return;
    for (const button of releaseButtons) {
      delete button.dataset.pending;
      button.href = release.downloadUrl;
      button.textContent = '下载 Windows 版 ↓';
      button.setAttribute('aria-label', `直接下载 Windows x64 安装包 ${release.version}`);
    }
    const notes = document.querySelector('[data-release-notes]');
    notes.href = release.notesUrl;
    notes.textContent = '版本说明与校验文件 ↗';
    document.querySelector('[data-release-meta]').textContent =
      `v${release.version} · Windows 10/11 x64 · ${(release.sizeBytes / 1024 / 1024).toFixed(1)} MB · 自带运行环境`;
    document.querySelector('[data-sha256]').textContent = release.sha256;
    document.querySelector('.checksum').hidden = false;
    const signing = document.querySelector('[data-signing-note]');
    signing.textContent = release.signed ? '' : '当前安装包未做 Windows 发布者签名，系统可能提示未知发布者。SHA-256 用于核对文件一致性。';
    signing.hidden = release.signed;
  } catch {
    // Keep the release page fallback usable; never invent an installer URL.
  }
}

document.querySelector('.copy-checksum').addEventListener('click', async event => {
  const button = event.currentTarget;
  const checksum = document.querySelector('[data-sha256]').textContent;
  if (!/^[a-f0-9]{64}$/.test(checksum)) return;
  try {
    await navigator.clipboard.writeText(checksum);
    button.textContent = '已复制';
    document.querySelector('[data-copy-status]').textContent = 'SHA-256 已复制';
  } catch {
    document.querySelector('[data-copy-status]').textContent = '请手动选择并复制上方 SHA-256';
  }
});

loadRelease();
