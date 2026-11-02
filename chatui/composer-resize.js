/* Know Your Friends, 2026-10-14. Apache-2.0. Resizable chat draft composer. */
(function (host) {
  "use strict";

  const storageKey = "chat-input-height-v1";
  const heightProperty = "--chat-input-height";
  const minimum = 140, maximum = 480, messageMinimum = 140, defaultHeight = 180;
  let config = null, preferred = null, drag = null, initialized = false;

  const zoom = () => Number(document.documentElement.style?.zoom) || 1;
  function limits() {
    const scale = zoom();
    const containerBounds = config.container.getBoundingClientRect();
    const mainBounds = config.mainArea.getBoundingClientRect();
    const height = containerBounds.height / scale;
    const paneHeight = config.pane.getBoundingClientRect().height / scale;
    const mainHeight = mainBounds.height / scale;
    const paneStyle = host.getComputedStyle?.(config.pane);
    const paneMargins = (parseFloat(paneStyle?.marginTop) || 0) + (parseFloat(paneStyle?.marginBottom) || 0);
    // A previously tall pane can briefly overflow after the window shrinks.
    const headerHeight = Math.max(0, height - paneHeight - mainHeight,
      (mainBounds.top - containerBounds.top) / scale + paneMargins);
    return { min: minimum, max: Math.max(minimum, Math.min(maximum,
      height - headerHeight - messageMinimum)) };
  }
  function clamp(height) {
    const range = limits();
    return Math.max(range.min, Math.min(height, range.max));
  }
  const currentHeight = () => config.pane.getBoundingClientRect().height / zoom();
  function updateAria(height, range = limits()) {
    config.handle.setAttribute("aria-valuemin", String(range.min));
    config.handle.setAttribute("aria-valuemax", String(Math.round(range.max)));
    config.handle.setAttribute("aria-valuenow", String(Math.round(height)));
    config.handle.setAttribute("aria-valuetext", `${Math.round(height)} 像素`);
  }
  function display(height) {
    const previous = currentHeight(), value = clamp(height), cssValue = `${value}px`;
    if (config.pane.style.getPropertyValue(heightProperty) !== cssValue)
      config.pane.style.setProperty(heightProperty, cssValue);
    updateAria(value);
    const rendered = currentHeight();
    if (Math.abs(rendered - previous) > 0.01) config.onResize?.(rendered);
    return value;
  }
  function refresh() {
    if (!config || config.container.getBoundingClientRect().height <= 0) return;
    display(drag ? drag.height : preferred ?? defaultHeight);
  }
  function readPreference() {
    try {
      const stored = host.localStorage.getItem(storageKey);
      if (stored === null || !stored.trim()) return null;
      const height = Number(stored);
      return Number.isFinite(height) && height >= minimum && height <= maximum ? height : null;
    } catch (_) { return null; }
  }
  function savePreference() {
    try { host.localStorage.setItem(storageKey, String(preferred)); }
    catch (_) { /* A storage failure does not prevent resizing this window. */ }
  }
  function releaseDrag() {
    const previous = drag;
    drag = null;
    document.body.classList.remove("composer-resizing");
    config.handle.classList.remove("dragging");
    if (previous && config.handle.hasPointerCapture(previous.pointerId))
      config.handle.releasePointerCapture(previous.pointerId);
    return previous;
  }
  function cancelDrag() {
    if (!drag) return;
    preferred = releaseDrag().preferred;
    refresh();
  }
  function startDrag(event) {
    if (drag || event.button !== 0 || !event.isPrimary ||
        config.container.getBoundingClientRect().height <= 0) return;
    event.preventDefault();
    refresh();
    const height = currentHeight();
    drag = { pointerId: event.pointerId, startY: event.clientY, startHeight: height,
      scale: zoom(), height, preferred };
    try { config.handle.setPointerCapture(event.pointerId); }
    catch (_) { cancelDrag(); return; }
    document.body.classList.add("composer-resizing");
    config.handle.classList.add("dragging");
    config.handle.focus();
  }
  function moveDrag(event) {
    if (!drag || event.pointerId !== drag.pointerId || !Number.isFinite(event.clientY)) return;
    event.preventDefault();
    drag.height = clamp(drag.startHeight + (drag.startY - event.clientY) / drag.scale);
    display(drag.height);
  }
  function finishDrag(event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    event.preventDefault();
    moveDrag(event);
    const previous = releaseDrag();
    preferred = clamp(previous.height);
    refresh();
    savePreference();
  }
  function resetPreference(event) {
    event.preventDefault();
    cancelDrag();
    preferred = null;
    try { host.localStorage.removeItem(storageKey); } catch (_) { }
    refresh();
  }
  function resizeKey(event) {
    if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    cancelDrag();
    const range = limits(), step = event.shiftKey ? 50 : 10;
    const height = event.key === "Home" ? range.min : event.key === "End" ? range.max :
      currentHeight() + (event.key === "ArrowUp" ? step : -step);
    preferred = clamp(height);
    refresh();
    savePreference();
  }
  function init(options) {
    if (initialized) { refresh(); return; }
    if (!options?.pane || !options.handle || !options.container || !options.mainArea) return;
    config = options;
    initialized = true;
    preferred = readPreference();
    config.handle.addEventListener("pointerdown", startDrag);
    config.handle.addEventListener("pointermove", moveDrag);
    config.handle.addEventListener("pointerup", finishDrag);
    config.handle.addEventListener("pointercancel", event => {
      if (drag?.pointerId === event.pointerId) cancelDrag();
    });
    config.handle.addEventListener("lostpointercapture", event => {
      if (drag?.pointerId === event.pointerId) cancelDrag();
    });
    config.handle.addEventListener("keydown", resizeKey);
    config.handle.addEventListener("dblclick", resetPreference);
    document.addEventListener("keydown", event => {
      if (!drag || event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      cancelDrag();
    });
    host.addEventListener("blur", cancelDrag);
    host.addEventListener("resize", refresh);
    if (typeof host.ResizeObserver === "function") {
      const observer = new host.ResizeObserver(refresh);
      observer.observe(config.container);
      observer.observe(config.mainArea);
    }
    updateAria(preferred ?? defaultHeight, { min: minimum, max: maximum });
    refresh();
  }

  host.ComposerResize = { init, refresh };
})(window);
