import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createAutoplayManager } from '../../src/lib/autoplay-manager.js';

const SERVER = 'http://jf.local:8096';
const KEY = 'key-1';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function flush(times = 10) {
  for (let i = 0; i < times; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

const EPISODE_META = {
  Type: 'Episode',
  Name: 'Ep 1',
  SeriesId: 'series',
  SeasonId: 'season-1',
  SeriesName: 'Show',
  ParentIndexNumber: 1,
  IndexNumber: 1,
};

function episodes(...items) {
  return { Items: items.map((item) => ({ MediaSources: [{}], ...item })) };
}

function createEnv(overrides = {}) {
  const routes = [];
  const http = {
    get: vi.fn(async (url) => {
      const match = routes.find(([fragment]) => url.includes(fragment));
      if (!match) return { data: null };
      const value = typeof match[1] === 'function' ? match[1](url) : match[1];
      if (value instanceof Error) throw value;
      return { data: value };
    }),
  };
  const mpv = {
    getNumber: vi.fn(() => 0),
    command: vi.fn(),
  };
  const core = { osd: vi.fn() };
  const prefs = new Map();
  const deps = {
    http,
    mpv,
    core,
    preferences: { get: vi.fn((key) => prefs.get(key)) },
    buildJellyfinHeaders: vi.fn((apiKey, extra) => ({ 'X-Key': apiKey, ...extra })),
    fetchItemMetadata: vi.fn(async () => EPISODE_META),
    log: vi.fn(),
    ...overrides,
  };
  const manager = createAutoplayManager(deps);
  return {
    ...deps,
    prefs,
    manager,
    route(fragment, value) {
      routes.push([fragment, value]);
    },
    urls: () => http.get.mock.calls.map((call) => call[0]),
    loadfile: () => mpv.command.mock.calls.filter((call) => call[0] === 'loadfile'),
  };
}

describe('createAutoplayManager', () => {
  let env;

  beforeEach(() => {
    env = createEnv();
  });

  describe('queueing the next episode of the season', () => {
    it('queues the following episode with a title and the stream url', async () => {
      env.route(
        '/Shows/series/Episodes',
        episodes(
          { Id: 'ep-3', Name: 'Three', IndexNumber: 3 },
          { Id: 'ep-1', Name: 'One', IndexNumber: 1 },
          { Id: 'ep-2', Name: 'Two', IndexNumber: 2 }
        )
      );

      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();

      expect(env.urls()[0]).toBe(
        `${SERVER}/Shows/series/Episodes?seasonId=season-1&fields=MediaSources%2CPath%2CLocationType%2CIsFolder&ApiKey=${KEY}`
      );
      expect(env.http.get.mock.calls[0][1]).toEqual({
        headers: { 'X-Key': KEY, Accept: 'application/json' },
      });
      expect(env.loadfile()).toEqual([
        [
          'loadfile',
          [
            `${SERVER}/Videos/ep-2/stream?static=true&ApiKey=${KEY}`,
            'insert-next',
            '-1',
            'force-media-title=Show S01E02 - Two',
          ],
        ],
      ]);
      expect(env.manager.isQueued()).toBe(true);
      expect(env.core.osd).not.toHaveBeenCalled();
      expect(env.log).toHaveBeenCalledWith('Fetched 3 episodes from series: E1, E2, E3');
      expect(env.log).toHaveBeenCalledWith('Series changed from null to series');
      expect(env.log).toHaveBeenCalledWith('Autoplay setup complete — queued next episode: Two');
    });

    it('lets the caller swap the stream url for a local copy', async () => {
      env = createEnv({
        resolvePlayUrl: vi.fn((episodeId, url) => (episodeId === 'ep-2' ? '/local/two.mkv' : url)),
      });
      env.route(
        '/Shows/series/Episodes',
        episodes({ Id: 'ep-1', IndexNumber: 1 }, { Id: 'ep-2', Name: 'Two', IndexNumber: 2 })
      );

      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();

      expect(env.resolvePlayUrl).toHaveBeenCalledWith(
        'ep-2',
        `${SERVER}/Videos/ep-2/stream?static=true&ApiKey=${KEY}`
      );
      expect(env.loadfile()[0][1][0]).toBe('/local/two.mkv');
    });

    it('skips a gap and episodes without media, and parses string responses', async () => {
      env.route(
        '/Shows/series/Episodes',
        JSON.stringify({
          Items: [
            { Id: 'ep-1', IndexNumber: 1, MediaSources: [{}] },
            { Id: 'ep-2', IndexNumber: 2, MediaSources: [] },
            { Id: 'ep-x', IndexNumber: 'x' },
            { Id: 'ep-none', MediaSources: [{}] },
            { Id: 'ep-4', Name: 'Four', IndexNumber: 4, MediaSources: [{}] },
          ],
        })
      );

      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();

      expect(env.loadfile()[0][1][0]).toContain('/Videos/ep-4/');
      expect(env.loadfile()[0][1][3]).toBe('force-media-title=Show S01E04 - Four');
    });

    it('shows an OSD when notifications are on and titles episodes without a series name', async () => {
      env.prefs.set('show_notifications', true);
      env.fetchItemMetadata.mockResolvedValue({ ...EPISODE_META, SeriesName: undefined });
      env.route(
        '/Shows/series/Episodes',
        episodes({ Id: 'ep-1', IndexNumber: 1 }, { Id: 'ep-2', Name: 'Two', IndexNumber: 2 })
      );

      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();

      expect(env.core.osd).toHaveBeenCalledWith('Up next: S01E02 - Two');
    });
  });

  describe('moving on to the next season', () => {
    it('queues the first episode of the next season', async () => {
      env.route(
        '/Shows/series/Episodes?seasonId=season-1',
        episodes({ Id: 'ep-1', IndexNumber: 1 })
      );
      env.route('/Shows/series/Seasons', {
        Items: [
          { Id: 'season-2', Name: 'Season 2', IndexNumber: 2 },
          { Id: 'season-x', Name: 'Extras' },
          { Id: 'season-1', Name: 'Season 1', IndexNumber: 1 },
          { Id: 'season-0', Name: 'Specials', IndexNumber: 0 },
        ],
      });
      env.route(
        '/Shows/series/Episodes?seasonId=season-2',
        episodes({ Id: 's2e1', Name: 'Opener', IndexNumber: 1 })
      );

      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();

      expect(env.urls()[1]).toBe(`${SERVER}/Shows/series/Seasons?ApiKey=${KEY}`);
      expect(env.http.get.mock.calls[1][1]).toEqual({
        headers: { 'X-Key': KEY, Accept: 'application/json' },
      });
      expect(env.loadfile()[0][1]).toEqual([
        `${SERVER}/Videos/s2e1/stream?static=true&ApiKey=${KEY}`,
        'insert-next',
        '-1',
        'force-media-title=Show S02E01 - Opener',
      ]);
      expect(env.log).toHaveBeenCalledWith('Found next season: Season 2 (S2)');
    });

    it('sorts seasons without an index number as zero', async () => {
      env.route(
        '/Shows/series/Episodes?seasonId=season-1',
        episodes({ Id: 'ep-1', IndexNumber: 1 })
      );
      env.route(
        '/Shows/series/Seasons',
        JSON.stringify({
          Items: [
            { Id: 'specials-a', IndexNumber: 0 },
            { Id: 'season-2', Name: 'Two', IndexNumber: 2 },
            { Id: 'specials-b', IndexNumber: 0 },
            { Id: 'season-1', IndexNumber: 1 },
            { Id: 'specials-c', IndexNumber: 0 },
          ],
        })
      );
      env.route(
        '/Shows/series/Episodes?seasonId=season-2',
        episodes({ Id: 's2e1', Name: 'A', IndexNumber: 1 })
      );

      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();

      expect(env.loadfile()).toHaveLength(1);
    });

    it('ends at the last season, unknown seasons and empty season lists', async () => {
      env.route(
        '/Shows/series/Episodes?seasonId=season-1',
        episodes({ Id: 'ep-1', IndexNumber: 1 })
      );
      env.route('/Shows/series/Seasons', {
        Items: [{ Id: 'season-1', IndexNumber: 1 }],
      });
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith('No next season available — end of series');
      expect(env.log).toHaveBeenCalledWith('No next episode found — end of series');

      env = createEnv();
      env.route(
        '/Shows/series/Episodes?seasonId=season-1',
        episodes({ Id: 'ep-1', IndexNumber: 1 })
      );
      env.route('/Shows/series/Seasons', { Items: [{ Id: 'elsewhere', IndexNumber: 3 }] });
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith('No next season available — end of series');

      env = createEnv();
      env.route(
        '/Shows/series/Episodes?seasonId=season-1',
        episodes({ Id: 'ep-1', IndexNumber: 1 })
      );
      env.route('/Shows/series/Seasons', { Items: [] });
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.loadfile()).toHaveLength(0);

      env = createEnv();
      env.route(
        '/Shows/series/Episodes?seasonId=season-1',
        episodes({ Id: 'ep-1', IndexNumber: 1 })
      );
      env.route('/Shows/series/Seasons', {});
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.loadfile()).toHaveLength(0);

      env = createEnv();
      env.route(
        '/Shows/series/Episodes?seasonId=season-1',
        episodes({ Id: 'ep-1', IndexNumber: 1 })
      );
      // No route for seasons: data is null
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.loadfile()).toHaveLength(0);
      expect(env.manager.isQueued()).toBe(false);
    });

    it('stops when the next season has no episodes or the lookup fails', async () => {
      env.route(
        '/Shows/series/Episodes?seasonId=season-1',
        episodes({ Id: 'ep-1', IndexNumber: 1 })
      );
      env.route('/Shows/series/Seasons', {
        Items: [
          { Id: 'season-1', IndexNumber: 1 },
          { Id: 'season-2', IndexNumber: 2 },
        ],
      });
      env.route('/Shows/series/Episodes?seasonId=season-2', { Items: [] });
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith('Next season has no episodes');

      env = createEnv();
      env.route(
        '/Shows/series/Episodes?seasonId=season-1',
        episodes({ Id: 'ep-1', IndexNumber: 1 })
      );
      env.route('/Shows/series/Seasons', new Error('offline'));
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith('Error resolving next episode: offline');
      expect(env.loadfile()).toHaveLength(0);
    });
  });

  describe('episode lists that cannot be used', () => {
    it('handles missing data, missing items and request errors', async () => {
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith(
        'Error fetching series episodes: No data received from Jellyfin API'
      );

      env = createEnv();
      env.route('/Shows/series/Episodes', {});
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith('No episodes found in response');

      env = createEnv();
      env.route('/Shows/series/Episodes', new Error('boom'));
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith('Error fetching series episodes: boom');
      expect(env.loadfile()).toHaveLength(0);
    });
  });

  describe('series information', () => {
    it('ignores items that are not episodes or lack series data', async () => {
      env.fetchItemMetadata.mockResolvedValue({ Type: 'Movie' });
      env.manager.setupAutoplayForEpisode(SERVER, 'm-1', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith("Item m-1 is not an episode, it's a Movie");
      expect(env.log).toHaveBeenCalledWith('Could not get series info, autoplay not available');

      env.fetchItemMetadata.mockResolvedValue({ Type: 'Episode', SeriesId: 'series' });
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-9', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith(
        'Missing series info - SeriesId: series, SeasonId: undefined'
      );

      env.fetchItemMetadata.mockRejectedValue(new Error('404'));
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-10', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith('Error getting series info from episode: 404');
      expect(env.http.get).not.toHaveBeenCalled();
    });

    it('defaults season and episode numbers sensibly', async () => {
      env.fetchItemMetadata.mockResolvedValue({
        Type: 'Episode',
        SeriesId: 'series',
        SeasonId: 'season-1',
      });
      env.route('/Shows/series/Episodes', episodes({ Id: 'n', Name: 'Next', IndexNumber: 1 }));
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith(
        'Series info: SeriesName=, SeriesId=series, SeasonId=season-1, SeasonNumber=1, EpisodeNumber=0'
      );
      expect(env.loadfile()[0][1][3]).toBe('force-media-title=S01E01 - Next');

      // Specials are season 0 and stay season 0; a garbage number becomes 1
      env = createEnv();
      env.fetchItemMetadata.mockResolvedValue({ ...EPISODE_META, ParentIndexNumber: 0 });
      env.route('/Shows/series/Episodes', episodes({ Id: 'n', Name: 'Next', IndexNumber: 2 }));
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.loadfile()[0][1][3]).toBe('force-media-title=Show S00E02 - Next');

      env = createEnv();
      env.fetchItemMetadata.mockResolvedValue({ ...EPISODE_META, ParentIndexNumber: 'abc' });
      env.route('/Shows/series/Episodes', episodes({ Id: 'n', Name: 'Next', IndexNumber: 2 }));
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.loadfile()[0][1][3]).toBe('force-media-title=Show S01E02 - Next');
    });
  });

  describe('playlist cleanup before queueing', () => {
    beforeEach(() => {
      env.route(
        '/Shows/series/Episodes',
        episodes({ Id: 'ep-1', IndexNumber: 1 }, { Id: 'ep-2', Name: 'Two', IndexNumber: 2 })
      );
    });

    it('removes the entries behind the current one, ignoring removal errors', async () => {
      env.mpv.getNumber.mockImplementation((name) => (name === 'playlist-count' ? 4 : 1));
      env.mpv.command.mockImplementation((name, args) => {
        if (name === 'playlist-remove' && args[0] === '2') throw new Error('gone');
      });

      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();

      expect(env.mpv.command.mock.calls.filter((call) => call[0] === 'playlist-remove')).toEqual([
        ['playlist-remove', ['3']],
        ['playlist-remove', ['2']],
      ]);
      expect(env.log).toHaveBeenCalledWith('Cleaned 2 stale playlist entries');
      expect(env.loadfile()).toHaveLength(1);
    });

    it('leaves a playlist that has nothing behind the current entry', async () => {
      env.mpv.getNumber.mockImplementation((name) => (name === 'playlist-count' ? null : 0));
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.mpv.command).not.toHaveBeenCalledWith('playlist-remove', expect.anything());
      expect(env.log).not.toHaveBeenCalledWith(expect.stringContaining('stale playlist'));
    });

    it('skips the cleanup for an invalid position', async () => {
      env.mpv.getNumber.mockImplementation((name) => (name === 'playlist-count' ? 3 : -1));
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith(
        'Skipping playlist cleanup due to invalid playlist-pos=-1, playlist-count=3'
      );

      env = createEnv();
      env.route(
        '/Shows/series/Episodes',
        episodes({ Id: 'ep-1', IndexNumber: 1 }, { Id: 'ep-2', IndexNumber: 2 })
      );
      env.mpv.getNumber.mockImplementation((name) => (name === 'playlist-count' ? 3 : NaN));
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith(
        'Skipping playlist cleanup due to invalid playlist-pos=NaN, playlist-count=3'
      );
      expect(env.loadfile()).toHaveLength(1);
    });

    it('tolerates mpv refusing to report the playlist and failing to queue', async () => {
      env.mpv.getNumber.mockImplementation(() => {
        throw new Error('no mpv');
      });
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith('Could not clean playlist (non-critical)');
      expect(env.loadfile()).toHaveLength(1);

      env = createEnv();
      env.route(
        '/Shows/series/Episodes',
        episodes({ Id: 'ep-1', IndexNumber: 1 }, { Id: 'ep-2', IndexNumber: 2 })
      );
      env.mpv.command.mockImplementation(() => {
        throw new Error('loadfile failed');
      });
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith('Error queuing next episode: loadfile failed');
      expect(env.manager.isQueued()).toBe(false);
    });
  });

  describe('request bookkeeping', () => {
    it('ignores a duplicate setup for the same episode', async () => {
      env.route(
        '/Shows/series/Episodes',
        episodes({ Id: 'ep-1', IndexNumber: 1 }, { Id: 'ep-2', IndexNumber: 2 })
      );
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith(
        'Episode ep-1 already being processed, skipping duplicate setup'
      );
      expect(env.fetchItemMetadata).toHaveBeenCalledTimes(1);
    });

    it('aborts a request superseded while the metadata was loading', async () => {
      const first = deferred();
      env.fetchItemMetadata.mockReturnValueOnce(first.promise);
      env.route(
        '/Shows/series/Episodes',
        episodes({ Id: 'ep-1', IndexNumber: 1 }, { Id: 'ep-2', IndexNumber: 2 })
      );

      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-5', KEY);
      await flush();
      first.resolve(EPISODE_META);
      await flush();

      expect(env.log).toHaveBeenCalledWith('Autoplay request #1 is stale (current: #2), aborting');
      expect(env.loadfile()).toHaveLength(1);
    });

    it('aborts a request superseded while the episodes were loading', async () => {
      const gate = deferred();
      let calls = 0;
      // The first episode request hangs until the gate opens
      env.http.get.mockImplementation(async () => {
        calls++;
        if (calls === 1) {
          await gate.promise;
        }
        return { data: episodes({ Id: 'ep-1', IndexNumber: 1 }, { Id: 'ep-2', IndexNumber: 2 }) };
      });

      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-7', KEY);
      await flush();
      gate.resolve();
      await flush();

      expect(env.log).toHaveBeenCalledWith('Autoplay request #1 is stale after resolve, aborting');
      expect(env.loadfile()).toHaveLength(1);
    });

    it('reports unexpected failures', async () => {
      env.log.mockImplementation((message) => {
        if (String(message).startsWith('Got series info')) throw new Error('logger broke');
      });
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.log).toHaveBeenCalledWith('Error setting up autoplay: logger broke');
    });

    it('resets its state for a new file', async () => {
      env.route(
        '/Shows/series/Episodes',
        episodes({ Id: 'ep-1', IndexNumber: 1 }, { Id: 'ep-2', IndexNumber: 2 })
      );
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.manager.isQueued()).toBe(true);

      // Same episode: the queued flag clears but the episode stays remembered
      env.manager.resetForNewFile('ep-1');
      expect(env.manager.isQueued()).toBe(false);
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      expect(env.fetchItemMetadata).toHaveBeenCalledTimes(1);

      // Another file forgets the episode, and so does a missing id
      env.manager.resetForNewFile('other');
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.fetchItemMetadata).toHaveBeenCalledTimes(2);
      env.manager.resetForNewFile(undefined);
      env.manager.setupAutoplayForEpisode(SERVER, 'ep-1', KEY);
      await flush();
      expect(env.fetchItemMetadata).toHaveBeenCalledTimes(3);

      env.manager.clearQueuedFlag();
      expect(env.manager.isQueued()).toBe(false);
    });
  });
});
