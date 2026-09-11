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

  describe('playback sync', () => {
    const report = {
      kind: 'stopped',
      serverUrl: SERVER,
      itemId: ITEM,
      positionTicks: 500,
      mediaSourceId: null,
      delivered: false,
    };

    it('keeps undelivered reports and syncs them when a window shows activity', async () => {
      vi.useFakeTimers();
      const fake = await loadGlobal({
        preferences: {
          sync_playback_progress: true,
          jellyfin_servers: JSON.stringify([{ id: 's', serverUrl: SERVER, accessToken: 'tok' }]),
        },
      });
      fake.iina.http.post.mockRejectedValue(new Error('offline'));

      fake.iina.global.receive('playback-report', report, 'p1');
      await vi.advanceTimersByTimeAsync(0);
      expect(JSON.parse(fake.files.get('@data/offline-playback-queue.json'))).toEqual([
        expect.objectContaining({ itemId: ITEM, positionTicks: 500 }),
      ]);
      expect(fake.iina.http.post).toHaveBeenCalledTimes(1);

      // Back online: the next window message triggers another attempt
      fake.iina.http.post.mockResolvedValue({ statusCode: 204 });
      await vi.advanceTimersByTimeAsync(6000);
      fake.iina.global.receive('get-offline-downloads', undefined, 'p1');
      await vi.advanceTimersByTimeAsync(0);
      expect(fake.iina.http.post).toHaveBeenCalledWith(
        `${SERVER}/Sessions/Playing/Stopped?ApiKey=tok`,
        expect.objectContaining({ data: expect.objectContaining({ ItemId: ITEM }) })
      );
      expect(JSON.parse(fake.files.get('@data/offline-playback-queue.json'))).toEqual([]);
      vi.useRealTimers();
    });

    it('sends leftovers from the previous run shortly after starting', async () => {
      vi.useFakeTimers();
      const fake = await loadGlobal({
        files: {
          '@data/offline-playback-queue.json': JSON.stringify([
            { serverUrl: SERVER, itemId: ITEM, positionTicks: 9, watched: true },
          ]),
        },
        preferences: {
          sync_playback_progress: true,
          jellyfin_servers: JSON.stringify([{ id: 's', serverUrl: SERVER, accessToken: 'tok' }]),
        },
      });
      await vi.advanceTimersByTimeAsync(3000);
      expect(fake.iina.http.post.mock.calls.map((call) => call[0])).toEqual([
        `${SERVER}/Sessions/Playing/Stopped?ApiKey=tok`,
        `${SERVER}/UserPlayedItems/${ITEM}?ApiKey=tok`,
      ]);
      vi.useRealTimers();
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

    it('answers state requests to the asking window only', async () => {
      const fake = await loadGlobal();
      fake.iina.global.receive('get-offline-downloads', undefined, 'p1');
      expect(fake.iina.global.postMessage).toHaveBeenCalledWith('p1', 'offline-downloads', {
        downloads: [],
        directory: '/abs/data/offline',
        quality: 'original',
        qualityPresets: expect.any(Array),
        error: null,
        notice: null,
      });

      // Nothing is ever sent to a window that is not asking right now:
      // IINA crashes when a message reaches a window that is still loading
      fake.iina.global.postMessage.mockClear();
      fake.iina.global.receive('offline-set-quality', { quality: '2000' }, 'p2');
      expect(fake.prefs.get('offline_download_quality')).toBe('2000');
      expect(fake.iina.global.postMessage).not.toHaveBeenCalled();

      fake.iina.global.receive('get-offline-downloads', undefined, null);
      fake.iina.global.receive('get-offline-downloads', undefined, undefined);
      expect(fake.iina.global.postMessage).not.toHaveBeenCalled();
      expect(fake.iina.console.log).toHaveBeenCalledWith(
        'DEBUG: No requesting window for offline-downloads, dropping it'
      );
    });

    it('runs downloads and hands OSD text to the polling windows once', async () => {
      const fake = await loadGlobal();
      routePlaybackInfo(fake);

      fake.iina.global.receive('offline-download', download, 'p2');
      await flushPromises(20);
      // No pushes while the download ran
      expect(fake.iina.global.postMessage).not.toHaveBeenCalled();

      fake.iina.global.receive('get-offline-downloads', undefined, 'p1');
      const [target, name, state] = fake.iina.global.postMessage.mock.calls[0];
      expect([target, name]).toEqual(['p1', 'offline-downloads']);
      expect(state.downloads[0]).toMatchObject({
        itemId: ITEM,
        status: 'completed',
        mediaPath: '@data/offline/Film.mkv',
      });
      expect(state.notice).toEqual({ id: 2, message: 'Downloaded for offline: Film' });

      // Playing goes to the window that asked
      fake.iina.global.postMessage.mockClear();
      fake.iina.global.receive('play-offline', { itemId: ITEM }, 'p1');
      expect(fake.iina.global.postMessage).toHaveBeenCalledWith('p1', 'offline-play', {
        streamUrl: '/abs/data/offline/Film.mkv',
        title: 'Film',
      });
      expect(fake.iina.global.postMessage).toHaveBeenCalledTimes(1);

      // A play request without a known sender is dropped rather than broadcast
      fake.iina.global.postMessage.mockClear();
      fake.iina.global.receive('play-offline', { itemId: ITEM }, null);
      expect(fake.iina.global.postMessage).not.toHaveBeenCalled();
      expect(fake.iina.console.log).toHaveBeenCalledWith(
        'DEBUG: No requesting window for offline-play, dropping it'
      );
    });

    it('forgets the requester after a handler that throws', async () => {
      const fake = await loadGlobal();
      fake.iina.global.postMessage.mockImplementationOnce(() => {
        throw new Error('window gone');
      });
      fake.iina.global.receive('get-offline-downloads', undefined, 'p1');
      expect(fake.iina.console.log).toHaveBeenCalledWith(
        'DEBUG: Message get-offline-downloads failed: window gone'
      );
      // The failed reply did not leave p1 as the target of later messages
      fake.iina.global.postMessage.mockClear();
      fake.iina.global.receive('play-offline', { itemId: 'nothing' }, undefined);
      expect(fake.iina.global.postMessage).not.toHaveBeenCalled();
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

      fake.iina.global.receive('get-offline-downloads', undefined, 'p1');
      expect(fake.iina.global.postMessage).toHaveBeenLastCalledWith(
        'p1',
        'offline-downloads',
        expect.objectContaining({
          directory: '/Volumes/Media/Offline',
          notice: { id: 1, message: 'Offline downloads folder: /Volumes/Media/Offline' },
        })
      );
    });
  });
});
