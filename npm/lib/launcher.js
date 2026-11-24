'use strict';
// Starts the health-data-registry MCP server (a Python program) for Claude, over stdio.
//
// What this file does, and nothing more:
//   1. finds uv (Astral's Python tool runner) on PATH or in its standard install folders;
//   2. if uv is missing, prints plain-words steps, or, only when asked with --install-uv
//      (or HDR_INSTALL_UV=1), runs Astral's official installer from astral.sh;
//   3. runs `uvx --from health-data-registry==<this package's version> health-data-registry serve`
//      with stdin, stdout, stderr and the environment passed straight through.
//
// The Python package comes only from PyPI (https://pypi.org/simple). Nothing runs at `npm install`.
// stdout belongs to the MCP protocol: every message from this file goes to stderr.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const PACKAGE = 'health-data-registry';
const PYTHON = '3.12';
const PYPI_INDEX = 'https://pypi.org/simple';
const INSTALLER = {
  posix: 'https://astral.sh/uv/install.sh',
  win32: 'https://astral.sh/uv/install.ps1',
};
// Hosts the installer URL may redirect to; anything else is refused.
const INSTALLER_HOSTS = new Set(['astral.sh', 'releases.astral.sh']);
// uv settings that could point the download at another index or mirror; dropped for the child.
const INDEX_ENV = [
  'UV_INDEX',
  'UV_INDEX_URL',
  'UV_DEFAULT_INDEX',
  'UV_EXTRA_INDEX_URL',
  'UV_FIND_LINKS',
  'UV_PYTHON_INSTALL_MIRROR',
  'UV_PYPY_INSTALL_MIRROR',
];

function pathFor(platform) {
  return platform === 'win32' ? path.win32 : path.posix;
}

function exe(name, platform) {
  return platform === 'win32' ? `${name}.exe` : name;
}

/** The exact PyPI requirement for this npm version, e.g. health-data-registry==0.0.1. */
function pypiSpec(version) {
  if (!/^\d+\.\d+\.\d+([.-]?(a|b|rc|post|dev)\.?\d+)?$/.test(String(version))) {
    throw new Error(`not a release version: ${version}`);
  }
  return `${PACKAGE}==${version}`;
}

/** Folders to look in for uv, in order: PATH first, then where uv's installers put it. */
function uvDirs({ env = process.env, platform = process.platform, home = os.homedir() } = {}) {
  const p = pathFor(platform);
  const dirs = [];
  const pathVar = platform === 'win32' ? env.Path || env.PATH || '' : env.PATH || '';
  for (const d of pathVar.split(p.delimiter)) if (d) dirs.push(d);
  if (env.UV_INSTALL_DIR) dirs.push(env.UV_INSTALL_DIR);
  if (env.XDG_BIN_HOME) dirs.push(env.XDG_BIN_HOME);
  if (env.XDG_DATA_HOME) dirs.push(p.join(env.XDG_DATA_HOME, '..', 'bin'));
  if (home) {
    dirs.push(p.join(home, '.local', 'bin')); // the official installer's default
    dirs.push(p.join(home, '.cargo', 'bin')); // older installers
  }
  if (platform === 'darwin') dirs.push('/opt/homebrew/bin', '/usr/local/bin');
  else if (platform === 'win32') {
    if (env.LOCALAPPDATA) dirs.push(p.join(env.LOCALAPPDATA, 'Microsoft', 'WinGet', 'Links'));
    if (env.USERPROFILE) dirs.push(p.join(env.USERPROFILE, '.local', 'bin'));
  } else dirs.push('/usr/local/bin', '/usr/bin', '/snap/bin');
  return [...new Set(dirs.map((d) => p.normalize(d)))];
}

function isFile(file) {
  try {
    return fs.statSync(file).isFile();
  } catch {
    return false;
  }
}

/** { uv, uvx } paths from the first folder holding uv (uvx may be null), or null if uv isn't found. */
function findUv({ env, platform = process.platform, home, exists = isFile } = {}) {
  const p = pathFor(platform);
  for (const dir of uvDirs({ env, platform, home })) {
    const uv = p.join(dir, exe('uv', platform));
    if (exists(uv)) {
      const uvx = p.join(dir, exe('uvx', platform));
      return { uv, uvx: exists(uvx) ? uvx : null };
    }
  }
  return null;
}

/** Split the launcher's own options from the server's command line (default: `serve`). */
function parseArgs(argv) {
  const opts = { installUv: false, printCommand: false, help: false, server: [] };
  let i = 0;
  for (; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--install-uv') opts.installUv = true;
    else if (a === '--print-command') opts.printCommand = true;
    else if (a === '--help' || a === '-h') opts.help = true;
    else break;
  }
  opts.server = argv.slice(i);
  if (opts.server.length === 0) opts.server = ['serve'];
  return opts;
}

/** The command that runs the pinned PyPI release with uv: uvx when present, else `uv tool run`. */
function buildCommand(found, version, serverArgs = ['serve']) {
  const toolArgs = [
    '--no-config',
    '--default-index',
    PYPI_INDEX,
    '--python',
    PYTHON,
    '--from',
    pypiSpec(version),
    PACKAGE,
    ...serverArgs,
  ];
  if (found.uvx) return { command: found.uvx, args: toolArgs };
  return { command: found.uv, args: ['tool', 'run', ...toolArgs] };
}

/** The environment for uv: the caller's, minus settings that would change where packages come from. */
function childEnv(env = process.env) {
  const out = { ...env };
  for (const k of INDEX_ENV) delete out[k];
  return out;
}

