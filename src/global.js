/**
 * IINA Jellyfin Plugin - Global Entry
 *
 * Lives for the whole IINA session, unlike the player entry that IINA creates
 * per window. Two jobs:
 * - create new player windows on request (open_in_new_window)
 * - run the offline downloads. A download must not stop when the window it
 *   was started from closes or when another window opens, and every window
 *   must show the same list, so exactly one manager owns the downloads and
 *   the manifest. Player entries relay the webview messages here and receive
 *   the state back.
 */

const { createDebugLogger } = require('./lib/debug-log.js');
const { createJellyfinApi } = require('./lib/jellyfin-api.js');
const { createServerSessionStore } = require('./lib/server-session-store.js');
const { createDownloadTransport } = require('./lib/download-transport.js');
const { createOfflineDownloadManager } = require('./lib/offline-downloads.js');

const { global, console, preferences, utils, file, http } = iina;

const debugLog = createDebugLogger(preferences, console);

debugLog('Jellyfin Plugin Global Entry loaded');

// Listen for messages from main entries to create new instances
global.onMessage('create-player', (data, player) => {
  debugLog('Global entry received create-player message', {
    hasData: !!data,
    url: data?.url,
    title: data?.title,
  });

  try {
    const { url, title } = data;

    // Create a new player instance with the media URL
    const playerId = global.createPlayerInstance({
      url: url,
      label: `jellyfin-${Date.now()}`, // Unique label
      enablePlugins: false, // Disable other plugins for cleaner experience
      disableWindowAnimation: false, // Keep animations for better UX
    });

    debugLog(`Created new player instance ${playerId} for: ${title}`);

    // Send confirmation back to the requesting player
    if (player) {
      global.postMessage(player, 'player-created', {
        playerId: playerId,
        title: title,
        url: url,
      });
    }
  } catch (error) {
    debugLog('Error creating player instance: ' + error);

    // Send error back to requesting player
    if (player) {
      global.postMessage(player, 'player-creation-failed', {
        error: error.toString(),
        url: data?.url,
      });
    }
  }
});

// ---------------------------------------------------------------------------
// Offline downloads
// ---------------------------------------------------------------------------

const { buildJellyfinHeaders, fetchPlaybackInfo } = createJellyfinApi({
  http,
  preferences,
  log: debugLog,
});
const { loadStoredServers } = createServerSessionStore({ preferences, log: debugLog });

// Player windows that talked to us. IINA only broadcasts to windows the
// plugin created itself, so every window is addressed by its label instead.
// A label whose window has closed is a no-op for IINA.
const players = new Set();
// The window whose message is being handled: replies and playback go there.
let replyTo = null;

function rememberPlayer(player) {
  if (player !== null && player !== undefined) {
    players.add(player);
  }
}

function postToPlayers(name, data) {
  for (const player of players) {
    try {
      global.postMessage(player, name, data);
    } catch (error) {
      debugLog(`Could not post ${name} to player ${player}: ${error.message}`);
    }
  }
}

function postToRequester(name, data) {
  if (replyTo !== null) {
    global.postMessage(replyTo, name, data);
    return;
  }
  postToPlayers(name, data);
}

const offlineDownloads = createOfflineDownloadManager({
  file,
  utils,
  http,
  preferences,
  fetchPlaybackInfo,
  buildJellyfinHeaders,
  loadStoredServers,
  transport: createDownloadTransport({ utils, http, log: debugLog }),
  // No player here: OSD messages are shown by the windows
  core: { osd: (message) => postToPlayers('offline-osd', { message }) },
  notifyViews: postToPlayers,
  // Playing a download happens in the window that asked for it
  openMedia: (data) => postToRequester('offline-play', data),
  log: debugLog,
});

offlineDownloads.registerMessageHandlers({
  onMessage(name, callback) {
    global.onMessage(name, (data, player) => {
      rememberPlayer(player);
      replyTo = player ?? null;
      callback(data);
    });
  },
  postMessage: postToRequester,
});

debugLog('Global entry message listeners registered');
