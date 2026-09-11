/**
 * IINA Jellyfin Plugin - Global Entry
 *
 * Lives for the whole IINA session, unlike the player entry that IINA creates
 * per window. Two jobs:
 * - create new player windows on request (open_in_new_window)
 * - run the offline downloads. A download must not stop when the window it
 *   was started from closes or when another window opens, and every window
 *   must show the same list, so exactly one manager owns the downloads and
 *   the manifest. Player entries relay the webview messages here and poll
 *   for the state.
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

// IINA answers a message to a window label by force-unwrapping that window's
// plugin instance: a window that is still loading its plugin, or one whose
// plugin is gone, crashes IINA. So this entry never sends anything on its
// own. It only replies to the window whose message it is handling (that
// window is alive and loaded, or it could not have asked), and the windows
// poll for state while downloads run.
let replyTo = null;
// OSD text for the windows; they show a notice once, by its id.
let noticeCounter = 0;
let lastNotice = null;

function postToRequester(name, data) {
  if (replyTo === null) {
    debugLog(`No requesting window for ${name}, dropping it`);
    return;
  }
  global.postMessage(replyTo, name, data);
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
  core: {
    osd: (message) => {
      noticeCounter += 1;
      lastNotice = { id: noticeCounter, message };
    },
  },
  // Windows poll for state, nothing is pushed
  notifyViews: () => {},
  // Playing a download happens in the window that asked for it
  openMedia: (data) => postToRequester('offline-play', data),
  log: debugLog,
});

offlineDownloads.registerMessageHandlers({
  onMessage(name, callback) {
    global.onMessage(name, (data, player) => {
      replyTo = player ?? null;
      try {
        callback(data);
      } finally {
        replyTo = null;
      }
    });
  },
  // The manager only ever posts the state this way; the OSD text rides along
  postMessage(name, data) {
    postToRequester(name, { ...data, notice: lastNotice });
  },
});

debugLog('Global entry message listeners registered');
