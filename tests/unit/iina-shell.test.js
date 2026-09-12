// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

const SERVER = 'http://jf.local:8096';

/**
 * Loads the shell fresh into the jsdom window, with (or without) the IINA
 * webview bridge in place, and records what it posts to the plugin.
 */
async function loadShell({ bridge = true, withoutBody = false, prepare = null } = {}) {
  if (window.__iinaShell) {
    window.__iinaShell.stop();
  }
  document.body.innerHTML = '';
  window.location.hash = '';
  const posted = [];
  const handlers = {};
  if (bridge === true) {
    window.iina = {
      postMessage: vi.fn((name, data) => posted.push([name, data])),
      onMessage: vi.fn((name, callback) => {
        handlers[name] = callback;
      }),
    };
  } else if (bridge) {
    window.iina = bridge;
  } else {
    delete window.iina;
  }
  if (withoutBody) {
    document.documentElement.removeChild(document.body);
  }
  if (prepare) {
    prepare();
  }
  vi.resetModules();
  await import('../../src/ui/web/iina/iina-shell.js');
  return {
    shell: window.__iinaShell,
    posted,
    handlers,
    emit: (name, data) => handlers[name](data),
    postedNames: () => posted.map(([name]) => name),
    lastPosted: (name) => posted.filter(([n]) => n === name).pop()?.[1],
  };
}

function fakeApiClient(overrides = {}) {
  return {
    accessToken: () => 'tok',
    serverAddress: () => `${SERVER}//`,
    getCurrentUserId: () => 'user-1',
    serverId: () => 'server-1',
    serverInfo: () => ({ Id: 'info-server', Name: 'Mock Jellyfin', UserId: 'info-user' }),
    getUrl: (path, params) => `${SERVER}/${path}?${new URLSearchParams(params)}`,
    getItems: vi.fn(async () => ({ Items: [] })),
    ...overrides,
  };
}

function connect(shell, apiClient) {
  shell.state.ServerConnections = { currentApiClient: () => apiClient };
  return apiClient;
}

const MOVIE = { Id: 'movie-1', Name: 'Big Film', Type: 'Movie', MediaType: 'Video' };
const SONG = { Id: 'song-1', Name: 'Tune', Type: 'Audio', MediaType: 'Audio' };
const EPISODE = {
  Id: 'ep-1',
  Name: 'Pilot',
  Type: 'Episode',
  MediaType: 'Video',
  SeriesName: 'Show',
  ParentIndexNumber: 1,
  IndexNumber: 2,
};
const SERIES = { Id: 'series-1', Name: 'Show', Type: 'Series', IsFolder: true };

function detailButtonsRow() {
  const row = document.createElement('div');
  row.className = 'mainDetailButtons';
  document.body.appendChild(row);
  return row;
}

