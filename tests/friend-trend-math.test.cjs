// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic tests for 关系趋势 bucketing: periods, metrics, reply medians and labels.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const context = { Math, Date, Map, Number, String, Float64Array, Int32Array, Infinity, NaN };
context.globalThis = context;
vm.createContext(context);
vm.runInContext(readFileSync(path.join(__dirname, "../plugin-sdk/web/time-trend.js"), "utf8"), context);
// Moved with the 关系趋势 page into the kyf.friend-analysis plugin (2026-10-13).
vm.runInContext(readFileSync(path.join(__dirname, "../plugins/kyf.friend-analysis/ui/friend-trend-math.js"), "utf8"),
  context);
const M = context.FriendTrendMath;
const KINDS = ["text", "image", "voice", "video", "sticker", "other"];

// Builds the bridge's column shape from readable rows: [Date, side, chars, kind, flags, replySeconds?].
function facts(rows) {
  const data = { kinds: KINDS, messages: { time: [], side: [], chars: [], kind: [], flags: [] }, replies: [] };
  let previous = 0;
  rows.forEach(([when, side, chars = 1, kind = "text", flags = M.TURN, reply], index) => {
    data.messages.time.push(when.getTime() - previous);
    previous = when.getTime();
    data.messages.side.push(side === "self" ? 0 : 1);
    data.messages.chars.push(chars);
    data.messages.kind.push(KINDS.indexOf(kind));
    data.messages.flags.push(flags);
    if (reply !== undefined) data.replies.push([index, reply]);
  });
  return M.decode(data);
}
const at = (...parts) => new Date(...parts);
const list = values => Array.from(values);

test("periods run from the first message to the in-progress one and sum each side", () => {
  const data = facts([
    [at(2026, 9, 1, 9), "other", 4], [at(2026, 9, 1, 9, 1), "other", 2, "text", 0], [at(2026, 9, 1, 9, 2), "self", 3],
    [at(2026, 9, 3, 22), "self", 0, "image"],
  ]);
  const now = at(2026, 9, 5, 12).getTime();
  const day = M.series(data, "day", { fromMs: data.time[0], nowMs: now });
  assert.equal(day.count, 5);
  assert.equal(day.inProgress, true);
  assert.deepEqual(list(day.values.messages[1]), [2, 0, 0, 0, 0]);
  assert.deepEqual(list(day.values.messages[0]), [1, 0, 1, 0, 0]);
  assert.deepEqual(list(day.values.turns[1]), [1, 0, 0, 0, 0], "a burst from one side is one turn");
  assert.deepEqual(list(day.values.chars[1]), [6, 0, 0, 0, 0]);
  assert.equal(M.latestWithMessages(day), 2);
  const hour = M.series(data, "hour", { fromMs: data.time[0], nowMs: now });
  assert.equal(hour.count, 4 * 24 + 3 + 1, "every hour from 09:00 on day one through the current hour");
  const year = M.series(data, "year", { fromMs: data.time[0], nowMs: now });
  assert.equal(year.count, 1);
});

test("a day opens as minutes, ending at the current minute when it is today", () => {
  const data = facts([[at(2026, 9, 1, 9, 30), "other"], [at(2026, 9, 1, 9, 31, 40), "self"]]);
  const dayStart = at(2026, 9, 1).getTime(), dayEnd = at(2026, 9, 2).getTime();
  const past = M.series(data, "minute", { fromMs: dayStart, toMs: dayEnd, nowMs: at(2026, 9, 5).getTime() });
  assert.equal(past.count, 1440);
  assert.equal(past.inProgress, false);
  assert.equal(M.firstWithMessages(past), 9 * 60 + 30);
  const today = M.series(data, "minute", { fromMs: dayStart, toMs: dayEnd, nowMs: at(2026, 9, 1, 10, 0, 30).getTime() });
  assert.equal(today.count, 10 * 60 + 1);
  assert.equal(today.inProgress, true);
});

test("reply means and medians need three samples and follow the replying side", () => {
  const data = facts([
    [at(2026, 9, 1, 9), "other"], [at(2026, 9, 1, 9, 1), "self", 1, "text", 1, 60],
    [at(2026, 9, 1, 9, 2), "other", 1, "text", 1, 60], [at(2026, 9, 1, 9, 5), "self", 1, "text", 1, 180],
    [at(2026, 9, 1, 9, 6), "other", 1, "text", 1, 60], [at(2026, 9, 1, 9, 16), "self", 1, "text", 1, 600],
    [at(2026, 9, 2, 9), "other", 1, "text", 3],
  ]);
  const day = M.series(data, "day", { fromMs: data.time[0], nowMs: at(2026, 9, 2, 12).getTime() });
  assert.equal(day.values.reply[0][0], 280, "the chart shows the mean: my replies 60, 180, 600 -> 280");
  assert.ok(Number.isNaN(day.values.reply[1][0]), "two replies are too few to show");
  assert.ok(Number.isNaN(day.values.reply[0][1]), "no replies is no bar, not zero");
  const one = M.summary(data, at(2026, 9, 1).getTime(), at(2026, 9, 2).getTime());
  assert.deepEqual(list(one.replySamples), [3, 2]);
  assert.deepEqual(list(one.replyMeanSeconds), [280, null]);
  assert.deepEqual(list(one.replyMedianSeconds), [180, null], "the median shows the live-chat pace");
  assert.deepEqual(list(one.replyLongestSeconds), [600, 60], "the longest wait needs no minimum");
  assert.deepEqual(Array.from(one.replyBands, list), [[0, 2, 1, 0], [0, 2, 0, 0]], "bands: <1m, 1-10m, 10-60m, longer");
  assert.equal(one.contactDays, null, "contact days are only reported for spans longer than a day");
});

