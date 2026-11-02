"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 关系趋势 arithmetic: buckets the bridge's per-message facts into local-time periods.
// Pure functions only, so the same code runs in the window and in Node tests.
(function (root) {
  const UNITS = ["minute", "hour", "day", "week", "month", "year"];
  const METRICS = ["messages", "turns", "chars", "reply"];
  const TURN = 1, START = 2, MIN_REPLY_SAMPLES = 3;
  // Reply speed bands: within a minute, within 10 minutes, within an hour, longer (up to 8 hours).
  const REPLY_BANDS = [60, 600, 3600];
  const { start, next, lower, days, axisLabel, title, labelRule } = root.KyfTimeTrend.calendar;

  function decode(data) {
    const columns = data.messages, count = columns.time.length;
    const time = new Float64Array(count);
    let total = 0;
    for (let index = 0; index < count; index++) time[index] = total += columns.time[index];
    const replyIndex = new Int32Array(data.replies.length), replySeconds = new Float64Array(data.replies.length);
    data.replies.forEach(([index, seconds], position) => { replyIndex[position] = index; replySeconds[position] = seconds; });
    return { time, side: columns.side, chars: columns.chars, kind: columns.kind, flags: columns.flags,
      replyIndex, replySeconds, kinds: data.kinds, count };
  }

  const tenth = value => Math.round(value * 10) / 10;
  function median(values) {
    if (values.length < MIN_REPLY_SAMPLES) return null;
    const sorted = [...values].sort((left, right) => left - right), middle = sorted.length >> 1;
    return tenth(sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2);
  }
  function mean(values) {
    if (values.length < MIN_REPLY_SAMPLES) return null;
    let total = 0;
    for (const value of values) total += value;
    return tenth(total / values.length);
  }

  // Every period of ``unit`` from the one holding fromMs up to the in-progress one (or toMs).
  // ``bounds`` holds each period start plus the last period's end. ``reply`` is each side's mean
  // wait, NaN below three replies: the mean, unlike the median, shows who keeps the other waiting.
  function series(facts, unit, { fromMs, toMs = Infinity, nowMs }) {
    const starts = [];
    for (let at = start(fromMs, unit); at < toMs && at <= nowMs; at = next(at, unit)) starts.push(at);
    if (!starts.length) starts.push(start(Math.min(fromMs, nowMs), unit));
    const count = starts.length, bounds = new Float64Array(count + 1);
    bounds.set(starts);
    bounds[count] = next(starts[count - 1], unit);
    const make = () => [new Float64Array(count), new Float64Array(count)];
    const values = { messages: make(), turns: make(), chars: make(), reply: make() };
    const replies = make();
    let period = 0;
    const first = lower(facts.time, bounds[0]), last = lower(facts.time, bounds[count]);
    for (let index = first; index < last; index++) {
      const at = facts.time[index];
      while (at >= bounds[period + 1]) period++;
      const side = facts.side[index];
      values.messages[side][period]++;
      if (facts.flags[index] & TURN) values.turns[side][period]++;
      values.chars[side][period] += facts.chars[index];
    }
    period = 0;
    for (let position = lower(facts.replyIndex, first); position < facts.replyIndex.length; position++) {
      const index = facts.replyIndex[position];
      if (index >= last) break;
      while (facts.time[index] >= bounds[period + 1]) period++;
      const side = facts.side[index];
      values.reply[side][period] += facts.replySeconds[position];
      replies[side][period]++;
    }
    for (const side of [0, 1])
      for (let index = 0; index < count; index++)
        values.reply[side][index] = replies[side][index] >= MIN_REPLY_SAMPLES ?
          tenth(values.reply[side][index] / replies[side][index]) : NaN;
    return { unit, count, bounds, values, inProgress: bounds[count] > nowMs };
  }

  // Everything the cards show for [startMs, endMs): one period or a selected range of periods.
  // Contact days are reported whenever the span reaches past its first day.
  function summary(facts, startMs, endMs) {
    const first = lower(facts.time, startMs), last = lower(facts.time, endMs);
    const pair = () => [0, 0];
    const result = { messages: pair(), turns: pair(), chars: pair(), starts: pair(),
      kinds: [facts.kinds.map(() => 0), facts.kinds.map(() => 0)], replySamples: pair(),
      replyMeanSeconds: [null, null], replyMedianSeconds: [null, null], replyLongestSeconds: [null, null],
      replyBands: [REPLY_BANDS.map(() => 0).concat(0), REPLY_BANDS.map(() => 0).concat(0)],
      contactDays: null, twoWayDays: null };
    let day = null, mask = 0, contact = 0, twoWay = 0;
    const closeDay = () => { if (mask) { contact++; if (mask === 3) twoWay++; } };
    for (let index = first; index < last; index++) {
      const side = facts.side[index];
      result.messages[side]++;
      if (facts.flags[index] & TURN) result.turns[side]++;
      if (facts.flags[index] & START) result.starts[side]++;
      result.chars[side] += facts.chars[index];
      result.kinds[side][facts.kind[index]]++;
      const today = start(facts.time[index], "day");
      if (today !== day) { closeDay(); day = today; mask = 0; }
      mask |= side ? 2 : 1;
    }
    closeDay();
    if (endMs > next(start(startMs, "day"), "day")) Object.assign(result, { contactDays: contact, twoWayDays: twoWay });
    const lists = [[], []];
    for (let position = lower(facts.replyIndex, first); position < facts.replyIndex.length && facts.replyIndex[position] < last; position++)
      lists[facts.side[facts.replyIndex[position]]].push(facts.replySeconds[position]);
    for (const side of [0, 1]) {
      result.replySamples[side] = lists[side].length;
      result.replyMeanSeconds[side] = mean(lists[side]);
      result.replyMedianSeconds[side] = median(lists[side]);
      for (const value of lists[side]) {
        result.replyLongestSeconds[side] = Math.max(result.replyLongestSeconds[side] ?? 0, value);
        const band = REPLY_BANDS.findIndex(limit => value < limit);
        result.replyBands[side][band < 0 ? REPLY_BANDS.length : band]++;
      }
    }
    return result;
  }

  // Default selection: the newest period that has messages, else the newest period.
  function latestWithMessages(view) {
    for (let index = view.count - 1; index >= 0; index--)
      if (view.values.messages[0][index] + view.values.messages[1][index] > 0) return index;
    return view.count - 1;
  }
  function firstWithMessages(view, from = 0, to = view.count) {
    for (let index = from; index < to; index++)
      if (view.values.messages[0][index] + view.values.messages[1][index] > 0) return index;
    return from;
  }

  root.FriendTrendMath = { UNITS, METRICS, TURN, START, REPLY_BANDS, decode, start, next, lower, median, mean, days, series, summary,
    latestWithMessages, firstWithMessages, axisLabel, title, labelRule };
})(typeof window !== "undefined" ? window : globalThis);
