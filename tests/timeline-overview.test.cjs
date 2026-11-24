"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// plugin-sdk/web/timeline.js: the shared overview's dot mode and time input (added 2026-11-07).
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../plugin-sdk/web/timeline.js"), "utf8");
function node(tag = "div") {
  const value = { tag, children: [], listeners: {}, attributes: {}, style: { setProperty(name, entry) { value.style[name] = entry; } },
    className: "", textContent: "", hidden: false, title: "" };
  value.addEventListener = (name, handler) => { (value.listeners[name] ||= []).push(handler); };
  value.appendChild = child => { value.children.push(child); return child; };
  value.append = (...children) => { value.children.push(...children); };
  value.prepend = child => { value.children.unshift(child); };
  value.replaceChildren = (...children) => { value.children = children; };
  value.setAttribute = (name, entry) => { value.attributes[name] = entry; if (name === "class") value.className = entry; };
  value.remove = () => { value.removed = true; };
  value.getBoundingClientRect = () => ({ left: 0, top: 0, bottom: 0, width: 0, height: 0 });
  return value;
}
function load(now = new Date(2026, 5, 1).getTime()) {
  const body = node("body");
  const document = { createElement: node, createElementNS: (space, tag) => node(tag), body,
    querySelector: selector => body.children.find(item => !item.removed && selector === ".tl-tip" && item.className.includes("tl-tip")) || null };
  const Clock = class extends Date { static now() { return now; } };
  const window = {};
  vm.runInNewContext(source, { window, document, Date: Clock, innerWidth: 800 });
  return { Timeline: window.KyfTimeline, body };
}
const all = (item, match) => [...(match(item) ? [item] : []), ...item.children.flatMap(child => all(child, match))];
const byClass = (host, name) => all(host, item => typeof item.className === "string" && item.className.split(" ").includes(name));
const at = (year, month, day = 1) => new Date(year, month - 1, day).getTime();
const event = (id, title, startMs, changes = {}) => ({ id, title, track: "我们", status: "occurred", importance: "general",
  startMs, endMs: startMs, precision: "month", whenText: "合成时间", ...changes });
const view = (events, periods = [], coverage = { earliestMs: at(2021, 3, 5), latestMs: at(2026, 5, 20) }) => ({ events, periods, coverage });
const percent = text => Number.parseFloat(text);
function draw(Timeline, value, options = {}) {
  const host = node(); host.scrollWidth = 900;
  const calls = [];
  const drawn = Timeline.renderOverview(host, { mode: "dots", value, tracks: [{ name: "我们", color: "#07c160" }], months: 12,
    colorOf: () => "#07c160", onEvent: item => calls.push(["event", item.id]), onPeriod: item => calls.push(["period", item.id]),
    onTrack: name => calls.push(["track", name]), ...options });
  return { host, drawn, calls };
}

test("a single row has no track-name column and merges one month into one marker", () => {
  const { Timeline } = load();
  const { host, drawn, calls } = draw(Timeline, view([event("a", "第一次见面", at(2022, 4, 3)), event("b", "一起吃饭", at(2022, 4, 20)),
    event("c", "去日本", at(2023, 7, 1), { importance: "important" }), event("d", "下个月搬家", at(2026, 7, 1), { status: "planned" }),
    event("e", "取消的旅行", at(2024, 1, 1), { status: "cancelled" }), event("f", "时间不明", null)]));
  assert.equal(drawn, true); assert.equal(host.hidden, false);
  assert.equal(byClass(host, "tl-dots")[0].className, "tl-dots single");
  assert.equal(byClass(host, "tl-track-label").length, 0);
  assert.equal(byClass(host, "tl-dots-base").length, 1);
  const marks = byClass(host, "tl-dot");
  assert.deepEqual(marks.map(mark => mark.attributes["aria-label"]), ["第一次见面、一起吃饭", "去日本", "下个月搬家"]);
  assert.equal(marks[1].textContent, "★"); assert.ok(marks[1].className.includes("important"));
  assert.ok(marks[2].className.includes("planned")); assert.equal(marks[2].textContent, "");
  assert.ok(percent(marks[0].style.left) < percent(marks[1].style.left));
  for (const callback of marks[0].listeners.click) callback({});
  assert.deepEqual(calls, [["event", "a"]], "a merged marker jumps to its first event");
  assert.equal(host.scrollLeft, 900, "opens scrolled to the most recent end");
});

