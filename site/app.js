// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
'use strict';

const imageButtons = [...document.querySelectorAll('[data-image]')];
const slideButtons = [...document.querySelectorAll('[data-slide]')];
const featureButtons = [...document.querySelectorAll('[data-feature]')];
const featureOptions = [...document.querySelectorAll('[data-feature-options]')];

function selectScreen(index) {
  imageButtons.forEach((button, position) => { button.closest('figure').hidden = position !== index; });
  slideButtons.forEach(button => button.setAttribute('aria-pressed', String(Number(button.dataset.slide) === index)));
  const feature = imageButtons[index].closest('figure').dataset.featureGroup;
  featureButtons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.feature === feature)));
  featureOptions.forEach(group => { group.hidden = group.dataset.featureOptions !== feature; });
  const total = String(imageButtons.length).padStart(2, '0');
  document.querySelector('.screen-number').textContent = `${String(index + 1).padStart(2, '0')} / ${total}`;
}

slideButtons.forEach(button => button.addEventListener('click', () => selectScreen(Number(button.dataset.slide))));
featureButtons.forEach(button => button.addEventListener('click', () => selectScreen(Number(button.dataset.feature))));

const viewer = document.querySelector('.image-viewer');
const viewerImage = viewer.querySelector('img');
const viewerTitle = document.getElementById('viewer-title');
const viewerCount = viewer.querySelector('.viewer-count');
let currentImage = 0;
let opener = null;

function showImage(index) {
  currentImage = (index + imageButtons.length) % imageButtons.length;
  const button = imageButtons[currentImage];
  const figure = button.closest('figure');
  const original = button.querySelector('img');
  viewerImage.src = original.src;
  viewerImage.alt = original.alt;
  viewerTitle.textContent = figure.querySelector('.shot-index').textContent;
  viewerCount.textContent = `${currentImage + 1} / ${imageButtons.length}`;
  viewer.querySelector('.viewer-image').scrollTop = 0;
}

for (const button of imageButtons) {
  button.addEventListener('click', () => {
    opener = button;
    showImage(Number(button.dataset.image));
    viewer.showModal();
  });
}
viewer.querySelector('.viewer-close').addEventListener('click', () => viewer.close());
viewer.querySelector('.viewer-previous').addEventListener('click', () => showImage(currentImage - 1));
viewer.querySelector('.viewer-next').addEventListener('click', () => showImage(currentImage + 1));
viewer.addEventListener('keydown', event => {
  if (event.key === 'ArrowLeft') { event.preventDefault(); showImage(currentImage - 1); }
  if (event.key === 'ArrowRight') { event.preventDefault(); showImage(currentImage + 1); }
});
viewer.addEventListener('close', () => { if (opener) opener.focus(); });
viewer.addEventListener('click', event => {
  if (event.target === viewer) {
    const box = viewer.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) viewer.close();
  }
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
