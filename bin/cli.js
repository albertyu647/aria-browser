#!/usr/bin/env node
// aria — client for the persistent silent Chrome daemon (scripts/ariad.py).
//
//   aria open <url> [--profile <alias>]            navigate + compact snapshot with refs
//   aria click e12 | aria type e7 "query" --submit  act on refs from the snapshot
//   aria batch 'open x.com' 'find search' ...      several commands, one round trip
//   aria help                                      all commands
//
// Legacy: `cli.js browse <url> [...]` (one-shot) and `cli.js` / `--list` / `--path` (cookie sync).

const { spawn } = require('child_process');
const net = require('net');
const fs = require('fs');
const os = require('os');
const path = require('path');

const SCRIPTS = path.join(__dirname, '..', 'scripts');
const HOME = process.env.ARIA_HOME || path.join(os.homedir(), '.cache', 'aria-browser');
const INFO = path.join(HOME, 'ariad.json');
const PY = process.env.ARIA_PYTHON || 'python3';

let argv = process.argv.slice(2);

// ---- legacy sync entry points ---------------------------------------------
const SYNC_FLAGS = new Set(['--list', '--path', '--json', 'sync']);
if (argv.length === 0 || SYNC_FLAGS.has(argv[0])) {
  if (argv[0] === 'sync') argv = argv.slice(1);
  const py = spawn(PY, [path.join(SCRIPTS, 'sync_auth.py'), ...argv], { stdio: 'inherit' });
  py.on('close', (c) => process.exit(c || 0));
  return;
}

// ---- global flags ---------------------------------------------------------
function defaultProfile() {
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(os.homedir(), '.config', 'chrome-storage-states', 'profiles.json'), 'utf8'));
    return cfg.default || 'default';
  } catch { return 'default'; }
}
let profile = process.env.ARIA_PROFILE || defaultProfile();
let jsonOut = false;
const rest = [];
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === '--profile' || a === '-p') { profile = argv[++i]; continue; }
  if (a.startsWith('--profile=')) { profile = a.slice(10); continue; }
  if (a === '--json-out') { jsonOut = true; continue; }
  rest.push(a);
}
argv = rest;

// ---- legacy one-shot `browse` → translated to daemon commands ---------------
if (argv[0] === 'browse') argv = translateBrowse(argv.slice(1));

function translateBrowse(a) {
  const url = a.find((x, i) => !x.startsWith('-') && (i === 0 || !a[i - 1].startsWith('--') || ['--full-page', '--json'].includes(a[i - 1])));
  const get = (f) => { const i = a.indexOf(f); return i >= 0 ? a[i + 1] : null; };
  const has = (f) => a.includes(f);
  const cmds = [];
  const mode = get('--mode') || 'auto';
  const wait = get('--wait');
  cmds.push(`open ${q(url)} --no-snap --new${wait ? ' --wait ' + Math.min(Number(wait), 3) : ''}`);
  const fill = get('--fill');
  if (fill) {
    const [t, ...v] = fill.includes('=') ? fill.split('=') : ['input, textarea', fill];
    cmds.push(`type ${q(t)} ${q(v.join('='))}`);
  }
  if (get('--click')) cmds.push(`click ${q(get('--click'))}`);
  if (get('--screenshot')) cmds.push(`shot ${q(get('--screenshot'))}${has('--full-page') ? ' --full' : ''}`);
  const filt = get('--filter');
  const f = filt ? ` --filter ${q(filt)}` : '';
  if (mode === 'html' || mode === 'raw-html') {
    cmds.push(`eval ${q(mode === 'html' ? 'document.documentElement.outerHTML.replace(/<(script|style|svg)[\\s\\S]*?<\\/\\1>/gi,"").replace(/data:image\\/[^"\')]+/g,"[data:image]")' : 'document.documentElement.outerHTML')}`);
  } else if (mode === 'aria') {
    cmds.push(`snap --raw${f}`);
  } else {
    cmds.push(`snap${f}`);
  }
  cmds.push('tab close');
  return ['batch', ...cmds];
}
function q(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'"; }

// ---- daemon connection ------------------------------------------------------
function readInfo() {
  try { return JSON.parse(fs.readFileSync(INFO, 'utf8')); } catch { return null; }
}

function send(info, payload) {
  return new Promise((resolve, reject) => {
    const sock = net.createConnection({ host: '127.0.0.1', port: info.port });
    let buf = '';
    sock.setEncoding('utf8');
    sock.on('connect', () => sock.write(JSON.stringify({ token: info.token, ...payload }) + '\n'));
    sock.on('data', (d) => { buf += d; });
    sock.on('end', () => {
      try { resolve(JSON.parse(buf)); } catch (e) { reject(new Error('bad response: ' + buf.slice(0, 200))); }
    });
    sock.on('error', reject);
  });
}

async function startDaemon() {
  fs.mkdirSync(HOME, { recursive: true });
  try { fs.unlinkSync(INFO); } catch {}
  const log = fs.openSync(path.join(HOME, 'ariad.log'), 'a');
  const child = spawn(PY, [path.join(SCRIPTS, 'ariad.py')], { detached: true, stdio: ['ignore', log, log] });
  child.unref();
  const t0 = Date.now();
  while (Date.now() - t0 < 30000) {
    await new Promise((r) => setTimeout(r, 100));
    const info = readInfo();
    if (info) return info;
  }
  throw new Error('daemon did not start; see ' + path.join(HOME, 'ariad.log'));
}

(async () => {
  if (argv[0] === 'stop' && !readInfo()) { console.log('not running'); return; }
  let info = readInfo();
  let res;
  try {
    if (!info) throw new Error('no daemon');
    res = await send(info, { argv, profile });
  } catch (e) {
    if (argv[0] === 'stop') { console.log('not running'); return; }
    info = await startDaemon();
    res = await send(info, { argv, profile });
  }
  if (jsonOut) process.stdout.write(JSON.stringify(res) + '\n');
  else process.stdout.write((res.out || '') + '\n');
  process.exitCode = res.code || 0;
})().catch((e) => { console.error('aria: ' + e.message); process.exit(1); });
