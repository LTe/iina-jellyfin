import http from 'node:http';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

/**
 * A tiny Jellyfin look-alike for end-to-end tests. It serves the sidebar's own
 * files under /ui/ (standing in for the plugin folder IINA loads them from) and
 * enough of the Jellyfin REST API for the sidebar to connect, browse and
 * download. Flip `state.online` to false to simulate losing the connection:
 * API requests are then dropped at the socket level while /ui/ keeps working,
 * exactly like a laptop whose home server is out of reach.
 */
export const TOKEN = 'e2e-access-token';
export const PASSWORD = 'secret';
export const USER = { Id: 'user-1', Name: 'tester' };
const LIBRARIES = {
  movies: {
    Id: 'lib-movies',
    Name: 'Movies',
    CollectionType: 'movies',
    Type: 'CollectionFolder',
    IsFolder: true,
    ServerId: 'server-1',
  },
  shows: {
    Id: 'lib-shows',
    Name: 'Shows',
    CollectionType: 'tvshows',
    Type: 'CollectionFolder',
    IsFolder: true,
    ServerId: 'server-1',
  },
};

/** The user as Jellyfin Web expects it: with policy and configuration */
function fullUser() {
  return {
    ...USER,
    ServerId: 'server-1',
    HasPassword: true,
    Policy: {
      IsAdministrator: false,
      EnableMediaPlayback: true,
      EnableContentDownloading: true,
      EnabledFolders: [],
      EnableAllFolders: true,
    },
    Configuration: { PlayDefaultAudioTrack: true, SubtitleMode: 'Default' },
  };
}

export const ITEMS = {
  movie: {
    Id: 'movie-1',
    Type: 'Movie',
    MediaType: 'Video',
    IsFolder: false,
    ServerId: 'server-1',
    Name: 'Big Film',
    ProductionYear: 2020,
    RunTimeTicks: 66 * 600000000,
  },
  broken: {
    Id: 'broken-1',
    Type: 'Movie',
    MediaType: 'Video',
    IsFolder: false,
    ServerId: 'server-1',
    Name: 'Broken Film',
    ProductionYear: 2021,
  },
  slow: {
    Id: 'slow-1',
    Type: 'Movie',
    MediaType: 'Video',
    IsFolder: false,
    ServerId: 'server-1',
    Name: 'Slow Film',
    ProductionYear: 2022,
  },
  series: { Id: 'series-1', Type: 'Series', IsFolder: true, ServerId: 'server-1', Name: 'Show' },
  season: {
    Id: 'season-1',
    Type: 'Season',
    IsFolder: true,
    ServerId: 'server-1',
    Name: 'Season 1',
    IndexNumber: 1,
    SeriesId: 'series-1',
  },
  episode: {
    Id: 'ep-1',
    MediaType: 'Video',
    IsFolder: false,
    ServerId: 'server-1',
    Type: 'Episode',
    Name: 'Pilot',
    SeriesName: 'Show',
    SeriesId: 'series-1',
    SeasonId: 'season-1',
    ParentIndexNumber: 1,
    IndexNumber: 1,
    RunTimeTicks: 25 * 600000000,
    MediaSources: [{ Id: 'src-ep-1' }],
  },
};

const MEDIA_SIZES = { 'movie-1': 256 * 1024, 'ep-1': 64 * 1024, 'slow-1': 2 * 1024 * 1024 };
// Bitrate the "server" reports for each source; a download capped below it is transcoded
const SOURCE_BITRATES = { 'movie-1': 6000000, 'ep-1': 3000000, 'slow-1': 20000000 };

const SUBTITLES = {
  'movie-1': [
    { Index: 2, Language: 'eng', Codec: 'subrip', DisplayTitle: 'English' },
    { Index: 3, Language: 'pol', Codec: 'subrip', DisplayTitle: 'Polish' },
    { Index: 4, Language: 'ger', Codec: 'pgssub', IsTextSubtitleStream: false },
  ],
  'ep-1': [{ Index: 2, Language: 'eng', Codec: 'subrip', DisplayTitle: 'English' }],
};

