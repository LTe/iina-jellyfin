#!/usr/bin/env node
/**
 * Vendors the official Jellyfin Web client into the plugin.
 *
 * The prebuilt client is taken from the Debian package Jellyfin publishes for
 * every release (no Node build, no fork of jellyfin-web sources). The only
 * change to the client is one script and one stylesheet tag in index.html,
 * which load the IINA integration from src/ui/web/iina/. Everything else the
 * integration needs goes through the NativeShell hooks Jellyfin Web offers to
 * host applications, so updating the client is a matter of bumping the
 * version below and re-running this script.
 *
 * Usage: node scripts/vendor-jellyfin-web.mjs [--from <dir-or-deb>] [--force]
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const JELLYFIN_WEB_VERSION = '12.0';
export const JELLYFIN_WEB_DEB_URL = `https://repo.jellyfin.org/files/server/debian/stable/v${JELLYFIN_WEB_VERSION}/amd64/jellyfin-web_${JELLYFIN_WEB_VERSION}%2Bdeb12_all.deb`;

const here = path.dirname(fileURLToPath(import.meta.url));
export const PROJECT_ROOT = path.resolve(here, '..');
export const DIST_DIR = path.join(PROJECT_ROOT, 'src/ui/web/dist');
export const CACHE_DIR = path.join(PROJECT_ROOT, '.cache');
export const VERSION_FILE = path.join(DIST_DIR, '.iina-vendored-version');

// What the integration adds to index.html. The shell must run before the
// deferred bundles so window.NativeShell exists when Jellyfin Web boots.
export const SHELL_TAGS =
  '<link rel="stylesheet" href="../iina/iina.css"><script src="../iina/iina-shell.js"></script>';

/**
 * index.html with the IINA integration tags inserted before the first bundle.
 * Idempotent: a page that already carries them comes back unchanged.
 */
export function patchIndexHtml(html) {
  if (html.includes('iina/iina-shell.js')) {
    return html;
  }
  const marker = '<script defer';
  const at = html.indexOf(marker);
  if (at === -1) {
    throw new Error('index.html has no deferred bundle scripts; the layout changed');
  }
  return `${html.slice(0, at)}${SHELL_TAGS}${html.slice(at)}`;
}

function log(message) {
  process.stdout.write(`${message}\n`);
}

async function download(url, destination) {
  log(`Downloading ${url}`);
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`Download failed: HTTP ${response.status} for ${url}`);
  }
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.writeFileSync(destination, Buffer.from(await response.arrayBuffer()));
}

/**
 * The web folder inside a Debian package: ar archive -> data.tar.xz ->
 * usr/share/jellyfin/web. `ar` and `tar` exist on macOS and Linux.
 */
function extractDeb(debPath) {
  const work = fs.mkdtempSync(path.join(os.tmpdir(), 'jellyfin-web-deb-'));
  execFileSync('ar', ['x', debPath], { cwd: work, stdio: 'inherit' });
  const data = fs.readdirSync(work).find((name) => name.startsWith('data.tar'));
  if (!data) {
    throw new Error('The package has no data archive');
  }
  execFileSync('tar', ['-xf', data], { cwd: work, stdio: 'inherit' });
  const web = path.join(work, 'usr/share/jellyfin/web');
  if (!fs.existsSync(path.join(web, 'index.html'))) {
    throw new Error('The package does not contain usr/share/jellyfin/web/index.html');
  }
  return web;
}

export function installDist(sourceDir) {
  fs.rmSync(DIST_DIR, { recursive: true, force: true });
  fs.mkdirSync(path.dirname(DIST_DIR), { recursive: true });
  fs.cpSync(sourceDir, DIST_DIR, {
    recursive: true,
    // Source maps only add size to the plugin package
    filter: (source) => !source.endsWith('.map'),
  });
  const indexPath = path.join(DIST_DIR, 'index.html');
  fs.writeFileSync(indexPath, patchIndexHtml(fs.readFileSync(indexPath, 'utf8')));
  fs.writeFileSync(VERSION_FILE, `${JELLYFIN_WEB_VERSION}\n`);
}

export async function vendor({ from, force } = {}) {
  if (!force && !from && fs.existsSync(VERSION_FILE)) {
    const current = fs.readFileSync(VERSION_FILE, 'utf8').trim();
    if (current === JELLYFIN_WEB_VERSION) {
      log(
        `Jellyfin Web ${current} is already vendored in ${path.relative(PROJECT_ROOT, DIST_DIR)}`
      );
      return DIST_DIR;
    }
  }

  let sourceDir;
  if (from && fs.statSync(from).isDirectory()) {
    sourceDir = from;
  } else {
    let debPath = from;
    if (!debPath) {
      debPath = path.join(CACHE_DIR, `jellyfin-web_${JELLYFIN_WEB_VERSION}.deb`);
      if (!fs.existsSync(debPath)) {
        await download(JELLYFIN_WEB_DEB_URL, debPath);
      }
    }
    sourceDir = extractDeb(debPath);
  }

  installDist(sourceDir);
  const files = fs.readdirSync(DIST_DIR).length;
  log(
    `Vendored Jellyfin Web ${JELLYFIN_WEB_VERSION} (${files} entries) into ${path.relative(PROJECT_ROOT, DIST_DIR)}`
  );
  return DIST_DIR;
}

function parseArgs(argv) {
  const options = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--from') {
      options.from = path.resolve(argv[++i]);
    } else if (argv[i] === '--force') {
      options.force = true;
    }
  }
  return options;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  vendor(parseArgs(process.argv.slice(2))).catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exit(1);
  });
}
