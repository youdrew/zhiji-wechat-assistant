// Synthetic desktop startup checks: no Electron window or bridge is launched.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'desktop-main.cjs'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

async function launch(isPackaged, stdout, options = {}) {
  const calls = { errors: [], launches: [], shellLoads: 0, profiles: [], quit: 0, sidecars: [], output: [], exits: [] };
  const env = { CHATUI_PORT: '8805', PATH: 'synthetic-path', ...options.env };
  const processStub = {
    env, argv: options.argv || [], resourcesPath: path.join(__dirname, 'synthetic-resources'),
    stdout: { write: (line, done) => { calls.output.push(JSON.parse(line)); done(); } },
  };
  const app = {
    isPackaged,
    setPath: (name, value) => calls.profiles.push({ name, value }),
    whenReady: () => Promise.resolve(),
    quit: () => { calls.quit += 1; },
    exit: code => calls.exits.push(code),
  };
  const fakeFs = { existsSync: () => true, mkdirSync: () => {} };
  const fakeChild = {
    execFile: (python, args, execOptions, done) => {
      calls.launches.push({ python, args, options: execOptions });
      queueMicrotask(() => done(options.execError || null, stdout));
    },
  };
  // Match the CommonJS wrapper: the packaged sidecar may return without starting the desktop.
  vm.runInNewContext(`(function () {\n${source}\n})()`, {
    require: name => {
      if (name === 'electron') return { app, dialog: { showErrorBox: (...args) => calls.errors.push(args) } };
      if (name === 'node:child_process') return fakeChild;
      if (name === 'node:fs') return fakeFs;
      if (name === 'node:path') return path;
      if (name === './real-client-recovery.cjs') return require('./real-client-recovery.cjs');
      if (name.startsWith(path.join(clientRoot(true), 'scripts') + path.sep)) {
        calls.sidecars.push(name);
        return {};
      }
      if (name === './real-client-shell.cjs') {
        calls.shellLoads += 1;
        if (options.shellThrows) throw new Error('synthetic shell init failure');
        return {};
      }
      throw new Error(`Unexpected import: ${name}`);
    },
    process: processStub, __dirname,
  });
  await tick();
  await tick();
  return { calls, processStub };
}

function validResult(created) {
  return JSON.stringify({ version: 'real-ui-1', url: 'http://127.0.0.1:34567',
    instanceId: 'a'.repeat(64), created });
}

function clientRoot(isPackaged) {
  return isPackaged ? path.join(__dirname, 'synthetic-resources', 'client') : path.resolve(__dirname, '..');
}

function cleanupLaunches(calls) {
  return calls.launches.filter(call => call.args.includes('--stop-owned-bridge'));
}