test("the tooltip lists every merged event and important-only keeps just the stars", () => {
  const { Timeline, body } = load();
  const events = [event("a", "见面", at(2022, 4, 3)), event("b", "一起爬山", at(2022, 4, 9), { importance: "important" }), event("c", "聚餐", at(2023, 2, 1))];
  const { host } = draw(Timeline, view(events));
  const [merged] = byClass(host, "tl-dot");
  assert.equal(merged.textContent, "★", "a merged marker with an important event is a star");
  for (const callback of merged.listeners.mouseenter) callback({});
  const tip = body.children.find(item => item.className.includes("tl-tip"));
  assert.deepEqual(tip.children.map(line => line.children.map(part => part.textContent).join("")), ["合成时间见面", "合成时间一起爬山"]);
  const only = draw(Timeline, view(events), { importantOnly: true }).host;
  assert.deepEqual(byClass(only, "tl-dot").map(mark => mark.attributes["aria-label"]), ["一起爬山"]);
});

test("year-only events sit in the middle of their year and quarters label the axis", () => {
  const { Timeline } = load(new Date(2023, 11, 1).getTime());
  const value = view([event("a", "同学", at(2022, 1, 1), { precision: "year" }), event("b", "毕业", at(2023, 6, 1))], [], { earliestMs: at(2022, 1, 5) });
  const yearly = draw(Timeline, value).host;
  const columns = byClass(yearly, "tl-year-label");
  assert.deepEqual(columns.map(label => label.textContent), ["2022", "2023"]);
  assert.equal(percent(byClass(yearly, "tl-dot")[0].style.left), percent(columns[0].style.left));
  const quarterly = draw(Timeline, value, { months: 3 }).host;
  const quarters = byClass(quarterly, "tl-year-label");
  assert.deepEqual(quarters.map(label => label.textContent), ["22Q1", "…", "23Q2", "23Q3", "23Q4"], "three empty quarters fold");
  const middle = percent(byClass(quarterly, "tl-dot")[0].style.left);
  assert.ok(percent(quarters[0].style.left) < middle && middle < percent(quarters[2].style.left), `a year-only event is mid-2022 (${middle})`);
});

test("several rows get names, periods are thin lines under the dots and empty spans fold", () => {
  const { Timeline } = load();
  const value = view([event("a", "认识", at(2016, 5, 1)), event("b", "入职同一家公司", at(2023, 3, 1), { track: "工作", kind: "start" })],
    [{ id: "p1", title: "同事", track: "工作", startMs: at(2023, 3, 1), endMs: null, ongoing: true, lastMs: at(2025, 9, 1), whenText: "2023年3月" }],
    { earliestMs: at(2016, 4, 1) });
  const { host, calls } = draw(Timeline, value, { tracks: [{ name: "我们" }, { name: "工作", description: "工作上的事" }], selectedTrack: "工作" });
  assert.equal(byClass(host, "tl-dots")[0].className, "tl-dots");
  const labels = byClass(host, "tl-track-label");
  assert.deepEqual(labels.map(label => label.textContent), ["我们", "工作"]);
  assert.ok(labels[0].className.includes("dim"));
  for (const callback of labels[1].listeners.click) callback({});
  const [bar] = byClass(host, "tl-dots-period");
  assert.ok(percent(bar.style.width) > 0, "an ongoing period runs to its last mention");
  for (const callback of bar.listeners.click) callback({});
  assert.deepEqual(calls, [["track", "工作"], ["period", "p1"]]);
  const years = byClass(host, "tl-year-label").map(label => label.textContent);
  assert.ok(years.includes("…"), years.join(","));
  assert.equal(byClass(host, "tl-year-label").find(label => label.textContent === "2016").className, "tl-year-label chat-start");
});

