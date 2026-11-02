/* Know Your Friends, 2026-10-13. Apache-2.0. Resizable conversation list. */
(function (host) {
  "use strict";

  const storageKey = "session-pane-width-v1";
  const widthProperty = "--session-pane-width";
  const minimum = 174, maximum = 480, chatMinimum = 360;
  let config = null, preferred = null, drag = null, initialized = false;

  const zoom = () => Number(document.documentElement.style?.zoom) || 1;
  function limits() {
    const scale = zoom();
    return { min: minimum, max: Math.max(minimum, Math.min(maximum,
      (config.container.getBoundingClientRect().width - config.rail.getBoundingClientRect().width) / scale - chatMinimum)) };
  }
  function clamp(width) {
    const range = limits();
    return Math.max(range.min, Math.min(width, range.max));
  }
  const currentWidth = () => config.pane.getBoundingClientRect().width / zoom();
  function updateAria(width) {
    const range = limits();
    config.handle.setAttribute("aria-valuemin", String(range.min));
    config.handle.setAttribute("aria-valuemax", String(Math.round(range.max)));
    config.handle.setAttribute("aria-valuenow", String(Math.round(width)));
    config.handle.setAttribute("aria-valuetext", `${Math.round(width)} 像素`);
  }
  function display(width) {
    const value = clamp(width);
    config.pane.style.setProperty(widthProperty, `${value}px`);
    updateAria(value);
    return value;
  }
  function refresh() {
    if (!config) return;
    if (drag) { display(drag.width); return; }
    if (preferred !== null) { display(preferred); return; }
    config.pane.style.removeProperty(widthProperty);
    const width = currentWidth(), constrained = clamp(width);
    if (constrained !== width) display(constrained);
    else updateAria(width);
  }
  function readPreference() {
    try {
      const stored = host.localStorage.getItem(storageKey);
      if (stored === null || !stored.trim()) return null;
      const width = Number(stored);
      return Number.isFinite(width) && width >= minimum && width <= maximum ? width : null;
    } catch (_) { return null; }
  }
  function savePreference() {
    try { host.localStorage.setItem(storageKey, String(preferred)); }
    catch (_) { /* A storage failure does not prevent resizing this window. */ }
  }
  function releaseDrag() {
    const previous = drag;
    drag = null;
    document.body.classList.remove("session-resizing");
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
    if (drag || event.button !== 0 || !event.isPrimary) return;
    event.preventDefault();
    refresh();
    const width = currentWidth();
    drag = { pointerId: event.pointerId, startX: event.clientX, startWidth: width,
      scale: zoom(), width, preferred };
    try { config.handle.setPointerCapture(event.pointerId); }
    catch (_) { cancelDrag(); return; }
    document.body.classList.add("session-resizing");
    config.handle.classList.add("dragging");
    config.handle.focus();
  }
  function moveDrag(event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    event.preventDefault();
    drag.width = clamp(drag.startWidth + (event.clientX - drag.startX) / drag.scale);
    display(drag.width);
  }
  function finishDrag(event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    event.preventDefault();
    const previous = releaseDrag();
    preferred = clamp(previous.width);
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
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    cancelDrag();
    const range = limits(), step = event.shiftKey ? 50 : 10;
    const width = event.key === "Home" ? range.min : event.key === "End" ? range.max :
      currentWidth() + (event.key === "ArrowRight" ? step : -step);
    preferred = clamp(width);
    refresh();
    savePreference();
  }
  function init(options) {
    if (initialized) { refresh(); return; }
    if (!options?.pane || !options.handle || !options.container || !options.rail) return;
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
    }
    refresh();
  }

  host.SessionResize = { init, refresh };
})(window);