function wantsInstall(opts, env = process.env) {
  const v = String(env.HDR_INSTALL_UV || '').toLowerCase();
  return opts.installUv || v === '1' || v === 'true' || v === 'yes';
}

function missingUvMessage(platform = process.platform) {
  const line =
    platform === 'win32'
      ? '  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
      : '  curl -LsSf https://astral.sh/uv/install.sh | sh';
  return [
    'health-data-registry could not start: it needs a small free program called uv, which is not on this computer.',
    '',
    'To fix it, do one of these, then quit Claude fully and open it again:',
    '  - Ask your IT team to install uv (https://docs.astral.sh/uv/).',
    `  - Or open a ${platform === 'win32' ? 'PowerShell' : 'Terminal'} window, paste this line and press Return:`,
    line,
    '  - Or let this tool install uv itself: add "--install-uv" after "health-data-registry" in the settings',
    '    that start it, or switch on "Install uv if missing" in the extension settings.',
    '',
    'uv comes from Astral (astral.sh), the company that makes it. This tool never installs anything without one of the steps above.',
  ].join('\n');
}

/** Download Astral's installer script (following redirects only within astral.sh) and run it.
 *
 * On every platform the script is fetched here, through fetchInstaller's host and redirect checks, and only
 * then handed to the shell on stdin: PowerShell never downloads anything itself (no `irm | iex`). */
async function installUv({ platform = process.platform, fetchImpl = globalThis.fetch, runImpl = run } = {}) {
  if (typeof fetchImpl !== 'function') throw new Error('this Node.js has no fetch; install uv by hand');
  if (platform === 'win32') {
    const script = await fetchInstaller(INSTALLER.win32, fetchImpl);
    // `-Command -` reads the checked script from stdin; nothing is fetched by PowerShell.
    return runImpl('powershell', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'ByPass', '-Command', '-'], script);
  }
  const script = await fetchInstaller(INSTALLER.posix, fetchImpl);
  return runImpl('sh', ['-s'], script);
}

async function fetchInstaller(url, fetchImpl, hops = 0) {
  if (hops > 5) throw new Error('too many redirects from the uv installer address');
  const u = new URL(url);
  if (u.protocol !== 'https:' || !INSTALLER_HOSTS.has(u.hostname)) {
    throw new Error(`refusing to download the uv installer from ${u.host}`);
  }
  const res = await fetchImpl(url, { redirect: 'manual' });
  if (res.status >= 300 && res.status < 400) {
    const next = res.headers.get('location');
    if (!next) throw new Error('the uv installer address redirected nowhere');
    return fetchInstaller(new URL(next, url).toString(), fetchImpl, hops + 1);
  }
  if (!res.ok) throw new Error(`could not download the uv installer (status ${res.status})`);
  return res.text();
}

function run(command, args, stdinText) {
  return new Promise((resolve, reject) => {
    // The installer's output goes to stderr so it never reaches the MCP client on stdout.
    const child = spawn(command, args, { stdio: ['pipe', process.stderr, process.stderr] });
    child.on('error', reject);
    child.on('exit', (code) => (code === 0 ? resolve() : reject(new Error(`${command} exited with ${code}`))));
    if (stdinText != null) child.stdin.end(stdinText);
    else child.stdin.end();
  });
}

const HELP = `Usage: npx -y health-data-registry [--install-uv] [--print-command] [server command...]

Starts the health-data-registry MCP server for Claude (default command: serve).
  --install-uv     if uv is missing, install it with Astral's official installer first
  --print-command  print the uv command that would run, then stop
Environment: HDR_HUB, HDR_LAKE, HDR_CACHE and the rest are passed to the server unchanged.`;

async function main(argv = process.argv.slice(2), { env = process.env, platform = process.platform } = {}) {
  const opts = parseArgs(argv);
  if (opts.help) {
    process.stderr.write(`${HELP}\n`);
    return 0;
  }
  const version = require('../package.json').version;
  let found = findUv({ env, platform });
  if (!found && wantsInstall(opts, env)) {
    process.stderr.write('health-data-registry: uv not found; installing it from astral.sh ...\n');
    try {
      await installUv({ platform });
    } catch (err) {
      process.stderr.write(`health-data-registry: the uv install did not finish: ${err.message}\n\n`);
    }
    found = findUv({ env, platform });
  }
  if (!found) {
    process.stderr.write(`${missingUvMessage(platform)}\n`);
    return 1;
  }
  const { command, args } = buildCommand(found, version, opts.server);
  if (opts.printCommand) {
    process.stderr.write(`${[command, ...args].join(' ')}\n`);
    return 0;
  }
  return new Promise((resolve) => {
    const child = spawn(command, args, { stdio: 'inherit', env: childEnv(env) });
    const signals = platform === 'win32' ? ['SIGINT', 'SIGTERM'] : ['SIGINT', 'SIGTERM', 'SIGHUP'];
    const handlers = signals.map((s) => [s, () => child.kill(s)]);
    for (const [s, h] of handlers) process.on(s, h);
    const done = (code) => {
      for (const [s, h] of handlers) process.removeListener(s, h); // no listeners left behind
      resolve(code);
    };
    child.on('error', (err) => {
      process.stderr.write(`health-data-registry could not start uv (${command}): ${err.message}\n`);
      done(1);
    });
    child.on('exit', (code, signal) => done(code ?? (signal ? 1 : 0)));
  });
}

module.exports = {
  PACKAGE,
  PYTHON,
  PYPI_INDEX,
  INSTALLER,
  pypiSpec,
  uvDirs,
  findUv,
  parseArgs,
  buildCommand,
  childEnv,
  wantsInstall,
  missingUvMessage,
  fetchInstaller,
  installUv,
  main,
};
