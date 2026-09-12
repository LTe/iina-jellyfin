/**
 * IINA integration for the official Jellyfin Web client.
 *
 * Loaded before the Jellyfin Web bundles (see scripts/vendor-jellyfin-web.mjs).
 * Jellyfin Web is unmodified: everything here goes through the hooks it
 * offers to host applications (window.NativeShell, plugins loaded via
 * window) and through IINA's webview bridge (window.iina). Three jobs:
 *
 * - NativeShell: identity, supported features, file downloads and the
 *   download manager entry of the user menu.
 * - IinaPlayerPlugin: a Jellyfin Web plugin that receives the app's
 *   playback manager and routes every play request to IINA instead of the
 *   in-page video element.
 * - Offline downloads UI: a Downloads panel and an offline button on item
 *   pages, driven by the same messages the plugin entry already speaks.
 */
(function iinaJellyfinShell(window) {
  'use strict';

  const PLUGIN_NAME = 'IinaPlayerPlugin';
  const STORAGE_DEVICE_ID = 'iina-jellyfin-device-id';
  const PLAYABLE_MEDIA_TYPES = ['Video', 'Audio'];
  const DEFAULT_QUALITY_PRESETS = [
    { id: 'original', label: 'Original quality' },
    { id: '8000', label: '8 Mb/s' },
    { id: '4000', label: '4 Mb/s' },
    { id: '2000', label: '2 Mb/s' },
    { id: '1000', label: '1 Mb/s' },
    { id: '500', label: '500 Kb/s' },
    { id: '250', label: '250 Kb/s' },
  ];
  const SUPPORTED_FEATURES = [
    'filedownload',
    'downloadmanagement',
    'externallinks',
    'multiserver',
    'displaylanguage',
    'displaymode',
    'fileinput',
    'screensaver',
    'subtitleappearance',
    'targetblank',
  ];

  // -------------------------------------------------------------------------
  // IINA bridge
  // -------------------------------------------------------------------------

  function hasBridge() {
    return Boolean(window.iina && typeof window.iina.postMessage === 'function');
  }

  function post(name, data) {
    if (!hasBridge()) {
      return false;
    }
    window.iina.postMessage(name, data);
    return true;
  }

  function on(name, callback) {
    if (window.iina && typeof window.iina.onMessage === 'function') {
      window.iina.onMessage(name, callback);
    }
  }

  // -------------------------------------------------------------------------
  // Session: what the plugin entry needs to talk to the server itself
  // -------------------------------------------------------------------------

  const state = {
    identity: null,
    ServerConnections: null,
    downloads: [],
    directory: null,
    quality: 'original',
    qualityPresets: DEFAULT_QUALITY_PRESETS.slice(),
    lastSessionKey: null,
  };

  function currentApiClient() {
    const connections = state.ServerConnections;
    return connections && typeof connections.currentApiClient === 'function'
      ? connections.currentApiClient()
      : null;
  }

  /**
   * Server url and credentials of the signed-in user, or null.
   */
  function currentSession() {
    const apiClient = currentApiClient();
    if (!apiClient || !apiClient.accessToken() || !apiClient.serverAddress()) {
      return null;
    }
    const info = typeof apiClient.serverInfo === 'function' ? apiClient.serverInfo() || {} : {};
    return {
      serverUrl: apiClient.serverAddress().replace(/\/+$/, ''),
      accessToken: apiClient.accessToken(),
      userId: apiClient.getCurrentUserId() || info.UserId || null,
      serverId: apiClient.serverId() || info.Id || null,
      serverName: info.Name || null,
    };
  }

  /**
   * Hands the session to the plugin entry (stored servers drive autoplay,
   * offline playback reporting and the sync queue). Sent once per change.
   */
  function publishSession(username) {
    const session = currentSession();
    if (!session) {
      return false;
    }
    const key = `${session.serverUrl}|${session.userId}|${session.accessToken}`;
    if (key === state.lastSessionKey) {
      return false;
    }
    state.lastSessionKey = key;
    return post('store-session', { ...session, username: username || null });
  }

  // -------------------------------------------------------------------------
  // NativeShell
  // -------------------------------------------------------------------------

  function localDeviceId() {
    try {
      let id = window.localStorage.getItem(STORAGE_DEVICE_ID);
      if (!id) {
        id = `iina-web-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
        window.localStorage.setItem(STORAGE_DEVICE_ID, id);
      }
      return id;
    } catch {
      return 'iina-web';
    }
  }

  function requestDownloadById(itemId) {
    const session = currentSession();
    if (!session) {
      return false;
    }
    return post('offline-download-by-id', {
      itemId,
      serverUrl: session.serverUrl,
      accessToken: session.accessToken,
      serverId: session.serverId,
      quality: state.quality,
    });
  }

  window.NativeShell = {
    AppHost: {
      init() {},
      supports(feature) {
        return SUPPORTED_FEATURES.includes(String(feature || '').toLowerCase());
      },
      getDefaultLayout() {
        return 'desktop';
      },
      getDeviceProfile(profileBuilder) {
        return profileBuilder({});
      },
      deviceName() {
        return 'IINA';
      },
      deviceId() {
        return (state.identity && state.identity.deviceId) || localDeviceId();
      },
      appName() {
        return (state.identity && state.identity.clientName) || 'IINA Jellyfin Plugin';
      },
      appVersion() {
        return (state.identity && state.identity.version) || '0.0.0';
      },
      exit() {},
    },
    getPlugins() {
      return [PLUGIN_NAME];
    },
    downloadFiles(items) {
      for (const item of items || []) {
        if (item && item.itemId) {
          requestDownloadById(item.itemId);
        }
      }
    },
    openDownloadManager() {
      downloadsPanel.open();
    },
    openUrl(url) {
      if (!post('open-external-url', { url })) {
        window.open(url, '_blank');
      }
    },
  };

  // -------------------------------------------------------------------------
  // Playback: every play request goes to IINA
  // -------------------------------------------------------------------------

  function isPlayable(item) {
    return Boolean(
      item && item.Id && !item.IsFolder && PLAYABLE_MEDIA_TYPES.includes(item.MediaType)
    );
  }

  function displayTitle(item) {
    if (item.Type === 'Episode' && item.SeriesName) {
      const hasNumbers =
        item.ParentIndexNumber !== undefined &&
        item.ParentIndexNumber !== null &&
        item.IndexNumber !== undefined &&
        item.IndexNumber !== null;
      const code = hasNumbers
        ? ` S${String(item.ParentIndexNumber).padStart(2, '0')}E${String(item.IndexNumber).padStart(2, '0')}`
        : '';
      return `${item.SeriesName}${code} - ${item.Name || 'Unknown Title'}`;
    }
    return item.Name || 'Unknown Title';
  }

  function streamUrlFor(apiClient, item) {
    const route = item.MediaType === 'Audio' ? 'Audio' : 'Videos';
    return apiClient.getUrl(`${route}/${item.Id}/stream`, {
      static: true,
      ApiKey: apiClient.accessToken(),
    });
  }

  async function fetchByIds(apiClient, ids) {
    const result = await apiClient.getItems(apiClient.getCurrentUserId(), {
      Ids: ids.join(','),
      Fields: 'MediaSources',
    });
    const items = (result && result.Items) || [];
    return ids.map((id) => items.find((item) => item.Id === id)).filter(Boolean);
  }

  /**
   * Children of a folder-like item (series, season, album, playlist, box set)
   * in play order.
   */
  async function fetchChildren(apiClient, item) {
    const result = await apiClient.getItems(apiClient.getCurrentUserId(), {
      ParentId: item.Id,
      Recursive: true,
      Filters: 'IsNotFolder',
      MediaTypes: PLAYABLE_MEDIA_TYPES.join(','),
      SortBy: 'ParentIndexNumber,IndexNumber,SortName',
      Fields: 'MediaSources',
      Limit: 1000,
    });
    return ((result && result.Items) || []).filter(isPlayable);
  }

  async function expandForPlayback(apiClient, items) {
    const playable = [];
    for (const item of items) {
      if (isPlayable(item)) {
        playable.push(item);
      } else if (item && item.Id) {
        playable.push(...(await fetchChildren(apiClient, item)));
      }
    }
    return playable;
  }

  function shuffled(items) {
    const copy = items.slice();
    for (let i = copy.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [copy[i], copy[j]] = [copy[j], copy[i]];
    }
    return copy;
  }

  /**
   * Turns a Jellyfin Web play request into IINA playback messages. Returns
   * false when the request is not ours to handle (photos, books, no
   * bridge), so the caller can fall back to the client's own player.
   */
  async function playInIina(options) {
    const apiClient = currentApiClient();
    if (!hasBridge() || !apiClient || !options) {
      return false;
    }
    let items = options.items || [];
    if (items.length === 0 && Array.isArray(options.ids) && options.ids.length > 0) {
      items = await fetchByIds(apiClient, options.ids);
    }
    let playable = await expandForPlayback(apiClient, items);
    if (playable.length === 0) {
      return false;
    }
    if (options.shuffle) {
      playable = shuffled(playable);
    } else if (options.startIndex > 0) {
      playable = playable.slice(options.startIndex);
    }

    publishSession();
    const entries = playable.map((item) => ({
      streamUrl: streamUrlFor(apiClient, item),
      title: displayTitle(item),
      itemId: item.Id,
    }));
    if (entries.length === 1) {
      post('play-media', entries[0]);
    } else {
      post('play-media-list', { items: entries });
    }
    return true;
  }

  /**
   * The plugin class Jellyfin Web instantiates: it hands over the playback
   * manager and the server connections, which is all the shell needs.
   */
  class IinaPlayerPlugin {
    constructor(deps) {
      this.name = 'IINA';
      this.type = 'mediaplayer';
      this.id = 'iinaplayer';
      this.priority = -1;
      this.isLocalPlayer = true;
      state.ServerConnections = deps.ServerConnections;
      this.toast = deps.toast;
      this.installPlaybackHook(deps.playbackManager);
      this.watchSessions(deps.events, deps.ServerConnections);
      publishSession();
    }

    // Never chosen as a player by the manager: playback is intercepted above
    // that level, so the manager keeps no state about IINA.
    canPlayMediaType() {
      return false;
    }

    installPlaybackHook(playbackManager) {
      if (!playbackManager || typeof playbackManager.play !== 'function') {
        return;
      }
      const original = playbackManager.play.bind(playbackManager);
      playbackManager.play = (options) =>
        playInIina(options)
          .catch((error) => {
            console.error('[iina] could not hand playback to IINA', error);
            return false;
          })
          .then((handled) => (handled ? undefined : original(options)));
    }

    watchSessions(events, connections) {
      if (!events || !connections || typeof events.on !== 'function') {
        return;
      }
      events.on(connections, 'localusersignedin', (_event, user) => {
        publishSession(user && user.Name);
      });
    }
  }

  window[PLUGIN_NAME] = async () => IinaPlayerPlugin;

  // -------------------------------------------------------------------------
  // Offline downloads: state and item page button
  // -------------------------------------------------------------------------

  function escapeHtml(value) {
    return String(value ?? '').replace(
      /[&<>"']/g,
      (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]
    );
  }

  function entryFor(itemId) {
    return state.downloads.find((entry) => entry.itemId === itemId) || null;
  }

  function isReady(entry) {
    return Boolean(entry && entry.status === 'completed' && !entry.fileMissing);
  }

  /**
   * Label and state of an offline button, shared by item pages and the panel.
   */
  function describeButton(entry) {
    if (!entry || entry.status === 'cancelled') {
      return { label: 'Offline', icon: 'file_download', state: 'idle', disabled: false };
    }
    if (entry.status === 'queued') {
      return { label: 'Queued…', icon: 'hourglass_empty', state: 'queued', disabled: true };
    }
    if (entry.status === 'downloading') {
      return {
        label: entry.transcoded ? 'Transcoding…' : `${entry.progress || 0}%`,
        icon: 'downloading',
        state: 'downloading',
        disabled: true,
      };
    }
    if (entry.status === 'completed') {
      return entry.fileMissing
        ? { label: 'Re-download', icon: 'file_download', state: 'missing', disabled: false }
        : { label: 'Play offline', icon: 'offline_pin', state: 'completed', disabled: false };
    }
    return { label: 'Retry download', icon: 'refresh', state: 'failed', disabled: false };
  }

  function currentDetailsItemId() {
    const match = String(window.location.hash || '').match(/[?&]id=([^&#]+)/);
    return match ? decodeURIComponent(match[1]) : null;
  }

  function handleOfflineClick(itemId) {
    const entry = entryFor(itemId);
    if (isReady(entry)) {
      return post('play-offline', { itemId });
    }
    return requestDownloadById(itemId);
  }

  function refreshDetailButtons() {
    const itemId = currentDetailsItemId();
    for (const button of document.querySelectorAll('.btnIinaOffline')) {
      if (!itemId) {
        button.classList.add('hide');
        continue;
      }
      button.classList.remove('hide');
      button.dataset.itemId = itemId;
      const description = describeButton(entryFor(itemId));
      button.title = description.label;
      button.dataset.offlineState = description.state;
      button.disabled = description.disabled;
      const icon = button.querySelector('.detailButton-icon');
      const text = button.querySelector('.iina-offline-label');
      if (icon) icon.className = `material-icons detailButton-icon ${description.icon}`;
      // Only touch the text node when it changes: the page observer reacts to
      // child list changes, and rewriting the label would retrigger it.
      if (text && text.textContent !== description.label) text.textContent = description.label;
    }
  }

  /**
   * Adds the offline button next to Play on item pages. Jellyfin Web renders
   * pages on demand, so new button rows are picked up as they appear.
   */
  function ensureDetailButtons() {
    let added = false;
    for (const container of document.querySelectorAll('.mainDetailButtons')) {
      if (container.querySelector('.btnIinaOffline')) {
        continue;
      }
      added = true;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'button-flat btnIinaOffline detailButton';
      button.innerHTML =
        '<div class="detailButton-content"><span class="material-icons detailButton-icon file_download" aria-hidden="true"></span><span class="iina-offline-label">Offline</span></div>';
      button.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        handleOfflineClick(button.dataset.itemId);
      });
      container.appendChild(button);
    }
    if (added) refreshDetailButtons();
  }

  function handleOfflineState(data) {
    if (!data) {
      return;
    }
    state.downloads = Array.isArray(data.downloads) ? data.downloads : [];
    state.directory = data.directory || null;
    if (Array.isArray(data.qualityPresets) && data.qualityPresets.length > 0) {
      state.qualityPresets = data.qualityPresets;
    }
    if (data.quality) {
      state.quality = data.quality;
    }
    if (data.error) {
      downloadsPanel.notice(data.error);
    }
    refreshDetailButtons();
    downloadsPanel.render();
  }

  // -------------------------------------------------------------------------
  // Downloads panel (the user menu's "Downloads" entry)
  // -------------------------------------------------------------------------

  const downloadsPanel = {
    element: null,
    noticeTimer: null,
    pendingRemoveId: null,

    ensure() {
      if (this.element) {
        return this.element;
      }
      const element = document.createElement('div');
      element.className = 'iina-downloads hide';
      element.innerHTML = `
        <div class="iina-downloads-dialog">
          <div class="iina-downloads-header">
            <h2>Offline downloads</h2>
            <button type="button" class="iina-downloads-close" title="Close">&#10005;</button>
          </div>
          <div class="iina-downloads-notice hide"></div>
          <div class="iina-downloads-toolbar">
            <label>Quality <select class="iina-downloads-quality"></select></label>
            <button type="button" class="iina-downloads-open-folder">Open folder</button>
            <button type="button" class="iina-downloads-change-folder">Change folder…</button>
          </div>
          <div class="iina-downloads-directory"></div>
          <div class="iina-downloads-list"></div>
        </div>`;
      element.addEventListener('click', (event) => {
        if (event.target === element) {
          this.close();
        }
      });
      element.querySelector('.iina-downloads-close').addEventListener('click', () => this.close());
      element.querySelector('.iina-downloads-quality').addEventListener('change', (event) => {
        state.quality = event.target.value;
        post('offline-set-quality', { quality: state.quality });
      });
      element
        .querySelector('.iina-downloads-open-folder')
        .addEventListener('click', () => post('offline-open-folder'));
      element
        .querySelector('.iina-downloads-change-folder')
        .addEventListener('click', () => post('offline-choose-folder'));
      element.querySelector('.iina-downloads-list').addEventListener('click', (event) => {
        const button = event.target.closest('[data-action]');
        if (button) {
          this.handleAction(button.dataset.action, button.dataset.itemId, button);
        }
      });
      document.body.appendChild(element);
      this.element = element;
      this.render();
      return element;
    },

    open() {
      this.ensure().classList.remove('hide');
      post('get-offline-downloads', {});
    },

    close() {
      if (this.element) {
        this.element.classList.add('hide');
      }
    },

    isOpen() {
      return Boolean(this.element) && !this.element.classList.contains('hide');
    },

    notice(message) {
      const box = this.ensure().querySelector('.iina-downloads-notice');
      box.textContent = message;
      box.classList.remove('hide');
      clearTimeout(this.noticeTimer);
      this.noticeTimer = setTimeout(() => box.classList.add('hide'), 5000);
    },

    handleAction(action, itemId, button) {
      if (action === 'play') {
        post('play-offline', { itemId });
      } else if (action === 'cancel') {
        post('offline-cancel', { itemId });
      } else if (action === 'retry') {
        const session = currentSession() || {};
        post('offline-retry', {
          itemId,
          serverUrl: session.serverUrl || null,
          accessToken: session.accessToken || null,
        });
      } else if (action === 'reveal') {
        post('offline-show-in-finder', { itemId });
      } else if (action === 'remove') {
        // Removing deletes files: the first click only arms the button
        if (this.pendingRemoveId === itemId) {
          this.pendingRemoveId = null;
          post('offline-remove', { itemId });
          return;
        }
        this.pendingRemoveId = itemId;
        button.textContent = 'Confirm?';
        setTimeout(() => {
          if (this.pendingRemoveId === itemId) {
            this.pendingRemoveId = null;
            this.render();
          }
        }, 4000);
      }
    },

    describeStatus(entry) {
      if (entry.status === 'queued') return 'Queued';
      if (entry.status === 'downloading') {
        return entry.transcoded
          ? 'Transcoding and downloading…'
          : `Downloading ${entry.progress || 0}%`;
      }
      if (entry.status === 'cancelled') return 'Cancelling…';
      if (entry.status === 'failed') return `Failed: ${entry.error || 'unknown error'}`;
      if (entry.fileMissing) return 'File missing';
      const parts = ['Ready to play offline'];
      if (entry.qualityLabel && entry.quality !== 'original') parts.push(entry.qualityLabel);
      parts.push(`${(entry.subtitles || []).length} subtitle(s)`);
      return parts.join(' · ');
    },

    actionsFor(entry) {
      if (entry.status === 'queued' || entry.status === 'downloading') {
        return [{ action: 'cancel', label: 'Cancel' }];
      }
      if (entry.status === 'cancelled') {
        return [];
      }
      if (entry.status === 'completed' && !entry.fileMissing) {
        return [
          { action: 'play', label: 'Play' },
          { action: 'reveal', label: 'Reveal' },
          { action: 'remove', label: 'Remove' },
        ];
      }
      return [
        { action: 'retry', label: 'Retry' },
        { action: 'remove', label: 'Remove' },
      ];
    },

    render() {
      if (!this.element) {
        return;
      }
      const select = this.element.querySelector('.iina-downloads-quality');
      const options = state.qualityPresets
        .map(
          (preset) =>
            `<option value="${escapeHtml(preset.id)}">${escapeHtml(preset.label)}</option>`
        )
        .join('');
      if (select.innerHTML !== options) {
        select.innerHTML = options;
      }
      select.value = state.quality;
      this.element.querySelector('.iina-downloads-directory').textContent = state.directory
        ? `Folder: ${state.directory}`
        : '';

      const list = this.element.querySelector('.iina-downloads-list');
      if (state.downloads.length === 0) {
        list.innerHTML =
          '<div class="iina-downloads-empty">No downloads yet. Use the Offline button on a movie, episode or song.</div>';
        return;
      }
      const sorted = state.downloads
        .slice()
        .sort((left, right) => (right.createdAt || 0) - (left.createdAt || 0));
      list.innerHTML = sorted
        .map((entry) => {
          const progress =
            entry.status === 'downloading'
              ? `<div class="iina-downloads-progress"><div style="width:${entry.transcoded ? 100 : Math.max(0, Math.min(100, entry.progress || 0))}%"></div></div>`
              : '';
          const actions = this.actionsFor(entry)
            .map(
              (action) =>
                `<button type="button" data-action="${action.action}" data-item-id="${escapeHtml(entry.itemId)}">${this.pendingRemoveId === entry.itemId && action.action === 'remove' ? 'Confirm?' : action.label}</button>`
            )
            .join('');
          return `<div class="iina-downloads-item status-${escapeHtml(entry.status)}" data-download-id="${escapeHtml(entry.itemId)}">
            <div class="iina-downloads-title">${escapeHtml(entry.title || entry.name || entry.itemId)}</div>
            <div class="iina-downloads-status">${escapeHtml(this.describeStatus(entry))}</div>
            ${progress}
            <div class="iina-downloads-actions">${actions}</div>
          </div>`;
        })
        .join('');
    },
  };

  // -------------------------------------------------------------------------
  // Boot
  // -------------------------------------------------------------------------

  on('client-identity', (data) => {
    if (data && data.deviceId) {
      state.identity = data;
    }
  });
  on('offline-downloads', handleOfflineState);
  post('get-client-identity', {});
  post('get-offline-downloads', {});

  let pageObserver = null;

  function start() {
    ensureDetailButtons();
    refreshDetailButtons();
    pageObserver = new window.MutationObserver(() => ensureDetailButtons());
    pageObserver.observe(document.body, { childList: true, subtree: true });
    window.addEventListener('hashchange', refreshDetailButtons);
  }

  function stop() {
    if (pageObserver) {
      pageObserver.disconnect();
    }
    window.removeEventListener('hashchange', refreshDetailButtons);
  }

  if (document.body) {
    start();
  } else {
    document.addEventListener('DOMContentLoaded', start);
  }

  // Exposed for the plugin's own tests
  window.__iinaShell = {
    state,
    playInIina,
    describeButton,
    displayTitle,
    currentSession,
    publishSession,
    downloadsPanel,
    ensureDetailButtons,
    refreshDetailButtons,
    IinaPlayerPlugin,
    stop,
  };
})(window);