test("a period summary counts kinds, conversation starts and shared days", () => {
  const data = facts([
    [at(2026, 9, 5, 9), "other", 3, "text", M.TURN | M.START], [at(2026, 9, 5, 9, 1), "self", 0, "sticker"],
    [at(2026, 9, 6, 9), "self", 0, "voice"], [at(2026, 9, 6, 9, 1), "self", 0, "voice", 0],
    [at(2026, 9, 7, 20), "other", 0, "image", M.TURN | M.START],
  ]);
  const week = M.summary(data, at(2026, 9, 5).getTime(), at(2026, 9, 12).getTime());
  assert.deepEqual(list(week.messages), [3, 2]);
  assert.deepEqual(list(week.turns), [2, 2]);
  assert.deepEqual(list(week.starts), [0, 2]);
  assert.deepEqual(list(week.kinds[0]), [0, 0, 2, 0, 1, 0]);
  assert.deepEqual(list(week.kinds[1]), [1, 1, 0, 0, 0, 0]);
  assert.deepEqual([week.contactDays, week.twoWayDays], [3, 1]);
  const twoDays = M.summary(data, at(2026, 9, 5).getTime(), at(2026, 9, 7).getTime());
  assert.deepEqual([twoDays.messages[0], twoDays.contactDays], [3, 2], "a selected range of days sums every day in it");
  assert.equal(M.days(at(2026, 8, 6).getTime(), at(2026, 9, 1).getTime()), 25, "Sep 6 to Sep 30 is 25 days");
  assert.equal(M.days(at(2026, 9, 1, 14).getTime(), at(2026, 9, 1, 18).getTime()), 1, "hours inside one day touch one day");
});

test("labels and titles name local periods plainly", () => {
  const ms = at(2026, 9, 11, 14, 5).getTime();
  assert.equal(M.axisLabel(M.start(ms, "minute"), "minute"), "14:05");
  assert.equal(M.axisLabel(M.start(ms, "hour"), "hour"), "14:00");
  assert.equal(M.axisLabel(M.start(ms, "day"), "hour"), "10-11", "midnight shows the date");
  assert.equal(M.axisLabel(M.start(ms, "week"), "week"), "2026-10-05");
  assert.equal(M.title(M.start(ms, "day"), M.next(M.start(ms, "day"), "day"), "day"), "2026-10-11 周日");
  const week = M.start(ms, "week");
  assert.equal(M.title(week, M.next(week, "week"), "week"), "2026-10-05 至 10-11");
  assert.equal(M.title(M.start(ms, "month"), 0, "month"), "2026 年 10 月");
  const hour = M.start(ms, "hour");
  assert.equal(M.title(hour, M.next(hour, "hour"), "hour"), "2026-10-11 14:00–15:00");
  assert.equal(M.title(at(2026, 8, 6).getTime(), at(2026, 9, 1).getTime(), "day"), "2026-09-06 至 09-30");
  assert.equal(M.title(at(2025, 10, 1).getTime(), at(2026, 2, 1).getTime(), "month"), "2025 年 11 月至 2026 年 2 月");
  assert.equal(M.title(at(2023, 0, 1).getTime(), at(2027, 0, 1).getTime(), "year"), "2023 至 2026 年");
  assert.equal(M.title(at(2026, 9, 11, 14).getTime(), at(2026, 9, 11, 18).getTime(), "hour"), "2026-10-11 14:00–18:00");
  assert.equal(M.title(at(2026, 9, 11, 22).getTime(), at(2026, 9, 12, 2).getTime(), "hour"), "2026-10-11 22:00 至 10-12 02:00");
  assert.equal(M.title(at(2026, 9, 11, 14, 5).getTime(), at(2026, 9, 11, 14, 31).getTime(), "minute"), "2026-10-11 14:05–14:31");
  const rule = M.labelRule("hour", 10, 44, 100);
  assert.equal(rule(0, at(2026, 9, 11, 6).getTime()), true);
  assert.equal(rule(0, at(2026, 9, 11, 7).getTime()), false);
});
