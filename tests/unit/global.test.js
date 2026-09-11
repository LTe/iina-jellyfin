import { afterEach, describe, expect, it, vi } from 'vitest';
import { createFakeIina, flushPromises } from './helpers/fake-iina.js';

const SERVER = 'http://jf.local:8096';
const ITEM = 'item-1';

/**
 * The controller side of IINA's global messaging: handlers are stored per
 * name and called with (data, playerLabel); postMessage(target, name, data)
 * is a spy.
 */
function createGlobalController() {
  const handlers = {};
  return {
    handlers,
    onMessage: vi.fn((name, callback) => {
      handlers[name] = callback;
    }),
    postMessage: vi.fn(),
    createPlayerInstance: vi.fn(() => 42),
    receive(name, data, player) {
      return handlers[name](data, player);
    },
  };
}

async function loadGlobal(options = {}) {
  const fake = createFakeIina({
    preferences: { debug_logging: true, ...(options.preferences || {}) },
    files: options.files || {},
  });
  fake.iina.global = createGlobalController();
  delete fake.iina.core;
  delete fake.iina.mpv;
  delete fake.iina.event;
  delete fake.iina.sidebar;
  globalThis.iina = fake.iina;
  vi.resetModules();
  await import('../../src/global.js');
  return fake;
}

const posts = (fake, name) =>
  fake.iina.global.postMessage.mock.calls.filter((call) => call[1] === name);

