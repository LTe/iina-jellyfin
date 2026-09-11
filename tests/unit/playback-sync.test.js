import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  QUEUE_FILE,
  classifyPlaybackReport,
  createPlaybackReportRecorder,
  createPlaybackSyncQueue,
} from '../../src/lib/playback-sync.js';

const SERVER = 'http://jf.local:8096';

describe('classifyPlaybackReport', () => {
  it('recognises progress, stop and watched reports', () => {
    expect(
      classifyPlaybackReport(`${SERVER}/Sessions/Playing/Progress?ApiKey=k`, {
        ItemId: 'a',
        PositionTicks: 500,
        MediaSourceId: 'src',
      })
    ).toEqual({
      kind: 'progress',
      serverUrl: SERVER,
      itemId: 'a',
      positionTicks: 500,
      mediaSourceId: 'src',
    });
    expect(
      classifyPlaybackReport(`${SERVER}/Sessions/Playing/Stopped`, {
        ItemId: 'a',
        PositionTicks: '7',
      })
    ).toEqual({
      kind: 'stopped',
      serverUrl: SERVER,
      itemId: 'a',
      positionTicks: 7,
      mediaSourceId: null,
    });
    expect(classifyPlaybackReport(`${SERVER}/UserPlayedItems/a?ApiKey=k`)).toEqual({
      kind: 'watched',
      serverUrl: SERVER,
      itemId: 'a',
      watched: true,
    });
    expect(
      classifyPlaybackReport(`https://JF.example/sessions/playing/stopped`, { ItemId: 'b' })
    ).toEqual({
      kind: 'stopped',
      serverUrl: 'https://JF.example',
      itemId: 'b',
      positionTicks: 0,
      mediaSourceId: null,
    });
  });

  it('ignores everything else', () => {
    expect(
      classifyPlaybackReport(`${SERVER}/Sessions/Playing?ApiKey=k`, { ItemId: 'a' })
    ).toBeNull();
    expect(classifyPlaybackReport(`${SERVER}/Sessions/Playing/Stopped`, {})).toBeNull();
    expect(classifyPlaybackReport(`${SERVER}/Sessions/Playing/Stopped`, undefined)).toBeNull();
    expect(classifyPlaybackReport(`${SERVER}/Items/a/PlaybackInfo`, { ItemId: 'a' })).toBeNull();
    expect(classifyPlaybackReport(`${SERVER}/UserPlayedItems/`, {})).toBeNull();
    expect(classifyPlaybackReport('/Sessions/Playing/Stopped', { ItemId: 'a' })).toBeNull();
    expect(classifyPlaybackReport(undefined)).toBeNull();
  });
});

describe('createPlaybackReportRecorder', () => {
  function setup(post) {
    const http = { get: vi.fn(), post: vi.fn(post), download: vi.fn() };
    const onOutcome = vi.fn();
    const log = vi.fn();
    const wrapped = createPlaybackReportRecorder({ http, onOutcome, log });
    return { http, onOutcome, log, wrapped };
  }

  it('passes requests through and reports delivered playback reports', async () => {
    const { http, onOutcome, wrapped } = setup(async () => ({ statusCode: 204 }));
    expect(wrapped.get).toBe(http.get);
    expect(wrapped.download).toBe(http.download);

    const options = { data: { ItemId: 'a', PositionTicks: 10 } };
    await expect(wrapped.post(`${SERVER}/Sessions/Playing/Stopped`, options)).resolves.toEqual({
      statusCode: 204,
    });
    expect(http.post).toHaveBeenCalledWith(`${SERVER}/Sessions/Playing/Stopped`, options);
    expect(onOutcome).toHaveBeenCalledWith({
      kind: 'stopped',
      serverUrl: SERVER,
      itemId: 'a',
      positionTicks: 10,
      mediaSourceId: null,
      delivered: true,
    });

    onOutcome.mockClear();
    await wrapped.post(`${SERVER}/Items/a/PlaybackInfo`, { data: { ItemId: 'a' } });
    await wrapped.post(`${SERVER}/Sessions/Playing`, { data: { ItemId: 'a' } });
    expect(onOutcome).not.toHaveBeenCalled();
  });

  it('reports rejected and failed reports as undelivered', async () => {
    const failing = setup(async () => ({ statusCode: 503 }));
    await failing.wrapped.post(`${SERVER}/UserPlayedItems/a`);
    expect(failing.onOutcome).toHaveBeenCalledWith({
      kind: 'watched',
      serverUrl: SERVER,
      itemId: 'a',
      watched: true,
      delivered: false,
    });

    const empty = setup(async () => undefined);
    await empty.wrapped.post(`${SERVER}/UserPlayedItems/a`);
    expect(empty.onOutcome).toHaveBeenCalledWith(expect.objectContaining({ delivered: false }));

    const throwing = setup(async () => {
      throw new Error('offline');
    });
    await expect(
      throwing.wrapped.post(`${SERVER}/Sessions/Playing/Progress`, {
        data: { ItemId: 'a', PositionTicks: 3 },
      })
    ).rejects.toThrow('offline');
    expect(throwing.onOutcome).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'progress', itemId: 'a', delivered: false })
    );
    expect(throwing.log).toHaveBeenCalledWith(
      'Playback report progress for a did not reach the server'
    );
    throwing.onOutcome.mockClear();
    await expect(throwing.wrapped.post(`${SERVER}/Other`, {})).rejects.toThrow('offline');
    expect(throwing.onOutcome).not.toHaveBeenCalled();
  });
});

