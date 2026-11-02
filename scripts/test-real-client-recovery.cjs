// Synthetic recovery checks: no bridge, Python process, or socket is opened.
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'real-client-recovery.cjs'), 'utf8');
const { pythonFlags } = require('./real-client-recovery.cjs');
const ROOT = path.join(__dirname, 'synthetic root');
const LAUNCHER = path.join(ROOT, 'scripts', 'start-real-client.py');
const BUNDLED_PYTHON = path.join(ROOT, 'runtime', 'python', 'python.exe');

function fixture(markerPresent, healthMode = 'offline', python = 'python') {
  const calls = [];
  let tick;
  let recovered = 0;
  const fakeFs = {
    existsSync: target => target.endsWith('no-auto-recovery.json') ? markerPresent : target === python,
  };
  const instanceId = 'a'.repeat(64);
  const fakeHttp = {
    get: (options, callback) => {
      assert.equal(options.path, '/api/health');
      assert.equal(options.port, 54321);
      const request = new EventEmitter();
      request.destroy = () => {};
      queueMicrotask(() => {
        if (healthMode === 'offline') return request.emit('error', new Error('synthetic offline'));
        const response = new EventEmitter();
        response.statusCode = 200;
        callback(response);
        response.emit('data', JSON.stringify({
          version: 'real-ui-1', instanceId: healthMode === 'matching' ? instanceId : 'b'.repeat(64),
        }));
        response.emit('end');
      });
      return request;
    },
  };
  const fakeChild = {
    execFile: (_python, args, _options, done) => calls.push({ args, done }),
  };
  const module = { exports: {} };
  vm.runInNewContext(source, {
    module,
    require: name => ({ 'node:child_process': fakeChild, 'node:fs': fakeFs,
      'node:http': fakeHttp, 'node:path': path })[name],
    process: { env: { KYF_PYTHON: python } },
    URL,
    setInterval: callback => { tick = callback; return { unref() {} }; },
    clearInterval: () => {},
  });
  const stop = module.exports.monitorBridge({
    root: ROOT,
    url: 'http://127.0.0.1:54321',
    instanceId,
    isOpen: () => true,
    onRecovered: () => { recovered += 1; },
  });
  return { calls, tick: () => tick(), stop,
    setMarker: value => { markerPresent = value; },
    recovered: () => recovered };
}

async function flush() {
  await new Promise(resolve => setImmediate(resolve));
}

async function main() {
  const blocked = fixture(true);
  await flush();
  assert.equal(blocked.calls.length, 0);
  blocked.setMarker(false);
  blocked.tick();
  await flush();
  assert.equal(blocked.calls.length, 1);
  assert.deepEqual(Array.from(blocked.calls[0].args).slice(-3), ['--no-open', '--recovery', '--json']);
  blocked.setMarker(true);
  blocked.calls[0].done(null);
  await flush();
  assert.equal(blocked.recovered(), 0);
  blocked.stop();

  const available = fixture(false);
  await flush();
  assert.equal(available.calls.length, 1);
  assert.deepEqual(Array.from(available.calls[0].args), [LAUNCHER, '--no-open', '--recovery', '--json']);
  available.calls[0].done(null, '{"ok":true,"created":true}');
  await flush();
  assert.equal(available.recovered(), 1);
  available.stop();

  // Only the bundled runtime runs with -s, so it never loads the user's own Python packages.
  const bundled = fixture(false, 'offline', BUNDLED_PYTHON);
  await flush();
  assert.equal(bundled.calls.length, 1);
  assert.deepEqual(Array.from(bundled.calls[0].args), ['-s', LAUNCHER, '--no-open', '--recovery', '--json']);
  bundled.stop();
  assert.deepEqual(pythonFlags(ROOT, BUNDLED_PYTHON.toUpperCase()), ['-s']);
  assert.deepEqual(pythonFlags(ROOT, path.join(ROOT, 'runtime', 'python', '..', 'python', 'python.exe')), ['-s']);
  assert.deepEqual(pythonFlags(ROOT, path.join(ROOT, '.venv', 'Scripts', 'python.exe')), []);
  assert.deepEqual(pythonFlags(ROOT, path.join(__dirname, 'other root', 'runtime', 'python', 'python.exe')), []);
  assert.deepEqual(pythonFlags(ROOT, 'python'), []);

  const reused = fixture(false);
  await flush();
  assert.equal(reused.calls.length, 1);
  reused.calls[0].done(null, '{"ok":true,"created":false}');
  await flush();
  assert.equal(reused.recovered(), 0, 'a reused bridge must not report recovery');
  reused.stop();

  const matching = fixture(false, 'matching');
  await flush();
  assert.equal(matching.calls.length, 0);
  matching.stop();

  const wrong = fixture(false, 'wrong');
  await flush();
  assert.equal(wrong.calls.length, 1);
  wrong.calls[0].done(new Error('foreign bridge occupies port'));
  await flush();
  assert.equal(wrong.recovered(), 0);
  wrong.stop();

  const draining = fixture(false);
  await flush();
  assert.equal(draining.calls.length, 1);
  let finished = false;
  const drained = draining.stop().then(() => { finished = true; });
  await flush();
  assert.equal(finished, false, 'quit must wait for an in-flight recovery launcher');
  draining.calls[0].done(null);
  await drained;
  assert.equal(finished, true);
  assert.equal(draining.recovered(), 0, 'a stopped monitor must not report recovery');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