const STATIC_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.svg': 'image/svg+xml',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.eot': 'application/vnd.ms-fontobject',
  '.wasm': 'application/wasm',
  '.txt': 'text/plain',
};

/**
 * Deterministic file content: a repeating pattern derived from the item id, so
 * the test can verify the downloaded bytes without storing fixtures.
 */
export function mediaBytes(itemId) {
  const size = MEDIA_SIZES[itemId] || 32 * 1024;
  return Buffer.alloc(size, `${itemId}:`);
}

/**
 * What the "transcoder" produces: a smaller, different pattern, so a test can
 * tell a transcoded download from the original bytes.
 */
export function transcodedBytes(itemId, bitrate) {
  return Buffer.alloc(48 * 1024, `${itemId}@${bitrate}:`);
}

export function subtitleText(itemId, index) {
  const stream = (SUBTITLES[itemId] || []).find((entry) => entry.Index === index);
  const language = stream ? stream.Language : 'unknown';
  return `1\n00:00:01,000 --> 00:00:02,000\nHello from ${itemId} in ${language}\n`;
}

function subtitleStreams(itemId) {
  return (SUBTITLES[itemId] || []).map((stream) => ({
    Type: 'Subtitle',
    IsExternal: true,
    IsTextSubtitleStream: true,
    ...stream,
  }));
}

function readJsonBody(req, callback) {
  let raw = '';
  req.on('data', (chunk) => {
    raw += chunk;
  });
  req.on('end', () => {
    let body = {};
    try {
      body = raw ? JSON.parse(raw) : {};
    } catch {
      body = {};
    }
    callback(body);
  });
}

function isAuthorized(req) {
  const token = req.headers['x-emby-token'];
  const authorization = req.headers.authorization || '';
  return token === TOKEN || authorization.includes(`Token="${TOKEN}"`);
}

