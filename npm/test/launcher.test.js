'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const L = require('../lib/launcher');

const ROOT = path.join(__dirname, '..', '..');
const pkg = require('../package.json');

function existsIn(files) {
  const set = new Set(files);
  return (f) => set.has(f);
}

test('uv on PATH is found first, with uvx beside it', () => {
  const found = L.findUv({
    env: { PATH: '/a:/b' },
    platform: 'linux',
    home: '/home/doc',
    exists: existsIn(['/b/uv', '/b/uvx', '/home/doc/.local/bin/uv']),
  });
  assert.deepEqual(found, { uv: '/b/uv', uvx: '/b/uvx' });
});

test('uv in the installer default folder is found when PATH is bare (Claude Desktop on a Mac)', () => {
  const found = L.findUv({
    env: { PATH: '/usr/bin:/bin' },
    platform: 'darwin',
    home: '/Users/doc',
    exists: existsIn(['/Users/doc/.local/bin/uv', '/Users/doc/.local/bin/uvx']),
  });
  assert.deepEqual(found, { uv: '/Users/doc/.local/bin/uv', uvx: '/Users/doc/.local/bin/uvx' });
});

test('Homebrew and cargo locations are searched', () => {
  const dirs = L.uvDirs({ env: { PATH: '' }, platform: 'darwin', home: '/Users/doc' });
  assert.ok(dirs.includes('/opt/homebrew/bin'));
  assert.ok(dirs.includes('/Users/doc/.cargo/bin'));
  assert.ok(dirs.indexOf('/Users/doc/.local/bin') < dirs.indexOf('/opt/homebrew/bin'));
});

test('UV_INSTALL_DIR and XDG_BIN_HOME are honoured', () => {
  const dirs = L.uvDirs({
    env: { PATH: '', UV_INSTALL_DIR: '/opt/uv', XDG_BIN_HOME: '/x/bin' },
    platform: 'linux',
    home: '/h',
  });
  assert.deepEqual(dirs.slice(0, 2), ['/opt/uv', '/x/bin']);
});

test('Windows: .exe names, Path variable and the WinGet links folder', () => {
  const env = {
    Path: 'C:\\Windows\\System32',
    LOCALAPPDATA: 'C:\\Users\\doc\\AppData\\Local',
    USERPROFILE: 'C:\\Users\\doc',
  };
  const winget = 'C:\\Users\\doc\\AppData\\Local\\Microsoft\\WinGet\\Links\\uv.exe';
  const found = L.findUv({ env, platform: 'win32', home: 'C:\\Users\\doc', exists: existsIn([winget]) });
  assert.deepEqual(found, { uv: winget, uvx: null });
  const local = L.findUv({
    env,
    platform: 'win32',
    home: 'C:\\Users\\doc',
    exists: existsIn(['C:\\Users\\doc\\.local\\bin\\uv.exe', 'C:\\Users\\doc\\.local\\bin\\uvx.exe']),
  });
  assert.equal(local.uvx, 'C:\\Users\\doc\\.local\\bin\\uvx.exe');
});

test('no uv anywhere returns null', () => {
  assert.equal(L.findUv({ env: { PATH: '/a' }, platform: 'linux', home: '/h', exists: () => false }), null);
});

test('the command pins the PyPI release, PyPI index and Python, and defaults to serve', () => {
  const { command, args } = L.buildCommand({ uv: '/b/uv', uvx: '/b/uvx' }, '1.2.3');
  assert.equal(command, '/b/uvx');
  assert.deepEqual(args, [
    '--no-config',
    '--default-index',
    'https://pypi.org/simple',
    '--python',
    '3.12',
    '--from',
    'health-data-registry==1.2.3',
    'health-data-registry',
    'serve',
  ]);
});

test('without uvx it falls back to `uv tool run` with the same arguments', () => {
  const withUvx = L.buildCommand({ uv: '/b/uv', uvx: '/b/uvx' }, '1.2.3');
  const without = L.buildCommand({ uv: '/b/uv', uvx: null }, '1.2.3');
  assert.equal(without.command, '/b/uv');
  assert.deepEqual(without.args, ['tool', 'run', ...withUvx.args]);
});

test('version specs that are not plain releases are refused', () => {
  assert.equal(L.pypiSpec('0.0.1'), 'health-data-registry==0.0.1');
  assert.equal(L.pypiSpec('1.0.0rc1'), 'health-data-registry==1.0.0rc1');
  for (const bad of ['latest', '1.0', '>=1.0.0', '1.0.0; rm -rf /', '1.0.0 @ https://x']) {
    assert.throws(() => L.pypiSpec(bad), /not a release version/);
  }
});

test('launcher flags come first; anything after goes to the server', () => {
  assert.deepEqual(L.parseArgs([]), { installUv: false, printCommand: false, help: false, server: ['serve'] });
  const o = L.parseArgs(['--install-uv', 'refresh', '--all', '--install-uv']);
  assert.equal(o.installUv, true);
  assert.deepEqual(o.server, ['refresh', '--all', '--install-uv']);
});