describe('createPlaybackSyncQueue', () => {
  let files;
  let http;
  let prefs;
  let servers;
  let log;

  function createQueue(options = {}) {
    return createPlaybackSyncQueue({
      file: {
        exists: vi.fn((path) => files.has(path)),
        read: vi.fn((path) => files.get(path)),
        write: vi.fn((path, content) => files.set(path, content)),
      },
      http,
      preferences: { get: vi.fn((key) => prefs.get(key)) },
      buildJellyfinHeaders: vi.fn((token, extra) => ({
        Authorization: `Token="${token}"`,
        ...extra,
      })),
      loadStoredServers: vi.fn(() => servers),
      log,
      ...options,
    });
  }

  const stored = () => JSON.parse(files.get(QUEUE_FILE));
  const undeliveredStop = (overrides = {}) => ({
    kind: 'stopped',
    serverUrl: `${SERVER}/`,
    itemId: 'a',
    positionTicks: 1200,
    mediaSourceId: 'src-a',
    delivered: false,
    ...overrides,
  });

  beforeEach(() => {
    vi.useFakeTimers();
    files = new Map();
    http = { post: vi.fn(async () => ({ statusCode: 204 })) };
    prefs = new Map([['sync_playback_progress', true]]);
    servers = [
      { serverUrl: `${SERVER}/`, accessToken: 'url-token' },
      { serverUrl: SERVER, accessToken: 'user-token', userId: 'u1' },
    ];
    log = vi.fn();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('keeps undelivered reports and delivers them with the stored credentials', async () => {
    const queue = createQueue();
    queue.record(undeliveredStop());
    expect(stored()).toEqual([
      {
        serverUrl: SERVER,
        itemId: 'a',
        mediaSourceId: 'src-a',
        positionTicks: 1200,
        watched: false,
        updatedAt: expect.any(Number),
      },
    ]);
    expect(log).toHaveBeenCalledWith('Queued playback stopped of a for a later sync');

    // The sync runs right away
    await vi.advanceTimersByTimeAsync(0);
    expect(http.post).toHaveBeenCalledWith(`${SERVER}/Sessions/Playing/Stopped?ApiKey=user-token`, {
      headers: {
        Authorization: 'Token="user-token"',
        'Content-Type': 'application/json',
        Accept: 'application/json',
      },
      data: { ItemId: 'a', MediaSourceId: 'src-a', PositionTicks: 1200 },
    });
    expect(stored()).toEqual([]);
    expect(queue.pending()).toEqual([]);
    expect(log).toHaveBeenCalledWith(`Synced playback of a with ${SERVER}`);
  });

  it('merges reports per item and drops what a delivered report covers', async () => {
    const queue = createQueue();
    queue.record({ ...undeliveredStop(), kind: 'progress', positionTicks: 100 });
    queue.record({
      kind: 'watched',
      serverUrl: SERVER,
      itemId: 'a',
      watched: true,
      delivered: false,
    });
    queue.record(undeliveredStop({ positionTicks: 2000, mediaSourceId: null }));
    expect(queue.pending()).toEqual([
      expect.objectContaining({
        itemId: 'a',
        positionTicks: 2000,
        mediaSourceId: null,
        watched: true,
      }),
    ]);
    // Zero positions never replace a real one
    queue.record(undeliveredStop({ positionTicks: 0 }));
    expect(queue.pending()[0].positionTicks).toBe(2000);

    // The server took the stop: only the watched flag is left
    queue.record(undeliveredStop({ delivered: true }));
    expect(queue.pending()).toEqual([
      expect.objectContaining({ positionTicks: null, watched: true }),
    ]);
    queue.record({ kind: 'watched', serverUrl: SERVER, itemId: 'a', delivered: true });
    expect(queue.pending()).toEqual([]);
    expect(stored()).toEqual([]);

    // Delivered reports for unknown items and junk change nothing
    const writes = files.get(QUEUE_FILE);
    queue.record(undeliveredStop({ itemId: 'zzz', delivered: true }));
    queue.record({ kind: 'stopped', serverUrl: SERVER, positionTicks: 5, delivered: false });
    queue.record(null);
    expect(files.get(QUEUE_FILE)).toBe(writes);
    // An undelivered zero position alone is nothing to keep
    queue.record(undeliveredStop({ itemId: 'b', positionTicks: 0 }));
    expect(queue.pending()).toEqual([]);
  });

  it('retries later when the server is still unreachable', async () => {
    http.post.mockRejectedValue(new Error('offline'));
    const queue = createQueue({ retryMs: 60000 });
    queue.record(undeliveredStop());
    await vi.advanceTimersByTimeAsync(0);
    expect(http.post).toHaveBeenCalledTimes(1);
    expect(log).toHaveBeenCalledWith('Playback sync of a failed: offline');
    expect(queue.pending()).toHaveLength(1);

    http.post.mockResolvedValue({ statusCode: 500 });
    await vi.advanceTimersByTimeAsync(60000);
    expect(http.post).toHaveBeenCalledTimes(2);
    expect(queue.pending()).toHaveLength(1);

    http.post.mockResolvedValue({ statusCode: 204 });
    await vi.advanceTimersByTimeAsync(60000);
    expect(http.post).toHaveBeenCalledTimes(3);
    expect(queue.pending()).toEqual([]);
    await vi.advanceTimersByTimeAsync(60000);
    expect(http.post).toHaveBeenCalledTimes(3);
  });

  it('delivers the watched flag after the position and keeps it on failure', async () => {
    const queue = createQueue();
    queue.record(undeliveredStop());
    queue.record({ kind: 'watched', serverUrl: SERVER, itemId: 'a', delivered: false });
    http.post.mockResolvedValueOnce({ statusCode: 204 }).mockResolvedValueOnce(undefined);
    await vi.advanceTimersByTimeAsync(0);
    expect(http.post.mock.calls.map((call) => call[0])).toEqual([
      `${SERVER}/Sessions/Playing/Stopped?ApiKey=user-token`,
      `${SERVER}/UserPlayedItems/a?ApiKey=user-token`,
    ]);
    expect(http.post.mock.calls[1][1]).toEqual({
      headers: {
        Authorization: 'Token="user-token"',
        'Content-Type': 'application/json',
        Accept: 'application/json',
      },
      data: undefined,
    });
    expect(queue.pending()).toEqual([
      expect.objectContaining({ positionTicks: null, watched: true }),
    ]);

    await vi.advanceTimersByTimeAsync(60000);
    expect(http.post).toHaveBeenCalledTimes(3);
    expect(queue.pending()).toEqual([]);
  });

  it('waits for credentials and for the preference', async () => {
    servers = [{ serverUrl: 'http://other', accessToken: 'x' }, { accessToken: 'no-url' }, null];
    const queue = createQueue();
    queue.record(undeliveredStop());
    await vi.advanceTimersByTimeAsync(0);
    expect(http.post).not.toHaveBeenCalled();
    expect(log).toHaveBeenCalledWith(`No credentials for ${SERVER}, keeping a queued`);
    expect(queue.pending()).toHaveLength(1);

    // No server list at all is handled like no credentials
    servers = null;
    await vi.advanceTimersByTimeAsync(60000);
    expect(http.post).not.toHaveBeenCalled();

    // A server without a user id is still good enough, and an entry without
    // a media source id reports the item itself
    servers = [{ serverUrl: SERVER, accessToken: 'url-only' }];
    queue.record(undeliveredStop({ mediaSourceId: undefined }));
    await vi.advanceTimersByTimeAsync(60000);
    expect(http.post).toHaveBeenCalledWith(
      `${SERVER}/Sessions/Playing/Stopped?ApiKey=url-only`,
      expect.objectContaining({ data: { ItemId: 'a', MediaSourceId: 'a', PositionTicks: 1200 } })
    );

    prefs.set('sync_playback_progress', false);
    queue.record(undeliveredStop({ itemId: 'b' }));
    await expect(queue.sync()).resolves.toBe(false);
    expect(queue.pending()).toHaveLength(1);
  });

  it('runs one sync at a time and answers whether everything went out', async () => {
    let release;
    http.post.mockImplementation(
      () =>
        new Promise((resolve) => {
          release = () => resolve({ statusCode: 204 });
        })
    );
    const queue = createQueue();
    queue.record(undeliveredStop());
    const first = queue.sync();
    await expect(queue.sync()).resolves.toBe(false);
    release();
    await expect(first).resolves.toBe(true);
    await expect(queue.sync()).resolves.toBe(true);
    // The immediate retry scheduled by record found nothing left to do
    await vi.advanceTimersByTimeAsync(0);
    expect(http.post).toHaveBeenCalledTimes(1);
  });

  it('syncs on request only when something is queued and enough time passed', async () => {
    http.post.mockRejectedValue(new Error('offline'));
    const queue = createQueue({ minGapMs: 5000 });
    queue.requestSync();
    await vi.advanceTimersByTimeAsync(0);
    expect(http.post).not.toHaveBeenCalled();

    queue.record(undeliveredStop());
    await vi.advanceTimersByTimeAsync(0);
    expect(http.post).toHaveBeenCalledTimes(1);
    // Too soon after the last attempt
    queue.requestSync();
    await vi.advanceTimersByTimeAsync(0);
    expect(http.post).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(5000);
    queue.stop();
    queue.requestSync();
    await vi.advanceTimersByTimeAsync(0);
    expect(http.post).toHaveBeenCalledTimes(2);
    // Requesting right after an attempt changes nothing
    queue.requestSync();
    queue.stop();
    await vi.advanceTimersByTimeAsync(120000);
    expect(http.post).toHaveBeenCalledTimes(2);
  });

  it('lets an immediate sync replace a pending retry, never the other way round', async () => {
    http.post.mockRejectedValue(new Error('offline'));
    const queue = createQueue({ retryMs: 60000 });
    queue.scheduleSync(60000);
    queue.record(undeliveredStop());
    await vi.advanceTimersByTimeAsync(0);
    expect(http.post).toHaveBeenCalledTimes(1);

    // The failed sync armed a retry; another retry request keeps the earlier one
    queue.scheduleSync(1000);
    await vi.advanceTimersByTimeAsync(1000);
    expect(http.post).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(59000);
    expect(http.post).toHaveBeenCalledTimes(2);
  });

  it('reads an existing queue and survives a broken one', async () => {
    files.set(
      QUEUE_FILE,
      JSON.stringify([
        { serverUrl: SERVER, itemId: 'kept', positionTicks: 9, watched: false },
        { itemId: 'no-server' },
        { serverUrl: SERVER },
        null,
      ])
    );
    expect(createQueue().pending()).toEqual([
      { serverUrl: SERVER, itemId: 'kept', positionTicks: 9, watched: false },
    ]);

    files.set(QUEUE_FILE, '{"not":"a list"}');
    expect(createQueue().pending()).toEqual([]);

    files.set(QUEUE_FILE, 'garbage');
    const broken = createQueue();
    expect(broken.pending()).toEqual([]);
    expect(log).toHaveBeenCalledWith(
      expect.stringContaining('Could not read the playback sync queue:')
    );

    const queue = createQueue({
      file: {
        exists: () => false,
        read: () => '',
        write: () => {
          throw new Error('disk full');
        },
      },
    });
    queue.record(undeliveredStop());
    expect(log).toHaveBeenCalledWith('Could not save the playback sync queue: disk full');
    expect(queue.pending()).toHaveLength(1);
  });
});
