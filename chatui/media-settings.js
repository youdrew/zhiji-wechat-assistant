/* Know Your Friends, 2026-10-10. Apache-2.0. Account-scoped media preferences. */
(function (host) {
  "use strict";
  function init({ account, request }) {
    const byId = id => document.getElementById(id);
    const mode = byId("mediaStorageMode"), limit = byId("mediaCacheLimit");
    const clear = byId("btnClearMediaCache"), status = byId("mediaStorageStatus");
    const hint = byId("mediaStorageHint");
    if (!mode || !limit || !clear || !status || !hint) return;
    let serial = 0, saved = null, savedAccount = null, busy = false;
    const formatSize = bytes => `${Math.round(Math.max(0, Number(bytes) || 0) / 1048576)} MB`;
    function render() {
      const active = account();
      const available = saved && savedAccount === active;
      mode.disabled = limit.disabled = clear.disabled = busy || !active || !available;
      if (available) {
        mode.value = saved.mode;
        limit.value = String(saved.cacheMaxBytes);
        status.textContent = `缓存 ${formatSize(saved.cacheBytes)} · 留存 ${formatSize(saved.archiveBytes)}`;
      }
      hint.textContent = mode.value === "archive"
        ? "查看或分析时复制到本机；占用磁盘，清理缓存不删除留存。"
        : "引用微信原文件；原文件清理后可能无法查看。";
    }
    async function run(path, body) {
      const owner = account(), ticket = ++serial;
      if (!owner) {
        saved = savedAccount = null;
        busy = false;
        render();
        status.textContent = "连接微信后可设置";
        return;
      }
      busy = true;
      render();
      status.textContent = body ? "保存中…" : "";
      try {
        const result = await request(body ? path : `${path}?account=${encodeURIComponent(owner)}`,
          body ? { method: "POST", body: JSON.stringify({ ...body, account: owner }) } : {});
        if (ticket !== serial || owner !== account()) return;
        if (result.account !== owner || !["reference", "archive"].includes(result.mode) ||
            !Number.isSafeInteger(result.cacheMaxBytes)) throw new Error("invalid-media-settings");
        saved = result;
        savedAccount = owner;
        busy = false;
        render();
      } catch (_) {
        if (ticket !== serial || owner !== account()) return;
        busy = false;
        render();
        status.textContent = body ? "保存失败，请重试" : "媒体设置读取失败";
      } finally {
        if (ticket === serial && owner !== account()) {
          busy = false;
          saved = savedAccount = null;
          render();
          status.textContent = "账号已切换，请重新打开设置";
        }
      }
    }
    host.MediaSettings.refresh = () => run("/api/media/settings");
    byId("btnSettings")?.addEventListener("click", () => {
      if (!host.desktopHost?.openSettingsWindow || host.desktopHost?.isSettingsWindow) void run("/api/media/settings");
    });
    mode.addEventListener("change", () => {
      if (busy || !saved || savedAccount !== account()) return;
      void run("/api/media/settings", { mode: mode.value });
    });
    limit.addEventListener("change", () => {
      if (busy || !saved || savedAccount !== account()) return;
      void run("/api/media/settings", { cacheMaxBytes: Number(limit.value) });
    });
    clear.addEventListener("click", () => {
      if (busy || !saved || savedAccount !== account()) return;
      void run("/api/media/cache/clear", {});
    });
    render();
  }
  host.MediaSettings = { init };
})(window);
