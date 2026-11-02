// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// The MBTI headline on the 性格分析 card: a partly known Big Five is not a type, so the
// self-report must win instead of a "?N??" placeholder standing in for it.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function charts() {
  const source = readFileSync(path.join(__dirname, '../plugin-sdk/web/persona-charts.js'), 'utf8');
  const sandbox = {};
  vm.runInNewContext(source, sandbox);
  return sandbox.KyfPersonaCharts;
}

const scale = (position, confidence = '较有把握') => ({ position, confidence });
const dimsWith = entries => Object.fromEntries(entries.map(([key, position]) => [key, scale(position)]));

test('a fully known Big Five still derives a type', () => {
  const { mbti } = charts();
  const type = mbti(dimsWith([['big5_e', -0.4], ['big5_o', 0.8], ['big5_a', 0.3], ['big5_c', -0.2]]), null);
  assert.ok(type, 'a complete scale derives a type');
  assert.equal(type.text.length, 4);
  assert.match(type.text, /^[EINSFTJP]+$/);
});

test('a partly known Big Five derives no type', () => {
  const { mbti } = charts();
  // Only 开放性 is known: the other three letters would be "?", so "?N??" is not a type.
  assert.equal(mbti(dimsWith([['big5_o', 0.8]]), null), null);
  assert.equal(mbti(dimsWith([['big5_e', -0.4], ['big5_o', 0.8]]), null), null);
});

test('the self-report stands in for a type that cannot be derived', () => {
  const { mbti } = charts();
  const dims = dimsWith([['big5_o', 0.8]]);
  const self = mbti(dims, 'INFJ');
  assert.equal(self.text, 'INFJ');
  assert.equal(self.title, '本人自述');
  // The card reads the derived type first and falls back to the self-report.
  assert.equal((mbti(dims, null) || mbti(dims, 'INFJ')).text, 'INFJ');
});

test('a dimension without evidence contributes no letter', () => {
  const { mbti } = charts();
  assert.equal(mbti(dimsWith([['big5_o', 0.8], ['big5_e', 0]]), null), null);
});
