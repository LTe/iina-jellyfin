'use strict';

/**
 * Playback reports that did not reach the server (offline copy watched
 * without a connection, server down mid-stream) are kept and delivered later,
 * so the server still learns the resume position and what was watched.
 *
 * Two parts: a recorder that wraps the http client used for playback
 * reporting and turns every report into an outcome (delivered or not), and a
 * queue that stores undelivered outcomes on disk and retries them.
 */

const QUEUE_FILE = '@data/offline-playback-queue.json';
const DEFAULT_RETRY_MS = 60000;
const DEFAULT_MIN_GAP_MS = 5000;

/**
 * What a playback report to the server says. Only the reports that carry the
 * end state matter: progress and stop (position), watched (played flag).
 */
function classifyPlaybackReport(url, data) {
  const match = String(url || '').match(
    /^(https?:\/\/[^/]+)(?:\/(?:Sessions\/Playing\/(Progress|Stopped))|\/UserPlayedItems\/([^/?#]+))(?:\?|$)/i
  );
  if (!match) {
    return null;
  }
  const [, serverUrl, sessionKind, playedItemId] = match;
  if (playedItemId) {
    return { kind: 'watched', serverUrl, itemId: playedItemId, watched: true };
  }
  const itemId = data && data.ItemId;
  if (!itemId) {
    return null;
  }
  return {
    kind: sessionKind.toLowerCase(),
    serverUrl,
    itemId,
    positionTicks: Number(data.PositionTicks) || 0,
    mediaSourceId: data.MediaSourceId || null,
  };
}

/**
 * http client wrapper: every playback report passes through, and `onOutcome`
 * learns whether the server took it.
 */
function createPlaybackReportRecorder({ http, onOutcome, log }) {
  async function post(url, options) {
    const report = classifyPlaybackReport(url, options && options.data);
    let response;
    try {
      response = await http.post(url, options);
    } catch (error) {
      if (report) {
        log(`Playback report ${report.kind} for ${report.itemId} did not reach the server`);
        onOutcome({ ...report, delivered: false });
      }
      throw error;
    }
    if (report) {
      const delivered = Boolean(response) && response.statusCode < 400;
      onOutcome({ ...report, delivered });
    }
    return response;
  }

  return { ...http, post };
}

/**
 * Undelivered playback outcomes, stored in the plugin data folder and retried
 * with the credentials of the stored server they belong to.
 */
function createPlaybackSyncQueue({
  file,
  http,
  preferences,
  buildJellyfinHeaders,
  loadStoredServers,
  log,
  retryMs = DEFAULT_RETRY_MS,
  minGapMs = DEFAULT_MIN_GAP_MS,
}) {
  let entries = null;
  let retryTimer = null;
  let syncing = false;
  let lastAttemptAt = 0;

  function normalizeServerUrl(url) {
    return String(url || '').replace(/\/+$/, '');
  }

  function load() {
    if (entries) {
      return entries;
    }
    entries = [];
    try {
      if (file.exists(QUEUE_FILE)) {
        const parsed = JSON.parse(file.read(QUEUE_FILE));
        entries = Array.isArray(parsed)
          ? parsed.filter((entry) => entry && entry.itemId && entry.serverUrl)
          : [];
      }
    } catch (error) {
      log(`Could not read the playback sync queue: ${error.message}`);
    }
    return entries;
  }

  function save() {
    try {
      file.write(QUEUE_FILE, JSON.stringify(load(), null, 2));
    } catch (error) {
      log(`Could not save the playback sync queue: ${error.message}`);
    }
  }

  function findEntry(serverUrl, itemId) {
    return load().find((entry) => entry.serverUrl === serverUrl && entry.itemId === itemId);
  }

  function isEmpty(entry) {
    return entry.positionTicks === null && !entry.watched;
  }

  /**
   * Take note of a report. A delivered report clears what it carried (the
   * server knows it now); an undelivered one stores it for later. A position
   * of zero is never queued: it would only wipe the position the server has.
   */
  function record(outcome) {
    if (!outcome || !outcome.itemId || !outcome.serverUrl) {
      return;
    }
    const serverUrl = normalizeServerUrl(outcome.serverUrl);
    let entry = findEntry(serverUrl, outcome.itemId);
    if (!entry) {
      if (outcome.delivered) {
        return;
      }
      entry = {
        serverUrl,
        itemId: outcome.itemId,
        mediaSourceId: null,
        positionTicks: null,
        watched: false,
        updatedAt: 0,
      };
      load().push(entry);
    }

    if (outcome.kind === 'watched') {
      entry.watched = !outcome.delivered;
    } else if (outcome.delivered) {
      entry.positionTicks = null;
    } else if (outcome.positionTicks > 0) {
      entry.positionTicks = outcome.positionTicks;
      entry.mediaSourceId = outcome.mediaSourceId || null;
    }
    entry.updatedAt = Date.now();

    if (isEmpty(entry)) {
      entries = load().filter((candidate) => candidate !== entry);
    }
    save();
    if (!outcome.delivered) {
      log(`Queued playback ${outcome.kind} of ${outcome.itemId} for a later sync`);
      scheduleSync(0);
    }
  }

  function tokenFor(serverUrl) {
    const servers = (loadStoredServers() || []).filter(
      (server) => server && server.accessToken && normalizeServerUrl(server.serverUrl) === serverUrl
    );
    const match = servers.find((server) => server.userId) || servers[0];
    return match ? match.accessToken : null;
  }

  async function post(url, token, data) {
    const response = await http.post(url, {
      headers: buildJellyfinHeaders(token, {
        'Content-Type': 'application/json',
        Accept: 'application/json',
      }),
      data,
    });
    return Boolean(response) && response.statusCode < 400;
  }

  /**
   * Deliver one entry. Returns whether nothing is left of it.
   */
  async function deliver(entry) {
    const token = tokenFor(entry.serverUrl);
    if (!token) {
      log(`No credentials for ${entry.serverUrl}, keeping ${entry.itemId} queued`);
      return false;
    }
    if (entry.positionTicks !== null) {
      const ok = await post(`${entry.serverUrl}/Sessions/Playing/Stopped?ApiKey=${token}`, token, {
        ItemId: entry.itemId,
        MediaSourceId: entry.mediaSourceId || entry.itemId,
        PositionTicks: entry.positionTicks,
      });
      if (!ok) {
        return false;
      }
      entry.positionTicks = null;
      save();
    }
    if (entry.watched) {
      const ok = await post(
        `${entry.serverUrl}/UserPlayedItems/${entry.itemId}?ApiKey=${token}`,
        token
      );
      if (!ok) {
        return false;
      }
      entry.watched = false;
      save();
    }
    return true;
  }

  /**
   * Try to deliver everything queued. Keeps what fails and retries later.
   */
  async function sync() {
    if (syncing) {
      return false;
    }
    if (!preferences.get('sync_playback_progress')) {
      return false;
    }
    const queued = load().slice();
    if (queued.length === 0) {
      return true;
    }
    syncing = true;
    lastAttemptAt = Date.now();
    let allDone = true;
    try {
      for (const entry of queued) {
        try {
          if (await deliver(entry)) {
            entries = load().filter((candidate) => candidate !== entry);
            save();
            log(`Synced playback of ${entry.itemId} with ${entry.serverUrl}`);
          } else {
            allDone = false;
          }
        } catch (error) {
          allDone = false;
          log(`Playback sync of ${entry.itemId} failed: ${error.message}`);
        }
      }
    } finally {
      syncing = false;
    }
    if (!allDone) {
      scheduleSync(retryMs);
    }
    return allDone;
  }

  /**
   * An immediate sync replaces a pending retry; a retry never postpones an
   * immediate sync that is already due.
   */
  function scheduleSync(delay) {
    if (retryTimer) {
      if (delay > 0) {
        return;
      }
      clearTimeout(retryTimer);
    }
    retryTimer = setTimeout(() => {
      retryTimer = null;
      sync();
    }, delay);
  }

  /**
   * A cheap trigger for moments the connection may be back (any user
   * activity): syncs when something is queued and enough time has passed.
   */
  function requestSync() {
    if (load().length === 0 || Date.now() - lastAttemptAt < minGapMs) {
      return;
    }
    scheduleSync(0);
  }

  function pending() {
    return load().map((entry) => ({ ...entry }));
  }

  function stop() {
    clearTimeout(retryTimer);
    retryTimer = null;
  }

  return { record, sync, requestSync, scheduleSync, pending, stop };
}

module.exports = {
  QUEUE_FILE,
  classifyPlaybackReport,
  createPlaybackReportRecorder,
  createPlaybackSyncQueue,
};