test("the chat line is normalised to the busiest column, starts with the chat and breaks at folded spans", () => {
  const { Timeline } = load();
  const value = view([event("a", "认识", at(2016, 5, 1)), event("b", "聚会", at(2024, 3, 1))], [], { earliestMs: at(2016, 4, 1), latestMs: at(2026, 5, 1) });
  const frequency = [{ month: "2015-12", count: 99 }, { month: "2016-04", count: 10 }, { month: "2016-05", count: 30 }, { month: "2024-03", count: 20 },
    { month: "2025-01", count: 5 }];
  const { host } = draw(Timeline, value, { frequency });
  const lines = byClass(host, "tl-freq")[0].children;
  assert.equal(lines.length, 2, "the folded 2017–2023 gap splits the line");
  const ys = lines.flatMap(line => line.attributes.points.split(" ").map(point => Number(point.split(",")[1])));
  assert.equal(Math.min(...ys), 1, "the busiest column touches the top");
  assert.ok(ys.every(y => y <= 25));
  const label = byClass(host, "tl-year-label").find(item => item.textContent === "2016");
  assert.match(label.title, /这一年聊了 40 条/);
  assert.equal(byClass(host, "tl-year-label").find(item => item.textContent === "2025").title, "这一年聊了 5 条");
});

test("one or two events still draw a short axis and nothing but chat frequency draws no chart", () => {
  const { Timeline } = load();
  const one = draw(Timeline, view([event("a", "第一次见面", at(2025, 10, 1))], [], { earliestMs: at(2025, 8, 1) }));
  assert.deepEqual(byClass(one.host, "tl-year-label").map(label => label.textContent), ["2025", "2026"]);
  assert.equal(byClass(one.host, "tl-dot").length, 1);
  const none = draw(Timeline, view([event("x", "取消了", at(2025, 1, 1), { status: "cancelled" })]), { frequency: [{ month: "2025-01", count: 3 }] });
  assert.equal(none.drawn, false); assert.equal(none.host.hidden, true); assert.equal(none.host.children.length, 0);
  const memory = draw(Timeline, view([]), { memoryRefs: [{ id: "m1", title: "一起看展", startMs: at(2024, 5, 1), precision: "month", whenText: "2024年5月" }] });
  assert.equal(memory.drawn, true);
  assert.ok(byClass(memory.host, "tl-dot")[0].className.includes("memory"));
  // An event's own linked ``memory`` object is not a 关系记忆 dot; only memoryRefs are, flagged ``memoryRef``.
  const linked = draw(Timeline, view([event("a", "一起吃饭", at(2024, 5, 1), { memory: { id: "x", text: "合成记忆" } })]));
  assert.ok(!byClass(linked.host, "tl-dot")[0].className.includes("memory"));
  const seen = [];
  const refs = draw(Timeline, view([]), { onEvent: item => seen.push(item),
    memoryRefs: [{ id: "memory:m1", title: "一起看展", startMs: at(2024, 5, 1), precision: "month", readOnly: true }] });
  for (const callback of byClass(refs.host, "tl-dot")[0].listeners.click) callback({});
  assert.equal(seen[0].id, "memory:m1"); assert.equal(seen[0].memoryRef, true);
});

test("typed dates and the stored-time text round trip", () => {
  const { Timeline } = load();
  const parsed = Timeline.parseWhenInput("2024年5月");
  assert.equal(parsed.precision, "month");
  assert.equal(Timeline.whenInput(parsed.startMs, parsed.precision), "2024-05");
  assert.equal(Timeline.parseWhenInput("1969"), null);
  assert.equal(Timeline.parseWhenInput("2023.02.30"), null);
  assert.equal(Timeline.bucketName(Timeline.bucket(at(2024, 8, 1), 3), 3), "2024 第三季度");
});