export function createMockJellyfin({ uiDir, webDir = null, iinaDir = null }) {
  const state = {
    online: true,
    requests: [],
    playbackInfoRequests: [],
    playbackStops: [],
    playedItems: [],
    logins: [],
    brokenItems: new Set(['broken-1']),
  };
  let server = null;
  const sockets = new Set();

  function json(res, status, body) {
    const payload = JSON.stringify(body);
    res.writeHead(status, {
      'Content-Type': 'application/json',
      'Content-Length': Buffer.byteLength(payload),
    });
    res.end(payload);
  }

  // Static roots: the plugin's own sidebar under /ui/, the vendored Jellyfin
  // Web client under /web/ and the IINA integration it loads under /iina/
  // (the client references it as ../iina/, exactly as inside the plugin).
  const staticRoots = [
    ['/ui/', uiDir],
    ['/web/', webDir],
    ['/iina/', iinaDir],
  ];

  function staticRootFor(pathname) {
    return staticRoots.find(([prefix, dir]) => dir && pathname.startsWith(prefix)) || null;
  }

  function serveStatic(req, res, pathname) {
    const [prefix, dir] = staticRootFor(pathname);
    const relative = decodeURIComponent(pathname.slice(prefix.length)) || 'index.html';
    const filePath = path.normalize(path.join(dir, relative));
    if (
      !filePath.startsWith(dir) ||
      !fs.existsSync(filePath) ||
      fs.statSync(filePath).isDirectory()
    ) {
      res.writeHead(404);
      res.end('not found');
      return;
    }
    res.writeHead(200, {
      'Content-Type': STATIC_TYPES[path.extname(filePath)] || 'application/octet-stream',
    });
    fs.createReadStream(filePath).pipe(res);
  }

  function streamSlowly(res, bytes) {
    // 2 MB in 64 KB chunks every 100 ms: long enough to cancel mid-way
    const chunkSize = 64 * 1024;
    let offset = 0;
    res.writeHead(200, { 'Content-Type': 'video/x-matroska', 'Content-Length': bytes.length });
    const timer = setInterval(() => {
      if (res.destroyed) {
        clearInterval(timer);
        return;
      }
      res.write(bytes.subarray(offset, offset + chunkSize));
      offset += chunkSize;
      if (offset >= bytes.length) {
        clearInterval(timer);
        res.end();
      }
    }, 100);
  }

  function handleApi(req, res, url) {
    const { pathname, searchParams } = url;
    let match;

    if (pathname === '/System/Info/Public') {
      return json(res, 200, {
        ServerName: 'Mock Jellyfin',
        Version: '12.0.0',
        Id: 'server-1',
        StartupWizardCompleted: true,
      });
    }
    // Sign-in flow of Jellyfin Web (before any token exists)
    if (pathname.toLowerCase() === '/users/public') {
      return json(res, 200, [{ ...USER, HasPassword: true }]);
    }
    if (pathname === '/Branding/Configuration' || pathname === '/Branding/Css') {
      return json(res, 200, { LoginDisclaimer: '', CustomCss: '', SplashscreenEnabled: false });
    }
    if (pathname === '/QuickConnect/Enabled') {
      return json(res, 200, false);
    }
    if (pathname.toLowerCase() === '/users/authenticatebyname' && req.method === 'POST') {
      return readJsonBody(req, (body) => {
        state.logins.push(body);
        if (body.Username !== USER.Name || body.Pw !== PASSWORD) {
          return json(res, 401, { error: 'bad credentials' });
        }
        return json(res, 200, {
          User: fullUser(),
          AccessToken: TOKEN,
          ServerId: 'server-1',
          SessionInfo: { Id: 'session-1' },
        });
      });
    }
    if (!isAuthorized(req)) {
      return json(res, 401, { error: 'unauthorized' });
    }
    if (pathname === '/System/Info') {
      return json(res, 200, {
        ServerName: 'Mock Jellyfin',
        Id: 'server-1',
        Version: '12.0.0',
        OperatingSystem: 'Linux',
      });
    }
    if (pathname === '/Users/Me' || pathname === `/Users/${USER.Id}`) {
      return json(res, 200, fullUser());
    }
    if (pathname === `/Users/${USER.Id}/Views` || pathname === '/UserViews') {
      return json(res, 200, { Items: [LIBRARIES.movies, LIBRARIES.shows], TotalRecordCount: 2 });
    }
    if (pathname.startsWith('/DisplayPreferences/')) {
      if (req.method === 'POST') {
        return readJsonBody(req, () => {
          res.writeHead(204);
          res.end();
        });
      }
      return json(res, 200, {
        Id: 'usersettings',
        CustomPrefs: {},
        SortBy: 'SortName',
        SortOrder: 'Ascending',
        Client: 'emby',
      });
    }
    if (pathname === '/Sessions/Capabilities/Full' || pathname === '/Sessions/Capabilities') {
      return readJsonBody(req, () => {
        res.writeHead(204);
        res.end();
      });
    }
    if (pathname === '/SyncPlay/List') {
      return json(res, 200, []);
    }
    if (pathname === '/System/Endpoint') {
      return json(res, 200, { IsLocal: true, IsInNetwork: true });
    }
    if (pathname === '/Playback/BitrateTest') {
      res.writeHead(200, { 'Content-Type': 'application/octet-stream' });
      return res.end(Buffer.alloc(Number(searchParams.get('size')) || 1024));
    }
    if (pathname === '/Sessions/Logout') {
      res.writeHead(204);
      return res.end();
    }
    if (pathname === '/Items/Counts' || pathname === '/Localization/Cultures') {
      return json(res, 200, pathname === '/Items/Counts' ? {} : []);
    }
    if (pathname === '/Items/Filters2' || pathname === '/Items/Filters') {
      return json(res, 200, { Genres: [], Tags: [], OfficialRatings: [], Years: [] });
    }
    if (pathname === '/Users/user-1/Items/Latest' || pathname === '/Items/Suggestions') {
      return json(
        res,
        200,
        pathname.endsWith('Latest') ? [ITEMS.movie, ITEMS.series] : { Items: [] }
      );
    }
    if (pathname === '/Items/Latest') {
      return json(res, 200, [ITEMS.movie, ITEMS.series]);
    }
    if (pathname === '/UserItems/Resume') {
      return json(res, 200, { Items: [] });
    }
    if (pathname === '/Shows/NextUp') {
      return json(res, 200, { Items: [ITEMS.episode] });
    }
    if (pathname === '/Genres' || pathname === '/MusicGenres' || pathname === '/Artists') {
      return json(res, 200, { Items: [] });
    }
    if (pathname === '/Search/Hints') {
      return json(res, 200, {
        SearchHints: [{ ItemId: 'movie-1', Type: 'Movie', Name: 'Big Film', ProductionYear: 2020 }],
      });
    }
    if (pathname === '/Items' && searchParams.get('Ids')) {
      const ids = searchParams.get('Ids').split(',');
      const items = Object.values(ITEMS).filter((item) => ids.includes(item.Id));
      return json(res, 200, { Items: items, TotalRecordCount: items.length });
    }
    if (pathname === '/Items' && searchParams.get('ParentId')) {
      const parentId = searchParams.get('ParentId');
      const children =
        parentId === 'series-1' || parentId === 'season-1'
          ? [ITEMS.episode]
          : parentId === LIBRARIES.movies.Id
            ? [ITEMS.movie, ITEMS.broken, ITEMS.slow]
            : parentId === LIBRARIES.shows.Id
              ? [ITEMS.series]
              : [];
      return json(res, 200, { Items: children, TotalRecordCount: children.length });
    }
    if (pathname === '/Items') {
      const types = searchParams.get('IncludeItemTypes') || '';
      if (types.includes('Movie')) {
        return json(res, 200, { Items: [ITEMS.movie, ITEMS.broken, ITEMS.slow] });
      }
      if (types.includes('Series')) {
        return json(res, 200, { Items: [ITEMS.series] });
      }
      return json(res, 200, { Items: [] });
    }
    if (pathname === '/Shows/series-1/Seasons') {
      return json(res, 200, { Items: [ITEMS.season] });
    }
    if (pathname === '/Shows/series-1/Episodes') {
      return json(res, 200, { Items: [ITEMS.episode] });
    }
    if (pathname === '/Sessions/Playing' || pathname === '/Sessions/Playing/Progress') {
      return readJsonBody(req, () => {
        res.writeHead(204);
        res.end();
      });
    }
    if (pathname === '/Sessions/Playing/Stopped') {
      // Playback reports: what the server learns about resume positions
      return readJsonBody(req, (body) => {
        state.playbackStops.push(body);
        res.writeHead(204);
        res.end();
      });
    }
    if ((match = pathname.match(/^\/UserPlayedItems\/([^/]+)$/))) {
      state.playedItems.push(match[1]);
      return json(res, 200, { Played: true });
    }
    if ((match = pathname.match(/^\/Items\/([^/]+)\/PlaybackInfo$/))) {
      const itemId = match[1];
      const source = {
        Id: `src-${itemId}`,
        Container: 'mkv,webm',
        Size: mediaBytes(itemId).length,
        Bitrate: SOURCE_BITRATES[itemId] || 1000000,
        MediaStreams: [{ Type: 'Video' }, ...subtitleStreams(itemId)],
      };
      if (req.method === 'POST') {
        // A capped request with a device profile: transcode when the source
        // is above the cap, exactly what Jellyfin does.
        return readJsonBody(req, (body) => {
          state.playbackInfoRequests.push({ itemId, body });
          const cap = Number(body.MaxStreamingBitrate) || 0;
          const hasProfile = Boolean(body.DeviceProfile && body.DeviceProfile.TranscodingProfiles);
          if (hasProfile && cap > 0 && cap < source.Bitrate) {
            source.TranscodingUrl = `/Videos/${itemId}/stream.mp4?MediaSourceId=src-${itemId}&VideoBitrate=${cap}&PlaySessionId=ps-1&api_key=${TOKEN}`;
          }
          json(res, 200, { PlaySessionId: 'ps-1', MediaSources: [source] });
        });
      }
      return json(res, 200, { PlaySessionId: 'ps-1', MediaSources: [source] });
    }
    if ((match = pathname.match(/^\/Videos\/([^/]+)\/stream\.mp4$/))) {
      // Transcoded output: chunked, no Content-Length, like a live encode
      const itemId = match[1];
      const bytes = transcodedBytes(itemId, searchParams.get('VideoBitrate'));
      res.writeHead(200, { 'Content-Type': 'video/mp4', 'Transfer-Encoding': 'chunked' });
      res.write(bytes.subarray(0, bytes.length / 2));
      setTimeout(() => res.end(bytes.subarray(bytes.length / 2)), 150);
      return undefined;
    }
    if ((match = pathname.match(/^\/Items\/([^/]+)\/Images\//))) {
      res.writeHead(404);
      return res.end();
    }
    if (pathname.match(/^\/Items\/[^/]+\/ThemeMedia$/)) {
      const empty = { Items: [], TotalRecordCount: 0, OwnerId: '' };
      return json(res, 200, {
        ThemeVideosResult: empty,
        ThemeSongsResult: empty,
        SoundtrackSongsResult: empty,
      });
    }
    if (
      pathname.match(
        /^\/Items\/[^/]+\/(Similar|Collections|SpecialFeatures|LocalTrailers|CriticReviews)$/
      )
    ) {
      return json(res, 200, /Similar|Collections/.test(pathname) ? { Items: [] } : []);
    }
    if ((match = pathname.match(/^\/(?:Users\/[^/]+\/)?Items\/([^/]+)$/))) {
      const item = Object.values(ITEMS).find((entry) => entry.Id === match[1]);
      return item ? json(res, 200, item) : json(res, 404, { error: 'unknown item' });
    }
    if ((match = pathname.match(/^\/Videos\/([^/]+)\/stream$/))) {
      const itemId = match[1];
      if (state.brokenItems.has(itemId)) {
        return json(res, 500, { error: 'transcoder exploded' });
      }
      const bytes = mediaBytes(itemId);
      if (itemId === 'slow-1') {
        return streamSlowly(res, bytes);
      }
      res.writeHead(200, { 'Content-Type': 'video/x-matroska', 'Content-Length': bytes.length });
      return res.end(bytes);
    }
    if ((match = pathname.match(/^\/Videos\/([^/]+)\/[^/]+\/Subtitles\/(\d+)\/stream\.\w+$/))) {
      const text = subtitleText(match[1], Number(match[2]));
      res.writeHead(200, {
        'Content-Type': 'text/plain',
        'Content-Length': Buffer.byteLength(text),
      });
      return res.end(text);
    }
    return json(res, 404, { error: `no route for ${pathname}` });
  }

  function requestListener(req, res) {
    const url = new URL(req.url, 'http://localhost');
    if (staticRootFor(url.pathname)) {
      return serveStatic(req, res, url.pathname);
    }
    state.requests.push({ method: req.method, path: url.pathname, query: url.search });
    if (!state.online) {
      // Nothing answers: the same failure a client sees without network.
      req.socket.destroy();
      return undefined;
    }
    return handleApi(req, res, url);
  }

  return {
    state,
    mediaBytes,
    subtitleText,
    async start() {
      server = http.createServer(requestListener);
      // Jellyfin Web opens a WebSocket for server events; accept it and stay
      // quiet, which is what an idle server does.
      server.on('upgrade', (req, socket) => {
        sockets.add(socket);
        socket.on('close', () => sockets.delete(socket));
        const key = req.headers['sec-websocket-key'];
        const accept = crypto
          .createHash('sha1')
          .update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
          .digest('base64');
        socket.write(
          [
            'HTTP/1.1 101 Switching Protocols',
            'Upgrade: websocket',
            'Connection: Upgrade',
            `Sec-WebSocket-Accept: ${accept}`,
            '',
            '',
          ].join('\r\n')
        );
        socket.on('error', () => {});
      });
      await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
      const { port } = server.address();
      this.baseUrl = `http://127.0.0.1:${port}`;
      return this.baseUrl;
    },
    async stop() {
      if (!server) return;
      // Idle keep-alive and WebSocket connections would keep close() waiting
      for (const socket of sockets) socket.destroy();
      sockets.clear();
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
      server = null;
    },
    requestsTo(pathname) {
      return state.requests.filter((request) => request.path === pathname);
    },
  };
}