async function main() {
  for (const kind of ['ui-host', 'pairing-window', 'confirm-window']) {
    const args = kind === 'ui-host' ? ['--client-url', 'http://127.0.0.1:34567/'] : [];
    const { calls, processStub } = await launch(true, '', { argv: ['KnowYourFriends.exe', '--kyf-sidecar', kind, ...args] });
    const entry = path.join(clientRoot(true), 'scripts', `${kind}.cjs`);
    assert.deepEqual(calls.sidecars, [entry]);
    assert.deepEqual(processStub.argv.slice(1), [entry, ...args]);
    assert.equal(calls.launches.length, 0);
    assert.equal(calls.profiles.length, 0);
  }
  for (const bad of [['../arbitrary'], ['pairing-window', '--user-data', 'elsewhere'],
    ['ui-host', '--client-url', 'http://example.invalid:34567/'], ['ui-host', '--client-url', 'http://127.0.0.1:65536/']]) {
    const { calls } = await launch(true, '', { argv: ['KnowYourFriends.exe', '--kyf-sidecar', ...bad] });
    assert.deepEqual(calls.sidecars, []);
    assert.deepEqual(calls.exits, [2]);
    assert.equal(calls.output[0].code, 'sidecar-unavailable');
    assert.equal(calls.launches.length, 0);
  }

  const instanceId = 'a'.repeat(64);
  // Core hands a running GUI a module: the shell starts at once, without the launcher.
  for (const extra of [[], ['--open-module', 'settings']]) {
    const args = ['--client-url', 'http://127.0.0.1:34567/', ...extra];
    const { calls, processStub } = await launch(true, '', {
      argv: ['KnowYourFriends.exe', '--kyf-shell', ...args], env: { KYF_INSTANCE_ID: instanceId } });
    assert.equal(calls.launches.length, 0);
    assert.equal(calls.shellLoads, 1);
    assert.deepEqual(processStub.argv.slice(1), args);
    assert.equal(processStub.env.CHATUI_PORT, '34567');
    assert.equal(processStub.env.KYF_BRIDGE_CREATED, '0');
    assert.equal(processStub.env.KYF_CLIENT_ROOT, clientRoot(true));
    assert.equal(calls.profiles[0].name, 'userData');
    assert.deepEqual(calls.exits, []);
  }
  for (const [argv, env] of [
    [['--client-url', 'http://example.invalid:34567/'], { KYF_INSTANCE_ID: instanceId }],
    [['--client-url', 'http://127.0.0.1:34567/', '--open-module', '../x'], { KYF_INSTANCE_ID: instanceId }],
    [['--client-url', 'http://127.0.0.1:34567/', '--user-data', 'x'], { KYF_INSTANCE_ID: instanceId }],
    [['--client-url', 'http://127.0.0.1:34567/', '--open-module', 'chat', '--chat-account=a', '--chat-user=u'], { KYF_INSTANCE_ID: instanceId }],
    [['--client-url', 'http://127.0.0.1:34567/'], {}]]) {
    const { calls } = await launch(true, '', { argv: ['KnowYourFriends.exe', '--kyf-shell', ...argv], env });
    assert.equal(calls.shellLoads, 0);
    assert.equal(calls.launches.length, 0);
    assert.deepEqual(calls.exits, [2]);
  }
  // Only the installed build has this entry; development keeps the normal launch.
  const devShell = await launch(false, validResult(false), { argv: ['electron', '--kyf-shell'] });
  assert.equal(devShell.calls.launches.length, 1);

  for (const isPackaged of [false, true]) {
    const { calls, processStub } = await launch(isPackaged, validResult(true));
    assert.equal(calls.launches.length, 1);
    assert.equal(calls.launches[0].options.env.CHATUI_PORT, undefined);
    // The bundled runtime runs with -s, so it never loads the user's own Python packages.
    const root = clientRoot(isPackaged);
    assert.equal(calls.launches[0].python, path.join(root, 'runtime', 'python', 'python.exe'));
    assert.deepEqual(Array.from(calls.launches[0].args),
      ['-s', path.join(root, 'scripts', 'start-real-client.py'), '--no-open', '--json']);
    assert.equal(processStub.env.CHATUI_PORT, '34567');
    assert.equal(processStub.env.KYF_INSTANCE_ID, instanceId);
    assert.equal(processStub.env.KYF_BRIDGE_CREATED, '1');
    assert.deepEqual(Array.from(processStub.argv).slice(-2), ['--client-url', 'http://127.0.0.1:34567/']);
    assert.equal(calls.shellLoads, 1);
    assert.equal(calls.errors.length, 0);
    assert.equal(calls.quit, 0);
    assert.equal(calls.profiles.length, 1);
    assert.equal(calls.profiles[0].name, 'userData');
  }

  // A development interpreter keeps its usual user site.
  const devPython = path.join(clientRoot(false), '.venv', 'Scripts', 'python.exe');
  const development = await launch(false, validResult(true), { env: { KYF_PYTHON: devPython } });
  assert.equal(development.calls.launches[0].python, devPython);
  assert.deepEqual(Array.from(development.calls.launches[0].args),
    [path.join(clientRoot(false), 'scripts', 'start-real-client.py'), '--no-open', '--json']);

  // A reused ready bridge is never stopped on a startup failure.
  const reused = await launch(true, validResult(false));
  assert.equal(reused.calls.shellLoads, 1);
  assert.equal(reused.processStub.env.KYF_BRIDGE_CREATED, '0');
  assert.equal(cleanupLaunches(reused.calls).length, 0);

  // An invalid launcher result only stops the bridge this launch created.
  const invalidOwned = await launch(true, JSON.stringify({ version: 'real-ui-1',
    url: 'http://localhost:34567', instanceId, created: true }));
  assert.equal(invalidOwned.calls.shellLoads, 0);
  assert.equal(invalidOwned.calls.errors.length, 1);
  assert.equal(invalidOwned.calls.quit, 1);
  const ownedCleanups = cleanupLaunches(invalidOwned.calls);
  assert.equal(ownedCleanups.length, 1);
  assert.deepEqual(Array.from(ownedCleanups[0].args), ['-s',
    path.join(clientRoot(true), 'scripts', 'start-real-client.py'), '--stop-owned-bridge', '--json']);
  assert.equal(ownedCleanups[0].options.env.KYF_CLIENT_ROOT,
    path.join(__dirname, 'synthetic-resources', 'client'));

  const invalidReused = await launch(true, JSON.stringify({ version: 'real-ui-1',
    url: 'http://localhost:34567', instanceId, created: false }));
  assert.equal(invalidReused.calls.errors.length, 1);
  assert.equal(cleanupLaunches(invalidReused.calls).length, 0);

  // Corrupt stdout that still says "created":true must not orphan the bridge.
  const corrupt = await launch(true, 'x "created": true y');
  assert.equal(corrupt.calls.errors.length, 1);
  assert.equal(corrupt.calls.quit, 1);
  assert.equal(cleanupLaunches(corrupt.calls).length, 1);
  const corruptReused = await launch(true, 'not json at all');
  assert.equal(cleanupLaunches(corruptReused.calls).length, 0);

  // The launcher cleans up a bridge it started before reporting an error, so the
  // desktop must not issue a second stop.
  const launcherFailed = await launch(true, '', { execError: new Error('launcher failed') });
  assert.equal(launcherFailed.calls.launches.length, 1);
  assert.equal(launcherFailed.calls.errors.length, 1);
  assert.equal(launcherFailed.calls.quit, 1);
  assert.equal(cleanupLaunches(launcherFailed.calls).length, 0);

  // A synchronous shell require failure stops only a bridge this launch created.
  const shellOwned = await launch(true, validResult(true), { shellThrows: true });
  assert.equal(shellOwned.calls.errors.length, 1);
  assert.equal(shellOwned.calls.quit, 1);
  assert.equal(cleanupLaunches(shellOwned.calls).length, 1);
  const shellReused = await launch(true, validResult(false), { shellThrows: true });
  assert.equal(shellReused.calls.errors.length, 1);
  assert.equal(cleanupLaunches(shellReused.calls).length, 0);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