test('installing uv is opt-in only', () => {
  assert.equal(L.wantsInstall(L.parseArgs([]), {}), false);
  assert.equal(L.wantsInstall(L.parseArgs([]), { HDR_INSTALL_UV: 'false' }), false);
  assert.equal(L.wantsInstall(L.parseArgs([]), { HDR_INSTALL_UV: '1' }), true);
  assert.equal(L.wantsInstall(L.parseArgs([]), { HDR_INSTALL_UV: 'true' }), true);
  assert.equal(L.wantsInstall(L.parseArgs(['--install-uv']), {}), true);
});

test('the server gets the environment, minus settings that change the package source', () => {
  const env = L.childEnv({ HDR_HUB: 'hf://datasets/o/n', HDR_LAKE: '/l', UV_INDEX_URL: 'https://evil', UV_EXTRA_INDEX_URL: 'x' });
  assert.deepEqual(env, { HDR_HUB: 'hf://datasets/o/n', HDR_LAKE: '/l' });
});

test('the missing-uv message is plain words with the official installer line', () => {
  const mac = L.missingUvMessage('darwin');
  assert.match(mac, /curl -LsSf https:\/\/astral\.sh\/uv\/install\.sh \| sh/);
  assert.match(L.missingUvMessage('win32'), /irm https:\/\/astral\.sh\/uv\/install\.ps1 \| iex/);
  assert.doesNotMatch(mac, /Error|ENOENT|stack/);
});

function fakeFetch(routes) {
  const seen = [];
  const impl = async (url) => {
    seen.push(url);
    const r = routes[url];
    if (!r) throw new Error(`unexpected fetch ${url}`);
    return {
      status: r.status,
      ok: r.status >= 200 && r.status < 300,
      headers: { get: (h) => (h === 'location' ? r.location : null) },
      text: async () => r.body,
    };
  };
  impl.seen = seen;
  return impl;
}

test('the installer download follows redirects within astral.sh only', async () => {
  const ok = fakeFetch({
    'https://astral.sh/uv/install.sh': { status: 301, location: 'https://releases.astral.sh/installers/uv/latest/uv-installer.sh' },
    'https://releases.astral.sh/installers/uv/latest/uv-installer.sh': { status: 200, body: '#!/bin/sh\n' },
  });
  assert.equal(await L.fetchInstaller(L.INSTALLER.posix, ok), '#!/bin/sh\n');
  const evil = fakeFetch({ 'https://astral.sh/uv/install.sh': { status: 302, location: 'https://example.com/x.sh' } });
  await assert.rejects(L.fetchInstaller(L.INSTALLER.posix, evil), /refusing/);
  assert.deepEqual(evil.seen, ['https://astral.sh/uv/install.sh']);
  await assert.rejects(L.fetchInstaller('http://astral.sh/uv/install.sh', ok), /refusing/);
});

test('package.json has no install-time scripts', () => {
  for (const hook of ['preinstall', 'install', 'postinstall', 'prepare', 'prepublish']) {
    assert.equal(pkg.scripts?.[hook], undefined, hook);
  }
  assert.equal(pkg.dependencies, undefined);
});

test('npm, PyPI, MCP Registry and .mcpb versions and names stay in sync', () => {
  const pyproject = fs.readFileSync(path.join(ROOT, 'pyproject.toml'), 'utf8');
  const pyVersion = /^version\s*=\s*"([^"]+)"/m.exec(pyproject)[1];
  assert.equal(pkg.version, pyVersion);
  const server = JSON.parse(fs.readFileSync(path.join(ROOT, 'packaging', 'server.json'), 'utf8'));
  assert.equal(server.name, pkg.mcpName);
  assert.equal(server.version, pkg.version);
  for (const p of server.packages) assert.equal(p.version, pkg.version);
  const manifest = JSON.parse(fs.readFileSync(path.join(ROOT, 'packaging', 'mcpb', 'manifest.json'), 'utf8'));
  assert.equal(manifest.version, pkg.version);
  assert.equal(manifest.name, pkg.name);
});

test('Windows installs uv through the checked download, never irm | iex', async () => {
  const ran = [];
  const runImpl = async (cmd, args, stdin) => ran.push({ cmd, args, stdin });
  const ok = fakeFetch({ 'https://astral.sh/uv/install.ps1': { status: 200, body: 'Write-Output uv' } });
  await L.installUv({ platform: 'win32', fetchImpl: ok, runImpl });
  assert.deepEqual(ok.seen, ['https://astral.sh/uv/install.ps1']);
  assert.equal(ran[0].cmd, 'powershell');
  assert.equal(ran[0].args.at(-1), '-');
  assert.equal(ran[0].stdin, 'Write-Output uv');
  assert.ok(!ran[0].args.join(' ').includes('irm'));
  const evil = fakeFetch({ 'https://astral.sh/uv/install.ps1': { status: 302, location: 'https://evil.example/x.ps1' } });
  await assert.rejects(L.installUv({ platform: 'win32', fetchImpl: evil, runImpl }), /refusing/);
  assert.equal(ran.length, 1);
});