const nextTick = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('iina shell for Jellyfin Web', () => {
  afterEach(() => {
    if (window.__iinaShell) {
      window.__iinaShell.stop();
    }
    vi.useRealTimers();
    vi.restoreAllMocks();
    delete window.iina;
    if (!document.body) {
      document.documentElement.appendChild(document.createElement('body'));
    }
    window.localStorage.clear();
  });

  describe('bridge', () => {
    it('asks the plugin for the identity and the downloads at load', async () => {
      const { postedNames, handlers } = await loadShell();
      expect(postedNames()).toEqual(['get-client-identity', 'get-offline-downloads']);
      expect(Object.keys(handlers)).toEqual(['client-identity', 'offline-downloads']);
    });

    it('works without the bridge (plain browser)', async () => {
      const { shell, handlers } = await loadShell({ bridge: false });
      expect(handlers).toEqual({});
      expect(shell.publishSession()).toBe(false);
      expect(shell.downloadsPanel.open()).toBeUndefined();
    });

    it('does not register listeners on a bridge without onMessage', async () => {
      const { shell } = await loadShell({ bridge: { postMessage: vi.fn() } });
      expect(window.iina.postMessage).toHaveBeenCalledWith('get-client-identity', {});
      expect(shell.state.identity).toBeNull();
    });

    it('treats a bridge without postMessage as absent', async () => {
      await loadShell({ bridge: { onMessage: vi.fn() } });
      expect(window.NativeShell.AppHost.deviceId()).toMatch(/^iina-web-/);
    });

    it('keeps the identity the plugin sends', async () => {
      const { shell, emit } = await loadShell();
      emit('client-identity', { clientName: 'no device id' });
      expect(shell.state.identity).toBeNull();
      emit('client-identity', { deviceId: 'dev-1', clientName: 'IINA Jellyfin', version: '1.2.3' });
      expect(window.NativeShell.AppHost.deviceId()).toBe('dev-1');
      expect(window.NativeShell.AppHost.appName()).toBe('IINA Jellyfin');
      expect(window.NativeShell.AppHost.appVersion()).toBe('1.2.3');
    });
  });

  describe('session', () => {
    it('is null without a signed-in api client', async () => {
      const { shell } = await loadShell();
      expect(shell.currentSession()).toBeNull();
      shell.state.ServerConnections = {};
      expect(shell.currentSession()).toBeNull();
      connect(shell, null);
      expect(shell.currentSession()).toBeNull();
      connect(shell, fakeApiClient({ accessToken: () => '' }));
      expect(shell.currentSession()).toBeNull();
      connect(shell, fakeApiClient({ serverAddress: () => '' }));
      expect(shell.currentSession()).toBeNull();
    });

    it('describes the signed-in session', async () => {
      const { shell } = await loadShell();
      connect(shell, fakeApiClient());
      expect(shell.currentSession()).toEqual({
        serverUrl: SERVER,
        accessToken: 'tok',
        userId: 'user-1',
        serverId: 'server-1',
        serverName: 'Mock Jellyfin',
      });
    });

    it('falls back to the server info and then to null', async () => {
      const { shell } = await loadShell();
      connect(
        shell,
        fakeApiClient({
          getCurrentUserId: () => '',
          serverId: () => null,
        })
      );
      expect(shell.currentSession()).toEqual(
        expect.objectContaining({ userId: 'info-user', serverId: 'info-server' })
      );

      connect(
        shell,
        fakeApiClient({ getCurrentUserId: () => '', serverId: () => null, serverInfo: () => null })
      );
      expect(shell.currentSession()).toEqual(
        expect.objectContaining({ userId: null, serverId: null, serverName: null })
      );

      connect(
        shell,
        fakeApiClient({ getCurrentUserId: () => '', serverId: () => null, serverInfo: undefined })
      );
      expect(shell.currentSession()).toEqual(
        expect.objectContaining({ userId: null, serverId: null, serverName: null })
      );
    });

    it('publishes each session once', async () => {
      const { shell, posted } = await loadShell();
      expect(shell.publishSession()).toBe(false);
      connect(shell, fakeApiClient());
      expect(shell.publishSession('tester')).toBe(true);
      expect(posted.pop()).toEqual([
        'store-session',
        {
          serverUrl: SERVER,
          accessToken: 'tok',
          userId: 'user-1',
          serverId: 'server-1',
          serverName: 'Mock Jellyfin',
          username: 'tester',
        },
      ]);
      expect(shell.publishSession('tester')).toBe(false);

      connect(shell, fakeApiClient({ accessToken: () => 'tok-2' }));
      expect(shell.publishSession()).toBe(true);
      expect(posted.pop()[1]).toEqual(
        expect.objectContaining({ accessToken: 'tok-2', username: null })
      );
    });
  });

  describe('NativeShell', () => {
    it('reports the supported features', async () => {
      await loadShell();
      const { AppHost } = window.NativeShell;
      expect(AppHost.supports('FileDownload')).toBe(true);
      expect(AppHost.supports('downloadmanagement')).toBe(true);
      expect(AppHost.supports('exit')).toBe(false);
      expect(AppHost.supports()).toBe(false);
      expect(AppHost.init()).toBeUndefined();
      expect(AppHost.exit()).toBeUndefined();
      expect(AppHost.getDefaultLayout()).toBe('desktop');
      expect(AppHost.deviceName()).toBe('IINA');
      expect(AppHost.appName()).toBe('IINA Jellyfin Plugin');
      expect(AppHost.appVersion()).toBe('0.0.0');
      const builder = vi.fn(() => ({ Name: 'profile' }));
      expect(AppHost.getDeviceProfile(builder)).toEqual({ Name: 'profile' });
      expect(builder).toHaveBeenCalledWith({});
      expect(window.NativeShell.getPlugins()).toEqual(['IinaPlayerPlugin']);
    });

    it('remembers a generated device id in local storage', async () => {
      await loadShell();
      const id = window.NativeShell.AppHost.deviceId();
      expect(id).toMatch(/^iina-web-/);
      expect(window.localStorage.getItem('iina-jellyfin-device-id')).toBe(id);
      expect(window.NativeShell.AppHost.deviceId()).toBe(id);
    });

    it('uses a fixed id when local storage is unavailable', async () => {
      await loadShell();
      vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
        throw new Error('blocked');
      });
      expect(window.NativeShell.AppHost.deviceId()).toBe('iina-web');
    });

    it('turns file downloads into offline downloads', async () => {
      const { shell, posted } = await loadShell();
      window.NativeShell.downloadFiles([{ itemId: 'movie-1' }]);
      expect(posted.some(([name]) => name === 'offline-download-by-id')).toBe(false);

      connect(shell, fakeApiClient());
      shell.state.quality = '4000';
      window.NativeShell.downloadFiles([{ itemId: 'movie-1' }, null, { url: 'x' }]);
      window.NativeShell.downloadFiles();
      expect(posted.filter(([name]) => name === 'offline-download-by-id')).toEqual([
        [
          'offline-download-by-id',
          {
            itemId: 'movie-1',
            serverUrl: SERVER,
            accessToken: 'tok',
            serverId: 'server-1',
            quality: '4000',
          },
        ],
      ]);
    });

    it('opens the downloads panel from the user menu', async () => {
      const { shell } = await loadShell();
      window.NativeShell.openDownloadManager();
      expect(shell.downloadsPanel.isOpen()).toBe(true);
    });

    it('opens external urls through the plugin, or the browser without it', async () => {
      const { posted } = await loadShell();
      window.NativeShell.openUrl('http://x/page');
      expect(posted.pop()).toEqual(['open-external-url', { url: 'http://x/page' }]);

      await loadShell({ bridge: false });
      window.open = vi.fn();
      window.NativeShell.openUrl('http://x/page');
      expect(window.open).toHaveBeenCalledWith('http://x/page', '_blank');
    });
  });

  describe('playback in IINA', () => {
    it('names items the way the download does', async () => {
      const { shell } = await loadShell();
      expect(shell.displayTitle(EPISODE)).toBe('Show S01E02 - Pilot');
      expect(shell.displayTitle({ ...EPISODE, Name: '' })).toBe('Show S01E02 - Unknown Title');
      expect(shell.displayTitle({ ...EPISODE, ParentIndexNumber: undefined })).toBe('Show - Pilot');
      expect(shell.displayTitle({ ...EPISODE, ParentIndexNumber: null })).toBe('Show - Pilot');
      expect(shell.displayTitle({ ...EPISODE, IndexNumber: undefined })).toBe('Show - Pilot');
      expect(shell.displayTitle({ ...EPISODE, IndexNumber: null })).toBe('Show - Pilot');
      expect(shell.displayTitle({ ...EPISODE, SeriesName: '' })).toBe('Pilot');
      expect(shell.displayTitle(MOVIE)).toBe('Big Film');
      expect(shell.displayTitle({ Id: 'x' })).toBe('Unknown Title');
    });

    it('does nothing without the bridge, a client or a request', async () => {
      const noBridge = await loadShell({ bridge: false });
      connect(noBridge.shell, fakeApiClient());
      expect(await noBridge.shell.playInIina({ items: [MOVIE] })).toBe(false);

      const { shell } = await loadShell();
      expect(await shell.playInIina({ items: [MOVIE] })).toBe(false);
      connect(shell, fakeApiClient());
      expect(await shell.playInIina(null)).toBe(false);
      expect(await shell.playInIina({})).toBe(false);
      expect(await shell.playInIina({ ids: [] })).toBe(false);
      expect(await shell.playInIina({ ids: 'movie-1' })).toBe(false);
      expect(await shell.playInIina({ items: [{ Id: 'photo', MediaType: 'Photo' }] })).toBe(false);
      expect(await shell.playInIina({ items: [null, { Name: 'no id' }] })).toBe(false);
    });

    it('plays a single item with its stream url and publishes the session', async () => {
      const { shell, posted } = await loadShell();
      connect(shell, fakeApiClient());
      expect(await shell.playInIina({ items: [MOVIE] })).toBe(true);
      expect(posted.slice(-2)).toEqual([
        ['store-session', expect.objectContaining({ accessToken: 'tok' })],
        [
          'play-media',
          {
            streamUrl: `${SERVER}/Videos/movie-1/stream?static=true&ApiKey=tok`,
            title: 'Big Film',
            itemId: 'movie-1',
          },
        ],
      ]);

      expect(await shell.playInIina({ items: [SONG] })).toBe(true);
      expect(posted.pop()[1].streamUrl).toBe(
        `${SERVER}/Audio/song-1/stream?static=true&ApiKey=tok`
      );
    });

    it('resolves ids and folders into a play list', async () => {
      const { shell, posted } = await loadShell();
      const apiClient = connect(shell, fakeApiClient());
      apiClient.getItems.mockImplementation(async (userId, query) => {
        expect(userId).toBe('user-1');
        if (query.Ids) {
          return { Items: [EPISODE, SERIES] };
        }
        if (query.ParentId === 'series-1') {
          return { Items: [MOVIE, { Id: 'folder', IsFolder: true }] };
        }
        return null;
      });

      expect(await shell.playInIina({ ids: ['series-1', 'ep-1', 'missing'] })).toBe(true);
      expect(posted.pop()).toEqual([
        'play-media-list',
        {
          items: [
            expect.objectContaining({ itemId: 'movie-1', title: 'Big Film' }),
            expect.objectContaining({ itemId: 'ep-1', title: 'Show S01E02 - Pilot' }),
          ],
        },
      ]);
      expect(apiClient.getItems).toHaveBeenCalledWith('user-1', {
        Ids: 'series-1,ep-1,missing',
        Fields: 'MediaSources',
      });
      expect(apiClient.getItems).toHaveBeenCalledWith(
        'user-1',
        expect.objectContaining({ ParentId: 'series-1', Recursive: true, Filters: 'IsNotFolder' })
      );

      // Empty answers are handled
      apiClient.getItems.mockResolvedValue(null);
      expect(await shell.playInIina({ ids: ['x'] })).toBe(false);
      expect(await shell.playInIina({ items: [{ Id: 'empty-folder' }] })).toBe(false);
    });

    it('honours the start index and shuffle', async () => {
      const { shell, posted } = await loadShell();
      connect(shell, fakeApiClient());
      const items = [MOVIE, EPISODE, SONG];

      expect(await shell.playInIina({ items, startIndex: 1 })).toBe(true);
      expect(posted.pop()[1].items.map((entry) => entry.itemId)).toEqual(['ep-1', 'song-1']);

      expect(await shell.playInIina({ items, startIndex: 0 })).toBe(true);
      expect(posted.pop()[1].items).toHaveLength(3);

      vi.spyOn(Math, 'random').mockReturnValue(0);
      expect(await shell.playInIina({ items, shuffle: true })).toBe(true);
      expect(posted.pop()[1].items.map((entry) => entry.itemId)).toEqual([
        'ep-1',
        'song-1',
        'movie-1',
      ]);
    });
  });

  describe('player plugin', () => {
    function deps(overrides = {}) {
      const connections = { currentApiClient: () => fakeApiClient() };
      const playbackManager = { play: vi.fn(() => 'original-result') };
      return {
        ServerConnections: connections,
        playbackManager,
        events: { on: vi.fn() },
        toast: vi.fn(),
        ...overrides,
      };
    }

    it('is loaded through the window and never chosen as a player', async () => {
      const { shell } = await loadShell();
      const Plugin = await window.IinaPlayerPlugin();
      expect(Plugin).toBe(shell.IinaPlayerPlugin);
      const plugin = new Plugin(deps());
      expect(plugin).toMatchObject({ name: 'IINA', type: 'mediaplayer', id: 'iinaplayer' });
      expect(plugin.canPlayMediaType('Video')).toBe(false);
    });

    it('routes play requests to IINA and falls back to the page player', async () => {
      const { posted } = await loadShell();
      const Plugin = await window.IinaPlayerPlugin();
      const d = deps();
      const original = d.playbackManager.play;
      new Plugin(d);
      expect(posted.pop()[0]).toBe('store-session');

      expect(await d.playbackManager.play({ items: [MOVIE] })).toBeUndefined();
      expect(posted.pop()[0]).toBe('play-media');
      expect(original).not.toHaveBeenCalled();

      const photos = { items: [{ Id: 'p', MediaType: 'Photo' }] };
      expect(await d.playbackManager.play(photos)).toBe('original-result');
      expect(original).toHaveBeenCalledWith(photos);
    });

    it('falls back when handing over fails', async () => {
      await loadShell();
      const Plugin = await window.IinaPlayerPlugin();
      const d = deps();
      d.ServerConnections.currentApiClient = () => ({
        accessToken: () => 'tok',
        serverAddress: () => SERVER,
        getCurrentUserId: () => 'u',
        serverId: () => 's',
        getItems: async () => {
          throw new Error('boom');
        },
      });
      new Plugin(d);
      const error = vi.spyOn(console, 'error').mockImplementation(() => {});
      expect(await d.playbackManager.play({ ids: ['x'] })).toBe('original-result');
      expect(error).toHaveBeenCalledWith(
        '[iina] could not hand playback to IINA',
        expect.any(Error)
      );
    });

    it('copes with a missing playback manager or event bus', async () => {
      await loadShell();
      const Plugin = await window.IinaPlayerPlugin();
      expect(() => new Plugin(deps({ playbackManager: null, events: null }))).not.toThrow();
      expect(() => new Plugin(deps({ playbackManager: {}, events: {} }))).not.toThrow();
      expect(
        () =>
          new Plugin(deps({ ServerConnections: null, playbackManager: {}, events: { on() {} } }))
      ).not.toThrow();
    });

    it('publishes the session again when a user signs in', async () => {
      const { posted } = await loadShell();
      const Plugin = await window.IinaPlayerPlugin();
      const d = deps();
      new Plugin(d);
      const [target, eventName, callback] = d.events.on.mock.calls[0];
      expect(target).toBe(d.ServerConnections);
      expect(eventName).toBe('localusersignedin');

      d.ServerConnections.currentApiClient = () => fakeApiClient({ accessToken: () => 'new' });
      callback({}, { Name: 'tester' });
      expect(posted.pop()).toEqual([
        'store-session',
        expect.objectContaining({ accessToken: 'new', username: 'tester' }),
      ]);

      d.ServerConnections.currentApiClient = () => fakeApiClient({ accessToken: () => 'newer' });
      callback({}, null);
      expect(posted.pop()[1].username).toBeNull();
    });
  });

  describe('offline button on item pages', () => {
    it('describes every download state', async () => {
      const { shell } = await loadShell();
      const { describeButton } = shell;
      expect(describeButton(null)).toEqual(
        expect.objectContaining({ state: 'idle', label: 'Offline' })
      );
      expect(describeButton({ status: 'cancelled' }).state).toBe('idle');
      expect(describeButton({ status: 'queued' })).toEqual(
        expect.objectContaining({ state: 'queued', disabled: true })
      );
      expect(describeButton({ status: 'downloading', progress: 42 })).toEqual(
        expect.objectContaining({ label: '42%', state: 'downloading', disabled: true })
      );
      expect(describeButton({ status: 'downloading' }).label).toBe('0%');
      expect(describeButton({ status: 'downloading', transcoded: true }).label).toBe(
        'Transcoding…'
      );
      expect(describeButton({ status: 'completed' })).toEqual(
        expect.objectContaining({ label: 'Play offline', state: 'completed', disabled: false })
      );
      expect(describeButton({ status: 'completed', fileMissing: true })).toEqual(
        expect.objectContaining({ label: 'Re-download', state: 'missing' })
      );
      expect(describeButton({ status: 'failed' })).toEqual(
        expect.objectContaining({ label: 'Retry download', state: 'failed' })
      );
    });

    it('adds the button to detail pages as they render', async () => {
      const { shell, posted, emit } = await loadShell();
      const row = detailButtonsRow();
      await nextTick();
      const button = row.querySelector('.btnIinaOffline');
      expect(button).not.toBeNull();
      expect(button.classList.contains('hide')).toBe(true);

      // A second pass adds nothing
      shell.ensureDetailButtons();
      expect(row.querySelectorAll('.btnIinaOffline')).toHaveLength(1);

      window.location.hash = '#/details?id=movie%2D1&serverId=server-1';
      window.dispatchEvent(new Event('hashchange'));
      expect(button.classList.contains('hide')).toBe(false);
      expect(button.dataset.itemId).toBe('movie-1');
      expect(button.dataset.offlineState).toBe('idle');
      expect(button.querySelector('.iina-offline-label').textContent).toBe('Offline');

      // Clicking without a session does nothing; with one it requests the download
      button.click();
      expect(posted.some(([name]) => name === 'offline-download-by-id')).toBe(false);
      connect(shell, fakeApiClient());
      button.click();
      expect(posted.pop()).toEqual([
        'offline-download-by-id',
        expect.objectContaining({ itemId: 'movie-1', quality: 'original' }),
      ]);

      emit('offline-downloads', {
        downloads: [{ itemId: 'movie-1', status: 'downloading', progress: 5 }],
      });
      expect(button.disabled).toBe(true);
      expect(button.dataset.offlineState).toBe('downloading');
      expect(button.querySelector('.detailButton-icon').className).toContain('downloading');

      emit('offline-downloads', { downloads: [{ itemId: 'movie-1', status: 'completed' }] });
      expect(button.disabled).toBe(false);
      button.click();
      expect(posted.pop()).toEqual(['play-offline', { itemId: 'movie-1' }]);

      window.location.hash = '#/home';
      window.dispatchEvent(new Event('hashchange'));
      expect(button.classList.contains('hide')).toBe(true);
    });

    it('tolerates buttons without the inner elements', async () => {
      const { shell } = await loadShell();
      window.location.hash = '#/details?id=movie-1';
      const bare = document.createElement('button');
      bare.className = 'btnIinaOffline';
      document.body.appendChild(bare);
      shell.refreshDetailButtons();
      expect(bare.dataset.itemId).toBe('movie-1');
      expect(bare.title).toBe('Offline');
    });

    it('starts once the body exists', async () => {
      const { shell } = await loadShell({ withoutBody: true });
      expect(document.body).toBeNull();
      shell.stop(); // nothing to disconnect yet
      const body = document.createElement('body');
      document.documentElement.appendChild(body);
      const row = detailButtonsRow();
      document.dispatchEvent(new Event('DOMContentLoaded'));
      expect(row.querySelector('.btnIinaOffline')).not.toBeNull();
      expect(shell.state.downloads).toEqual([]);
    });

    it('ignores incomplete state messages', async () => {
      const { shell, emit } = await loadShell();
      emit('offline-downloads', null);
      emit('offline-downloads', { downloads: 'nope', qualityPresets: [], quality: '' });
      expect(shell.state.downloads).toEqual([]);
      expect(shell.state.directory).toBeNull();
      expect(shell.state.quality).toBe('original');
      expect(shell.state.qualityPresets).toHaveLength(7);

      emit('offline-downloads', {
        downloads: [],
        directory: '/Volumes/Media',
        quality: '2000',
        qualityPresets: [{ id: '2000', label: '2 Mb/s' }],
        error: 'Disk full',
      });
      expect(shell.state).toEqual(
        expect.objectContaining({ directory: '/Volumes/Media', quality: '2000' })
      );
      expect(shell.state.qualityPresets).toEqual([{ id: '2000', label: '2 Mb/s' }]);
      expect(document.querySelector('.iina-downloads-notice').textContent).toBe('Disk full');
    });
  });

  describe('downloads panel', () => {
    function completed(overrides = {}) {
      return {
        itemId: 'movie-1',
        title: 'Big Film (2020)',
        status: 'completed',
        createdAt: 10,
        subtitles: [{}, {}],
        quality: 'original',
        ...overrides,
      };
    }

    it('opens, closes and renders the state', async () => {
      vi.useFakeTimers();
      const { shell, posted, emit } = await loadShell();
      const panel = shell.downloadsPanel;
      expect(panel.isOpen()).toBe(false);
      panel.close();

      panel.open();
      expect(panel.isOpen()).toBe(true);
      expect(posted.pop()).toEqual(['get-offline-downloads', {}]);
      const element = document.querySelector('.iina-downloads');
      expect(element.querySelector('.iina-downloads-empty')).not.toBeNull();
      expect(element.querySelectorAll('.iina-downloads-quality option')).toHaveLength(7);
      expect(element.querySelector('.iina-downloads-directory').textContent).toBe('');

      emit('offline-downloads', {
        directory: '/Volumes/Media',
        quality: '4000',
        downloads: [
          completed({ createdAt: 1 }),
          { itemId: 'old', name: 'Named', status: 'failed', error: 'timeout', createdAt: 2 },
          { itemId: 'q', status: 'queued' },
          { itemId: 'd', status: 'downloading', progress: 150, createdAt: 5 },
          { itemId: 'neg', status: 'downloading', progress: -5, createdAt: 4 },
          { itemId: 't', status: 'downloading', transcoded: true, createdAt: 3 },
          { itemId: 'c', status: 'cancelled', createdAt: 6 },
        ],
      });
      expect(element.querySelector('.iina-downloads-directory').textContent).toBe(
        'Folder: /Volumes/Media'
      );
      expect(element.querySelector('.iina-downloads-quality').value).toBe('4000');
      const items = Array.from(element.querySelectorAll('.iina-downloads-item'));
      expect(items.map((item) => item.dataset.downloadId)).toEqual([
        'c',
        'd',
        'neg',
        't',
        'old',
        'movie-1',
        'q',
      ]);
      const status = (id) =>
        element.querySelector(`[data-download-id="${id}"] .iina-downloads-status`).textContent;
      expect(status('movie-1')).toBe('Ready to play offline · 2 subtitle(s)');
      expect(status('old')).toBe('Failed: timeout');
      expect(status('q')).toBe('Queued');
      expect(status('d')).toBe('Downloading 150%');
      expect(status('t')).toBe('Transcoding and downloading…');
      expect(status('c')).toBe('Cancelling…');
      const width = (id) =>
        element.querySelector(`[data-download-id="${id}"] .iina-downloads-progress div`).style
          .width;
      expect(width('d')).toBe('100%');
      expect(width('neg')).toBe('0%');
      expect(width('t')).toBe('100%');
      expect(
        element.querySelector('[data-download-id="old"] .iina-downloads-title').textContent
      ).toBe('Named');
      expect(
        element.querySelector('[data-download-id="q"] .iina-downloads-title').textContent
      ).toBe('q');
      const actions = (id) =>
        Array.from(element.querySelectorAll(`[data-download-id="${id}"] [data-action]`)).map(
          (button) => button.dataset.action
        );
      expect(actions('movie-1')).toEqual(['play', 'reveal', 'remove']);
      expect(actions('old')).toEqual(['retry', 'remove']);
      expect(actions('q')).toEqual(['cancel']);
      expect(actions('c')).toEqual([]);

      // Clicking the backdrop closes, clicking inside does not
      element.querySelector('.iina-downloads-dialog').click();
      expect(panel.isOpen()).toBe(true);
      element.click();
      expect(panel.isOpen()).toBe(false);
      panel.open();
      element.querySelector('.iina-downloads-close').click();
      expect(panel.isOpen()).toBe(false);
      expect(document.querySelectorAll('.iina-downloads')).toHaveLength(1);
    });

    it('describes the remaining statuses', async () => {
      const { shell } = await loadShell();
      const { describeStatus } = shell.downloadsPanel;
      expect(describeStatus({ status: 'downloading' })).toBe('Downloading 0%');
      expect(describeStatus({ status: 'failed' })).toBe('Failed: unknown error');
      expect(describeStatus({ status: 'completed', fileMissing: true })).toBe('File missing');
      expect(describeStatus({ status: 'completed' })).toBe('Ready to play offline · 0 subtitle(s)');
      expect(
        describeStatus({
          status: 'completed',
          quality: '4000',
          qualityLabel: '4 Mb/s',
          subtitles: [],
        })
      ).toBe('Ready to play offline · 4 Mb/s · 0 subtitle(s)');
      expect(
        describeStatus({ status: 'completed', quality: 'original', qualityLabel: 'Original' })
      ).toBe('Ready to play offline · 0 subtitle(s)');
    });

    it('sends the toolbar actions to the plugin', async () => {
      const { shell, posted } = await loadShell();
      shell.downloadsPanel.open();
      const element = document.querySelector('.iina-downloads');
      element.querySelector('.iina-downloads-open-folder').click();
      expect(posted.pop()).toEqual(['offline-open-folder', undefined]);
      element.querySelector('.iina-downloads-change-folder').click();
      expect(posted.pop()).toEqual(['offline-choose-folder', undefined]);
      const select = element.querySelector('.iina-downloads-quality');
      select.value = '2000';
      select.dispatchEvent(new Event('change'));
      expect(shell.state.quality).toBe('2000');
      expect(posted.pop()).toEqual(['offline-set-quality', { quality: '2000' }]);
    });

    it('handles the entry actions', async () => {
      vi.useFakeTimers();
      const { shell, posted, emit } = await loadShell();
      shell.downloadsPanel.open();
      emit('offline-downloads', {
        downloads: [
          completed(),
          { itemId: 'old', status: 'failed' },
          { itemId: 'q', status: 'queued' },
        ],
      });
      const element = document.querySelector('.iina-downloads');
      const click = (selector) => element.querySelector(selector).click();

      click('[data-action="play"][data-item-id="movie-1"]');
      expect(posted.pop()).toEqual(['play-offline', { itemId: 'movie-1' }]);
      click('[data-action="reveal"][data-item-id="movie-1"]');
      expect(posted.pop()).toEqual(['offline-show-in-finder', { itemId: 'movie-1' }]);
      click('[data-action="cancel"][data-item-id="q"]');
      expect(posted.pop()).toEqual(['offline-cancel', { itemId: 'q' }]);

      click('[data-action="retry"][data-item-id="old"]');
      expect(posted.pop()).toEqual([
        'offline-retry',
        { itemId: 'old', serverUrl: null, accessToken: null },
      ]);
      connect(shell, fakeApiClient());
      click('[data-action="retry"][data-item-id="old"]');
      expect(posted.pop()).toEqual([
        'offline-retry',
        { itemId: 'old', serverUrl: SERVER, accessToken: 'tok' },
      ]);

      // Clicks that are not on an action button, and unknown actions, are ignored
      const before = posted.length;
      element.querySelector('.iina-downloads-title').click();
      shell.downloadsPanel.handleAction('unknown', 'movie-1', null);
      expect(posted).toHaveLength(before);
    });

    it('asks for confirmation before removing', async () => {
      vi.useFakeTimers();
      const { shell, posted, emit } = await loadShell();
      shell.downloadsPanel.open();
      emit('offline-downloads', { downloads: [completed(), completed({ itemId: 'other' })] });
      const element = document.querySelector('.iina-downloads');
      const removeButton = () =>
        element.querySelector('[data-action="remove"][data-item-id="movie-1"]');

      removeButton().click();
      expect(removeButton().textContent).toBe('Confirm?');
      expect(posted.some(([name]) => name === 'offline-remove')).toBe(false);

      // The armed state survives a re-render and expires after a while
      emit('offline-downloads', { downloads: [completed(), completed({ itemId: 'other' })] });
      expect(removeButton().textContent).toBe('Confirm?');
      vi.advanceTimersByTime(4000);
      expect(removeButton().textContent).toBe('Remove');

      removeButton().click();
      removeButton().click();
      expect(posted.pop()).toEqual(['offline-remove', { itemId: 'movie-1' }]);
      vi.advanceTimersByTime(4000);

      // Arming one entry and then another keeps only the latest armed
      removeButton().click();
      element.querySelector('[data-action="remove"][data-item-id="other"]').click();
      expect(shell.downloadsPanel.pendingRemoveId).toBe('other');
      vi.advanceTimersByTime(4000);
      expect(shell.downloadsPanel.pendingRemoveId).toBeNull();
    });

    it('shows notices for a while', async () => {
      vi.useFakeTimers();
      const { shell } = await loadShell();
      shell.downloadsPanel.notice('Disk full');
      const box = document.querySelector('.iina-downloads-notice');
      expect(box.classList.contains('hide')).toBe(false);
      shell.downloadsPanel.notice('Still full');
      vi.advanceTimersByTime(4999);
      expect(box.classList.contains('hide')).toBe(false);
      vi.advanceTimersByTime(1);
      expect(box.classList.contains('hide')).toBe(true);
    });

    it('escapes what it renders', async () => {
      const { shell, emit } = await loadShell();
      shell.downloadsPanel.open();
      emit('offline-downloads', {
        downloads: [
          completed({ itemId: 'a"b', title: `<b>&'x'</b>`, createdAt: 3 }),
          { itemId: 'bare', createdAt: 2 },
          { itemId: 'p0', status: 'downloading', createdAt: 1 },
        ],
        qualityPresets: [{ id: '<1>', label: 'A & B' }],
      });
      const element = document.querySelector('.iina-downloads');
      expect(element.querySelector('[data-download-id="bare"]').className).toBe(
        'iina-downloads-item status-'
      );
      expect(
        element.querySelector('[data-download-id="p0"] .iina-downloads-progress div').style.width
      ).toBe('0%');
      expect(element.querySelector('.iina-downloads-title').textContent).toBe(`<b>&'x'</b>`);
      expect(element.querySelector('.iina-downloads-title b')).toBeNull();
      expect(element.querySelector('.iina-downloads-item').dataset.downloadId).toBe('a"b');
      expect(element.querySelector('.iina-downloads-quality option').value).toBe('<1>');
    });
  });

  describe('details that matter', () => {
    it('lists the quality presets with their ids and labels', async () => {
      const { shell } = await loadShell();
      shell.downloadsPanel.open();
      const select = document.querySelector('.iina-downloads-quality');
      expect(select.childNodes).toHaveLength(7);
      expect(
        Array.from(select.options).map((option) => [option.value, option.textContent])
      ).toEqual([
        ['original', 'Original quality'],
        ['8000', '8 Mb/s'],
        ['4000', '4 Mb/s'],
        ['2000', '2 Mb/s'],
        ['1000', '1 Mb/s'],
        ['500', '500 Kb/s'],
        ['250', '250 Kb/s'],
      ]);
    });

    it('generates a plain device id', async () => {
      await loadShell();
      expect(window.NativeShell.AppHost.deviceId()).toMatch(/^iina-web-[0-9a-z]+$/);
    });

    it('only formats episodes with the series name', async () => {
      const { shell } = await loadShell();
      expect(shell.displayTitle({ ...MOVIE, SeriesName: 'Show' })).toBe('Big Film');
    });

    it('asks the server for the children in play order', async () => {
      const { shell } = await loadShell();
      const apiClient = connect(shell, fakeApiClient());
      apiClient.getItems.mockResolvedValue({ Items: [MOVIE] });
      await shell.playInIina({ items: [SERIES] });
      expect(apiClient.getItems).toHaveBeenCalledWith('user-1', {
        ParentId: 'series-1',
        Recursive: true,
        Filters: 'IsNotFolder',
        MediaTypes: 'Video,Audio',
        SortBy: 'ParentIndexNumber,IndexNumber,SortName',
        Fields: 'MediaSources',
        Limit: 1000,
      });
    });

    it('prefers the items over the ids and never resolves an empty id list', async () => {
      const { shell, posted } = await loadShell();
      const apiClient = connect(shell, fakeApiClient());
      expect(await shell.playInIina({ items: [MOVIE], ids: ['song-1'] })).toBe(true);
      expect(apiClient.getItems).not.toHaveBeenCalled();
      expect(posted.pop()[1].itemId).toBe('movie-1');
      expect(await shell.playInIina({ ids: [] })).toBe(false);
      expect(apiClient.getItems).not.toHaveBeenCalled();
    });

    it('shuffles a copy of the list', async () => {
      const { shell, posted } = await loadShell();
      connect(shell, fakeApiClient());
      const items = [MOVIE, EPISODE, SONG];
      vi.spyOn(Math, 'random').mockReturnValue(0.99);
      await shell.playInIina({ items, shuffle: true });
      expect(posted.pop()[1].items.map((entry) => entry.itemId)).toEqual([
        'movie-1',
        'ep-1',
        'song-1',
      ]);
      Math.random.mockReturnValue(0);
      await shell.playInIina({ items, shuffle: true });
      expect(posted.pop()[1].items.map((entry) => entry.itemId)).toEqual([
        'ep-1',
        'song-1',
        'movie-1',
      ]);
      expect(items).toEqual([MOVIE, EPISODE, SONG]);
    });

    it('registers as a low-priority local player', async () => {
      await loadShell();
      const Plugin = await window.IinaPlayerPlugin();
      const plugin = new Plugin({ ServerConnections: null, playbackManager: null, events: null });
      expect(plugin.priority).toBe(-1);
      expect(plugin.isLocalPlayer).toBe(true);
    });

    it('describes the button icons and enabled states', async () => {
      const { shell } = await loadShell();
      const { describeButton } = shell;
      expect(describeButton(null)).toEqual({
        label: 'Offline',
        icon: 'file_download',
        state: 'idle',
        disabled: false,
      });
      expect(describeButton({ status: 'queued' })).toEqual({
        label: 'Queued…',
        icon: 'hourglass_empty',
        state: 'queued',
        disabled: true,
      });
      expect(describeButton({ status: 'downloading', progress: 7 })).toEqual({
        label: '7%',
        icon: 'downloading',
        state: 'downloading',
        disabled: true,
      });
      expect(describeButton({ status: 'completed' })).toEqual({
        label: 'Play offline',
        icon: 'offline_pin',
        state: 'completed',
        disabled: false,
      });
      expect(describeButton({ status: 'completed', fileMissing: true })).toEqual({
        label: 'Re-download',
        icon: 'file_download',
        state: 'missing',
        disabled: false,
      });
      expect(describeButton({ status: 'failed' })).toEqual({
        label: 'Retry download',
        icon: 'refresh',
        state: 'failed',
        disabled: false,
      });
    });

    it('finds the entry of the page item and only plays completed copies', async () => {
      const { shell, posted, emit } = await loadShell();
      connect(shell, fakeApiClient());
      window.location.hash = '#/details?id=movie-1';
      const wrapper = document.createElement('div');
      document.body.appendChild(wrapper);
      await nextTick();
      const row = document.createElement('div');
      row.className = 'mainDetailButtons';
      wrapper.appendChild(row);
      await nextTick();
      const button = row.querySelector('.btnIinaOffline');
      expect(button.type).toBe('button');

      emit('offline-downloads', {
        downloads: [
          { itemId: 'other', status: 'completed' },
          { itemId: 'movie-1', status: 'failed' },
        ],
      });
      expect(button.dataset.offlineState).toBe('failed');
      expect(button.querySelector('.iina-offline-label').textContent).toBe('Retry download');
      const rowClicks = vi.fn();
      row.addEventListener('click', rowClicks);
      const event = new MouseEvent('click', { bubbles: true, cancelable: true });
      button.dispatchEvent(event);
      expect(event.defaultPrevented).toBe(true);
      expect(rowClicks).not.toHaveBeenCalled();
      expect(posted.pop()[0]).toBe('offline-download-by-id');

      emit('offline-downloads', {
        downloads: [
          { itemId: 'other', status: 'failed' },
          { itemId: 'movie-1', status: 'completed', fileMissing: true },
        ],
      });
      button.click();
      expect(posted.pop()[0]).toBe('offline-download-by-id');

      // After stop the page is left alone
      shell.stop();
      window.location.hash = '#/home';
      window.dispatchEvent(new Event('hashchange'));
      expect(button.classList.contains('hide')).toBe(false);
    });

    it('refreshes buttons that already exist at load', async () => {
      const { shell } = await loadShell({
        prepare: () => {
          window.location.hash = '#/details?id=ep-1';
          const bare = document.createElement('button');
          bare.className = 'btnIinaOffline';
          document.body.appendChild(bare);
        },
      });
      expect(document.querySelector('.btnIinaOffline').dataset.itemId).toBe('ep-1');
      expect(shell.state.downloads).toEqual([]);
    });

    it('does not build the panel for a state without an error', async () => {
      const { emit } = await loadShell();
      emit('offline-downloads', { downloads: [], error: null });
      expect(document.querySelector('.iina-downloads')).toBeNull();
    });

    it('extends the notice when a new one arrives', async () => {
      vi.useFakeTimers();
      const { shell } = await loadShell();
      shell.downloadsPanel.notice('One');
      vi.advanceTimersByTime(3000);
      shell.downloadsPanel.notice('Two');
      vi.advanceTimersByTime(2000);
      const box = document.querySelector('.iina-downloads-notice');
      expect(box.classList.contains('hide')).toBe(false);
      vi.advanceTimersByTime(3000);
      expect(box.classList.contains('hide')).toBe(true);
    });

    it('keeps the latest armed removal when an older one expires', async () => {
      vi.useFakeTimers();
      const { shell, emit } = await loadShell();
      shell.downloadsPanel.open();
      const entries = [
        { itemId: 'a', status: 'completed', createdAt: 2 },
        { itemId: 'b', status: 'completed', createdAt: 1 },
      ];
      emit('offline-downloads', { downloads: entries });
      const element = document.querySelector('.iina-downloads');
      element.querySelector('[data-action="remove"][data-item-id="a"]').click();
      vi.advanceTimersByTime(2000);
      element.querySelector('[data-action="remove"][data-item-id="b"]').click();
      vi.advanceTimersByTime(2000);
      expect(shell.downloadsPanel.pendingRemoveId).toBe('b');
      vi.advanceTimersByTime(2000);
      expect(shell.downloadsPanel.pendingRemoveId).toBeNull();
      // Rendering sorts a copy, the state keeps its order
      expect(shell.state.downloads.map((entry) => entry.itemId)).toEqual(['a', 'b']);
    });

    it('renders exactly one node per entry and per action', async () => {
      const { shell, emit } = await loadShell();
      shell.downloadsPanel.open();
      emit('offline-downloads', {
        downloads: [
          { itemId: 'a', status: 'completed', createdAt: 2 },
          { itemId: 'd', status: 'downloading', progress: 10, createdAt: 1 },
          { itemId: 'f', status: 'failed' },
        ],
      });
      const element = document.querySelector('.iina-downloads');
      const list = element.querySelector('.iina-downloads-list');
      expect(list.childNodes).toHaveLength(3);
      const labels = (id) =>
        Array.from(element.querySelectorAll(`[data-download-id="${id}"] [data-action]`)).map(
          (button) => button.textContent
        );
      expect(labels('a')).toEqual(['Play', 'Reveal', 'Remove']);
      expect(labels('d')).toEqual(['Cancel']);
      expect(labels('f')).toEqual(['Retry', 'Remove']);
      expect(
        element.querySelector('[data-download-id="a"] .iina-downloads-actions').childNodes
      ).toHaveLength(3);
      expect(element.querySelector('[data-download-id="a"] .iina-downloads-progress')).toBeNull();
      expect(
        element.querySelector('[data-download-id="d"] .iina-downloads-progress')
      ).not.toBeNull();
    });
  });
});