describe('plugin global entry', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    delete globalThis.iina;
  });

  describe('creating player windows', () => {
    it('creates a player and confirms to the requesting window', async () => {
      const fake = await loadGlobal();
      vi.spyOn(Date, 'now').mockReturnValue(1234);

      fake.iina.global.receive('create-player', { url: 'http://x/v', title: 'Film' }, 'p1');

      expect(fake.iina.global.createPlayerInstance).toHaveBeenCalledWith({
        url: 'http://x/v',
        label: 'jellyfin-1234',
        enablePlugins: false,
        disableWindowAnimation: false,
      });
      expect(fake.iina.global.postMessage).toHaveBeenCalledWith('p1', 'player-created', {
        playerId: 42,
        title: 'Film',
        url: 'http://x/v',
      });

      fake.iina.global.postMessage.mockClear();
      fake.iina.global.receive('create-player', { url: 'http://x/v', title: 'Film' }, undefined);
      expect(fake.iina.global.postMessage).not.toHaveBeenCalled();
    });

    it('reports failures to the requesting window', async () => {
      const fake = await loadGlobal();
      fake.iina.global.createPlayerInstance.mockImplementation(() => {
        throw new Error('no window');
      });

      fake.iina.global.receive('create-player', { url: 'http://x/v', title: 'Film' }, 'p1');
      expect(fake.iina.global.postMessage).toHaveBeenCalledWith('p1', 'player-creation-failed', {
        error: 'Error: no window',
        url: 'http://x/v',
      });

      fake.iina.global.postMessage.mockClear();
      fake.iina.global.receive('create-player', undefined, 'p1');
      expect(fake.iina.global.postMessage).toHaveBeenCalledWith('p1', 'player-creation-failed', {
        error: expect.stringContaining('TypeError'),
        url: undefined,
      });

      fake.iina.global.postMessage.mockClear();
      fake.iina.global.receive('create-player', undefined, null);
      expect(fake.iina.global.postMessage).not.toHaveBeenCalled();
    });
  });

  describe('offline downloads', () => {
    const download = {
      item: { Id: ITEM, Type: 'Movie', Name: 'Film' },
      serverUrl: SERVER,
      accessToken: 'tok',
    };

    function routePlaybackInfo(fake) {
      fake.iina.http.get.mockImplementation(async (url) =>
        url.includes('/PlaybackInfo')
          ? {
              statusCode: 200,
              data: { MediaSources: [{ Id: 'src', Container: 'mkv', Size: 10, MediaStreams: [] }] },
            }
          : { data: null }
      );
      fake.iina.http.download.mockImplementation(async (url, destination) => {
        fake.files.set(destination, 'media');
      });
    }

    it('answers state requests to the asking window and remembers it', async () => {
      const fake = await loadGlobal();
      fake.iina.global.receive('get-offline-downloads', undefined, 'p1');
      expect(fake.iina.global.postMessage).toHaveBeenCalledWith('p1', 'offline-downloads', {
        downloads: [],
        directory: '/abs/data/offline',
        quality: 'original',
        qualityPresets: expect.any(Array),
        error: null,
      });

      // A quality change is broadcast to every window seen so far
      fake.iina.global.receive('get-offline-downloads', undefined, 'p2');
      fake.iina.global.postMessage.mockClear();
      fake.iina.global.receive('offline-set-quality', { quality: '2000' }, 'p2');
      expect(
        posts(fake, 'offline-downloads')
          .map((call) => call[0])
          .sort()
      ).toEqual(['p1', 'p2']);
      expect(fake.prefs.get('offline_download_quality')).toBe('2000');
    });

    it('runs downloads for every window and reports OSD text to them', async () => {
      const fake = await loadGlobal();
      routePlaybackInfo(fake);
      fake.iina.global.receive('get-offline-downloads', undefined, 'p1');
      fake.iina.global.postMessage.mockClear();

      fake.iina.global.receive('offline-download', download, 'p2');
      await flushPromises(20);

      const updates = posts(fake, 'offline-downloads');
      expect(updates.map((call) => call[0])).toEqual(expect.arrayContaining(['p1', 'p2']));
      expect(updates[updates.length - 1][2].downloads[0]).toMatchObject({
        itemId: ITEM,
        status: 'completed',
        mediaPath: '@data/offline/Film.mkv',
      });
      expect(fake.iina.global.postMessage).toHaveBeenCalledWith('p1', 'offline-osd', {
        message: 'Downloaded for offline: Film',
      });
      expect(fake.iina.global.postMessage).toHaveBeenCalledWith('p2', 'offline-osd', {
        message: 'Downloaded for offline: Film',
      });
      expect(fake.iina.global.postMessage).not.toHaveBeenCalledWith(
        null,
        expect.anything(),
        expect.anything()
      );

      // Playing goes to the window that asked
      fake.iina.global.postMessage.mockClear();
      fake.iina.global.receive('play-offline', { itemId: ITEM }, 'p1');
      expect(fake.iina.global.postMessage).toHaveBeenCalledWith('p1', 'offline-play', {
        streamUrl: '/abs/data/offline/Film.mkv',
        title: 'Film',
      });
      expect(fake.iina.global.postMessage).not.toHaveBeenCalledWith(
        'p2',
        'offline-play',
        expect.anything()
      );
    });

    it('falls back to every known window when the sender is unknown', async () => {
      const fake = await loadGlobal();
      // Nobody known yet: nothing to answer to
      fake.iina.global.receive('get-offline-downloads', undefined, null);
      expect(fake.iina.global.postMessage).not.toHaveBeenCalled();

      fake.iina.global.receive('get-offline-downloads', undefined, 'p1');
      fake.iina.global.postMessage.mockClear();
      fake.iina.global.receive('get-offline-downloads', undefined, undefined);
      expect(fake.iina.global.postMessage).toHaveBeenCalledTimes(1);
      expect(fake.iina.global.postMessage).toHaveBeenCalledWith(
        'p1',
        'offline-downloads',
        expect.anything()
      );
    });

    it('keeps going when a window cannot be reached', async () => {
      const fake = await loadGlobal();
      fake.iina.global.receive('get-offline-downloads', undefined, 'gone');
      fake.iina.global.receive('get-offline-downloads', undefined, 'p1');
      fake.iina.global.postMessage.mockImplementation((target) => {
        if (target === 'gone') throw new Error('closed');
      });

      fake.iina.global.receive('offline-set-quality', { quality: '500' }, undefined);

      expect(fake.iina.global.postMessage).toHaveBeenCalledWith(
        'p1',
        'offline-downloads',
        expect.objectContaining({ quality: '500' })
      );
      expect(fake.iina.console.log).toHaveBeenCalledWith(
        'DEBUG: Could not post offline-downloads to player gone: closed'
      );
    });

    it('handles the folder actions', async () => {
      const fake = await loadGlobal();
      fake.iina.global.receive('offline-open-folder', undefined, 'p1');
      await flushPromises();
      expect(fake.iina.utils.exec).toHaveBeenCalledWith('/bin/mkdir', ['-p', '/abs/data/offline']);
      expect(fake.iina.file.showInFinder).toHaveBeenCalledWith('@data/offline');

      fake.iina.utils.chooseFile.mockResolvedValue('/Volumes/Media/Offline');
      fake.iina.global.receive('offline-choose-folder', undefined, 'p1');
      await flushPromises();
      expect(fake.prefs.get('offline_download_dir')).toBe('/Volumes/Media/Offline');
      expect(fake.iina.global.postMessage).toHaveBeenCalledWith('p1', 'offline-osd', {
        message: 'Offline downloads folder: /Volumes/Media/Offline',
      });
    });
  });
});
