'use strict';

const { subtitleExtensionForCodec, isExternalTextSubtitle } = require('./subtitle-utils.js');
const {
  resolveQualityPreset,
  listQualityPresets,
  buildDownloadDeviceProfile,
  containerFromTranscodingUrl,
} = require('./download-profile.js');

const DEFAULT_DIRECTORY = '@data/offline';
const MANIFEST_FILE = 'manifest.json';
// IINA's utils.exec does not search PATH, so system tools need absolute paths.
const MKDIR_BINARY = '/bin/mkdir';
const RM_BINARY = '/bin/rm';
const DOWNLOADABLE_TYPES = ['Movie', 'Episode', 'Audio'];
// What to do when an item that is about to be streamed has a downloaded copy.
const PLAYBACK_MODES = {
  LOCAL: 'local',
  ASK: 'ask',
  STREAM: 'stream',
};

const STATUS = {
  QUEUED: 'queued',
  DOWNLOADING: 'downloading',
  COMPLETED: 'completed',
  FAILED: 'failed',
  CANCELLED: 'cancelled',
};

function sanitizeId(value) {
  return String(value).replace(/[^a-zA-Z0-9_-]/g, '_');
}

function normalizeServerUrl(url) {
  return String(url || '').replace(/\/+$/, '');
}

/**
 * Whether a path lives in one of the plugin's own folders. IINA's file API
 * only overwrites and deletes files there; anywhere else (a folder picked by
 * the user) has to go through rm.
 */
function isPluginLocalPath(path) {
  return /^@(?:data|tmp)\//.test(String(path));
}

/**
 * Jellyfin item id in a playback URL (/Videos/{id}/stream, /Audio/{id}/stream
 * or the older /Items/{id}/...). Local paths never yield one.
 */
function itemIdFromStreamUrl(url) {
  const text = String(url || '');
  if (!/^https?:\/\//i.test(text)) {
    return null;
  }
  const match = text.match(/\/(?:Videos|Audio|Items)\/([^/?#]+)/);
  return match ? match[1] : null;
}

/**
 * Plain JSON copy of a value. IINA relays messages to the webview as JSON and
 * refuses payloads that are not strictly serializable (undefined, NaN, ...).
 */
function toMessageData(value) {
  return JSON.parse(JSON.stringify(value));
}

/**
 * File name (without extension) for a download: the display title, stripped
 * of characters the file system rejects. Falls back to the item id.
 */
function sanitizeFileName(value) {
  const cleaned = String(value || '')
    .replace(/[\\/:*?"<>|]|\p{Cc}/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 120)
    .replace(/[\s.]+$/, '');
  return cleaned;
}

function padNumber(value) {
  return String(value).padStart(2, '0');
}

/**
 * Title used for the player window and the downloads list, mirroring what
 * the streaming flow puts into force-media-title.
 */
function buildDisplayTitle(item) {
  const name = item.Name || 'Unknown Title';
  if (item.Type === 'Episode' && item.SeriesName) {
    const hasNumbers = item.ParentIndexNumber !== undefined && item.IndexNumber !== undefined;
    const code = hasNumbers
      ? ` S${padNumber(item.ParentIndexNumber)}E${padNumber(item.IndexNumber)}`
      : '';
    return `${item.SeriesName}${code} - ${name}`;
  }
  if (item.Type === 'Movie' && item.ProductionYear) {
    return `${name} (${item.ProductionYear})`;
  }
  if (item.Type === 'Audio') {
    const artist = item.AlbumArtist || (Array.isArray(item.Artists) ? item.Artists.join(', ') : '');
    return artist ? `${artist} - ${name}` : name;
  }
  return name;
}

/**
 * Container extension for the downloaded file. Jellyfin reports containers as
 * a comma separated list of aliases ("mov,mp4,m4a"); the first one is used.
 */
function pickContainer(source, itemType) {
  const reported = String((source && source.Container) || '')
    .split(',')[0]
    .toLowerCase()
    .replace(/[^a-z0-9]/g, '');
  if (reported) return reported;
  return itemType === 'Audio' ? 'mp3' : 'mkv';
}

/**
 * Local path notation of a loaded file, so it can be compared with the paths
 * stored in the manifest. IINA reports local files both as plain paths and as
 * percent-encoded file:// URLs.
 */
function normalizeLoadedPath(fileUrl) {
  const path = String(fileUrl || '').replace(/^file:\/\/(localhost)?/i, '');
  try {
    return decodeURIComponent(path);
  } catch {
    return path;
  }
}

/**
 * Text subtitle streams of a media source, embedded or external. A transcoded
 * MP4 carries none of them, so all have to come down as sidecar files.
 */
function isTextSubtitle(stream) {
  return Boolean(stream && stream.Type === 'Subtitle' && stream.IsTextSubtitleStream === true);
}

// Every message a webview can send about offline downloads; the player entry
// relays these to whoever runs the downloads.
const OFFLINE_MESSAGES = [
  'get-offline-downloads',
  'offline-download',
  'offline-cancel',
  'offline-remove',
  'offline-retry',
  'play-offline',
  'offline-show-in-finder',
  'offline-open-folder',
  'offline-choose-folder',
  'offline-set-quality',
];

/**
 * The download manager. One instance owns the downloads and the manifest
 * (in IINA that is the global entry, which outlives player windows); player
 * windows create it with `readOnly` to look up finished downloads for local
 * playback, always reading the manifest fresh from disk and never writing.
 */
function createOfflineDownloadManager({
  file,
  utils,
  http,
  core,
  mpv,
  preferences,
  fetchPlaybackInfo,
  buildJellyfinHeaders,
  loadStoredServers,
  transport,
  notifyViews,
  openMedia,
  readOnly = false,
  log,
}) {
  let entries = [];
  let loadedFromDirectory = null;
  let activeItemId = null;
  // Access tokens for queued downloads live in memory only; the manifest on
  // disk never contains credentials.
  const credentials = {};

  function getDirectory() {
    const custom = String(preferences.get('offline_download_dir') || '').trim();
    return normalizeServerUrl(custom || DEFAULT_DIRECTORY);
  }

  function manifestPath() {
    return `${getDirectory()}/${MANIFEST_FILE}`;
  }

  function getQualityPreset(qualityId) {
    return resolveQualityPreset(qualityId ?? preferences.get('offline_download_quality'));
  }

  function setQuality(qualityId) {
    const preset = resolveQualityPreset(qualityId);
    preferences.set('offline_download_quality', preset.id);
    preferences.sync();
    log(`Offline download quality set to ${preset.label}`);
    broadcast();
    return preset;
  }

  /**
   * Let the user pick the download folder with the system dialog and store
   * it. Returns the chosen path, or null when the dialog was cancelled.
   * IINA's chooseFile answers with a promise.
   */
  async function chooseDownloadFolder() {
    let chosen = null;
    try {
      chosen = await utils.chooseFile('Choose the folder for offline downloads', {
        chooseDir: true,
      });
    } catch (error) {
      log(`Folder chooser failed: ${error.message}`);
    }
    if (!chosen) {
      log('Folder chooser cancelled');
      return null;
    }
    preferences.set('offline_download_dir', chosen);
    preferences.sync();
    log(`Offline download folder set to ${chosen}`);
    osd(`Offline downloads folder: ${chosen}`);
    broadcast();
    return chosen;
  }

  function osd(message) {
    try {
      core.osd(message);
    } catch (error) {
      log(`Could not show OSD: ${error.message}`);
    }
  }

  function readManifest() {
    const path = manifestPath();
    try {
      if (!file.exists(path)) {
        return [];
      }
      const parsed = JSON.parse(file.read(path));
      if (!Array.isArray(parsed)) {
        log('Offline manifest is not a list, ignoring it');
        return [];
      }
      return parsed.filter((entry) => entry && entry.itemId);
    } catch (error) {
      log(`Could not read offline manifest: ${error.message}`);
      return [];
    }
  }

  function getEntries() {
    if (readOnly) {
      // Another instance owns the manifest and may change it at any time
      return readManifest();
    }
    const directory = getDirectory();
    if (loadedFromDirectory !== directory) {
      entries = readManifest();
      loadedFromDirectory = directory;
      // A download that was still running when IINA quit cannot be resumed.
      for (const entry of entries) {
        if (entry.status === STATUS.QUEUED || entry.status === STATUS.DOWNLOADING) {
          entry.status = STATUS.FAILED;
          entry.error = 'Interrupted before the download finished';
          entry.progress = 0;
        }
      }
      log(`Loaded ${entries.length} offline download entries from ${directory}`);
    }
    return entries;
  }

  async function ensureDirectory() {
    const directory = getDirectory();
    if (file.exists(directory)) {
      return directory;
    }
    const resolved = utils.resolvePath(directory);
    log(`Creating offline download directory: ${resolved}`);
    await utils.exec(MKDIR_BINARY, ['-p', resolved]);
    if (!file.exists(directory)) {
      throw new Error(`Could not create download directory ${resolved}`);
    }
    return directory;
  }

  async function removeWithRm(path) {
    const resolved = utils.resolvePath(path);
    const result = await utils.exec(RM_BINARY, ['-f', resolved]);
    if (!result || result.status !== 0) {
      throw new Error(`rm exited with status ${result ? result.status : 'unknown'}`);
    }
  }

  /**
   * Write a file, replacing any previous one. Outside @data and @tmp IINA
   * refuses to overwrite, so the old file is removed first.
   */
  async function writeFile(path, content) {
    if (!isPluginLocalPath(path) && file.exists(path)) {
      await removeWithRm(path);
    }
    file.write(path, content);
  }

  async function saveManifest() {
    try {
      await ensureDirectory();
      await writeFile(manifestPath(), JSON.stringify(getEntries(), null, 2));
    } catch (error) {
      log(`Could not save offline manifest: ${error.message}`);
    }
  }

  function fileExists(path) {
    try {
      return Boolean(path) && file.exists(path);
    } catch (error) {
      log(`Could not check ${path}: ${error.message}`);
      return false;
    }
  }

  function publicEntry(entry) {
    return {
      ...entry,
      fileMissing: entry.status === STATUS.COMPLETED && !fileExists(entry.mediaPath),
    };
  }

  function listDownloads() {
    return getEntries().map(publicEntry);
  }

  /**
   * State shown by the webviews. Never throws: when the manifest or the
   * folder cannot be read, the presets still go out together with the error,
   * so the UI stays usable and says what is wrong.
   */
  function snapshot() {
    try {
      return toMessageData({
        downloads: listDownloads(),
        directory: utils.resolvePath(getDirectory()),
        quality: getQualityPreset().id,
        qualityPresets: listQualityPresets(),
        error: null,
      });
    } catch (error) {
      log(`Could not build the offline downloads snapshot: ${error.message}`);
      return {
        downloads: [],
        directory: null,
        quality: 'original',
        qualityPresets: listQualityPresets(),
        error: `Offline downloads unavailable: ${error.message}`,
      };
    }
  }

  function broadcast() {
    notifyViews('offline-downloads', snapshot());
  }

  async function persistAndBroadcast() {
    await saveManifest();
    broadcast();
  }

  function findEntry(itemId) {
    return getEntries().find((entry) => entry.itemId === itemId) || null;
  }

  /**
   * Base file name for an entry. Two different items with the same title
   * (a remake, a re-recorded song) get the item id appended, so their files
   * never overwrite each other.
   */
  function uniqueFileBaseName(entry) {
    const base = sanitizeFileName(entry.title) || sanitizeId(entry.itemId);
    const clash = getEntries().some(
      (other) => other.itemId !== entry.itemId && other.fileBase === base
    );
    return clash ? `${base} [${sanitizeId(entry.itemId)}]` : base;
  }

  function removeEntry(itemId) {
    entries = getEntries().filter((entry) => entry.itemId !== itemId);
  }

  async function deleteFile(path) {
    try {
      if (!fileExists(path)) {
        return;
      }
      if (isPluginLocalPath(path)) {
        file.delete(path);
      } else {
        await removeWithRm(path);
      }
    } catch (error) {
      log(`Could not delete ${path}: ${error.message}`);
    }
  }

  async function deleteEntryFiles(entry) {
    await deleteFile(entry.mediaPath);
    for (const subtitle of entry.subtitles || []) {
      await deleteFile(subtitle.path);
    }
  }

  function resolveStoredToken(serverUrl) {
    const wanted = normalizeServerUrl(serverUrl);
    const servers = loadStoredServers() || [];
    const match =
      servers.find((server) => server.userId && normalizeServerUrl(server.serverUrl) === wanted) ||
      servers.find((server) => normalizeServerUrl(server.serverUrl) === wanted);
    return match && match.accessToken ? match.accessToken : null;
  }

  function isDownloadable(item) {
    return Boolean(item && item.Id && DOWNLOADABLE_TYPES.includes(item.Type));
  }

  function createEntry(item, serverUrl, serverId, preset) {
    return {
      quality: preset.id,
      qualityLabel: preset.label,
      bitrate: preset.bitrate,
      transcoded: false,
      itemId: item.Id,
      type: item.Type,
      title: buildDisplayTitle(item),
      name: item.Name || 'Unknown Title',
      seriesName: item.SeriesName || null,
      seasonNumber: item.ParentIndexNumber ?? null,
      episodeNumber: item.IndexNumber ?? null,
      productionYear: item.ProductionYear || null,
      runTimeTicks: item.RunTimeTicks || null,
      serverUrl: normalizeServerUrl(serverUrl),
      serverId: serverId || null,
      status: STATUS.QUEUED,
      progress: 0,
      error: null,
      container: null,
      expectedBytes: null,
      fileBase: null,
      mediaPath: null,
      mediaAbsolutePath: null,
      subtitles: [],
      createdAt: Date.now(),
      completedAt: null,
    };
  }

  async function startDownload({ item, serverUrl, accessToken, serverId, quality } = {}) {
    if (!isDownloadable(item)) {
      log(`Item is not downloadable: ${item ? `${item.Type} ${item.Id}` : 'missing'}`);
      osd('This item cannot be downloaded for offline use');
      return null;
    }
    if (!serverUrl || !accessToken) {
      log('Offline download requested without server credentials');
      osd('Connect to a Jellyfin server before downloading');
      return null;
    }

    const existing = findEntry(item.Id);
    if (existing) {
      if (existing.status === STATUS.QUEUED || existing.status === STATUS.DOWNLOADING) {
        log(`Download already in progress for ${item.Id}`);
        osd(`Already downloading: ${existing.title}`);
        return existing;
      }
      if (existing.status === STATUS.COMPLETED && fileExists(existing.mediaPath)) {
        log(`Item already downloaded: ${item.Id}`);
        osd(`Already downloaded: ${existing.title}`);
        return existing;
      }
      // A failed download or one whose file went missing starts over.
      await deleteEntryFiles(existing);
      removeEntry(item.Id);
    }

    const entry = createEntry(item, serverUrl, serverId, getQualityPreset(quality));
    credentials[entry.itemId] = accessToken;
    getEntries().push(entry);
    log(`Queued offline download: ${entry.title} (${entry.qualityLabel})`);
    osd(`Queued for download: ${entry.title}`);

    await persistAndBroadcast();
    processQueue();
    return entry;
  }

  function processQueue() {
    if (activeItemId) {
      return;
    }
    const next = getEntries().find((entry) => entry.status === STATUS.QUEUED);
    if (!next) {
      return;
    }
    runDownload(next);
  }

  function updateProgress(entry, percent) {
    if (entry.status !== STATUS.DOWNLOADING) {
      return;
    }
    const rounded = Math.max(0, Math.min(100, Math.floor(percent)));
    if (rounded !== entry.progress) {
      entry.progress = rounded;
      broadcast();
    }
  }

  async function downloadSubtitles(entry, source, headers) {
    // The original file keeps its embedded tracks, so only sidecar files are
    // fetched; a transcoded MP4 loses every text track, so all come down.
    const wanted = entry.transcoded ? isTextSubtitle : isExternalTextSubtitle;
    const streams = (source.MediaStreams || []).filter(wanted);
    log(`Found ${streams.length} subtitle stream(s) to save for ${entry.itemId}`);
    const downloaded = [];
    const directory = getDirectory();
    const mediaSourceId = source.Id || entry.itemId;
    const usedNames = new Set();

    for (const stream of streams) {
      if (entry.status !== STATUS.DOWNLOADING) {
        break;
      }
      const language = stream.Language || 'unknown';
      const extension = subtitleExtensionForCodec(stream.Codec);
      const url = `${entry.serverUrl}/Videos/${entry.itemId}/${mediaSourceId}/Subtitles/${stream.Index}/stream.${extension}`;
      // Sidecar naming ("Title.eng.srt") that players match to the video;
      // a second track in the same language carries its stream index.
      let name = `${entry.fileBase}.${sanitizeId(language)}.${extension}`;
      if (usedNames.has(name)) {
        name = `${entry.fileBase}.${sanitizeId(language)}.${stream.Index}.${extension}`;
      }
      usedNames.add(name);
      const path = `${directory}/${name}`;
      try {
        await transport.download(url, path, { headers });
        downloaded.push({
          index: stream.Index,
          language,
          title: stream.DisplayTitle || stream.Title || language,
          codec: stream.Codec || null,
          path,
          absolutePath: utils.resolvePath(path),
        });
        log(`Downloaded subtitle ${language} (${stream.Index}) for ${entry.itemId}`);
      } catch (error) {
        log(`Subtitle ${language} (${stream.Index}) failed: ${error.message}`);
        await deleteFile(path);
      }
    }
    return downloaded;
  }

  function parseResponseData(response) {
    const data = response && response.data;
    if (typeof data === 'string') {
      return JSON.parse(data);
    }
    return data;
  }

  /**
   * Ask the server how to deliver the item. Original quality reads the plain
   * playback info; a capped quality posts a device profile and a bitrate
   * limit, and the server answers with a TranscodingUrl when the source is
   * above the cap (otherwise the original file is already small enough).
   */
  async function negotiateSource(entry, token, headers) {
    const preset = resolveQualityPreset(entry.quality);
    if (preset.bitrate === null) {
      const playbackInfo = await fetchPlaybackInfo(entry.serverUrl, entry.itemId, token);
      return { source: playbackInfo && playbackInfo.MediaSources && playbackInfo.MediaSources[0] };
    }

    const response = await http.post(`${entry.serverUrl}/Items/${entry.itemId}/PlaybackInfo`, {
      headers: { ...headers, 'Content-Type': 'application/json', Accept: 'application/json' },
      data: {
        DeviceProfile: buildDownloadDeviceProfile(preset.bitrate),
        MaxStreamingBitrate: preset.bitrate,
        StartTimeTicks: 0,
        IsPlayback: true,
        AutoOpenLiveStream: true,
      },
    });
    if (response && response.statusCode >= 400) {
      throw new Error(`PlaybackInfo failed with status ${response.statusCode}`);
    }
    const playbackInfo = parseResponseData(response);
    const source = playbackInfo && playbackInfo.MediaSources && playbackInfo.MediaSources[0];
    if (source && source.TranscodingUrl) {
      return { source, transcodingUrl: source.TranscodingUrl };
    }
    log(
      `Server offers ${entry.itemId} without transcoding at ${preset.label}, taking the original`
    );
    return { source };
  }

  async function runDownload(entry) {
    activeItemId = entry.itemId;
    entry.status = STATUS.DOWNLOADING;
    entry.progress = 0;
    entry.error = null;

    // The user can cancel while any of the awaits below is pending.
    const assertStillDownloading = () => {
      if (entry.status !== STATUS.DOWNLOADING) {
        throw new Error('Download cancelled');
      }
    };

    try {
      await persistAndBroadcast();
      // Set by startDownload/retryDownload; never read from the manifest.
      const headers = buildJellyfinHeaders(credentials[entry.itemId]);

      const directory = await ensureDirectory();
      const { source, transcodingUrl } = await negotiateSource(
        entry,
        credentials[entry.itemId],
        headers
      );
      assertStillDownloading();
      if (!source) {
        throw new Error('No media source available for this item');
      }

      entry.transcoded = Boolean(transcodingUrl);
      let mediaUrl;
      if (transcodingUrl) {
        // The server encodes while we download: no size is known up front.
        entry.container = containerFromTranscodingUrl(transcodingUrl);
        entry.expectedBytes = null;
        mediaUrl = `${entry.serverUrl}${transcodingUrl}`;
      } else {
        entry.container = pickContainer(source, entry.type);
        entry.expectedBytes = Number(source.Size) || null;
        const route = entry.type === 'Audio' ? 'Audio' : 'Videos';
        const mediaSourceId = source.Id || entry.itemId;
        mediaUrl = `${entry.serverUrl}/${route}/${entry.itemId}/stream?static=true&mediaSourceId=${encodeURIComponent(mediaSourceId)}`;
      }
      entry.fileBase = uniqueFileBaseName(entry);
      entry.mediaPath = `${directory}/${entry.fileBase}.${entry.container}`;
      entry.mediaAbsolutePath = utils.resolvePath(entry.mediaPath);
      await persistAndBroadcast();
      log(
        `Downloading ${entry.title} to ${entry.mediaAbsolutePath}${entry.transcoded ? ' (transcoded)' : ''}`
      );

      await transport.download(mediaUrl, entry.mediaPath, {
        headers,
        onProgress: (percent) => updateProgress(entry, percent),
      });
      assertStillDownloading();

      entry.subtitles = await downloadSubtitles(entry, source, headers);
      assertStillDownloading();

      entry.status = STATUS.COMPLETED;
      entry.progress = 100;
      entry.completedAt = Date.now();
      log(`Offline download completed: ${entry.title} (${entry.subtitles.length} subtitle(s))`);
      osd(`Downloaded for offline: ${entry.title}`);
    } catch (error) {
      if (entry.status === STATUS.CANCELLED) {
        log(`Offline download cancelled: ${entry.title}`);
        await deleteEntryFiles(entry);
        removeEntry(entry.itemId);
      } else {
        entry.status = STATUS.FAILED;
        entry.error = error.message || String(error);
        entry.progress = 0;
        log(`Offline download failed: ${entry.title}: ${entry.error}`);
        await deleteEntryFiles(entry);
        osd(`Download failed: ${entry.title}`);
      }
    } finally {
      delete credentials[entry.itemId];
      activeItemId = null;
      await persistAndBroadcast();
      processQueue();
    }
  }

  async function cancelDownload(itemId) {
    const entry = findEntry(itemId);
    if (!entry) {
      log(`Cannot cancel unknown download: ${itemId}`);
      return false;
    }
    if (entry.status === STATUS.QUEUED) {
      delete credentials[itemId];
      removeEntry(itemId);
      log(`Removed queued download: ${entry.title}`);
      await persistAndBroadcast();
      return true;
    }
    if (entry.status !== STATUS.DOWNLOADING) {
      log(`Download ${itemId} is ${entry.status}, nothing to cancel`);
      return false;
    }
    entry.status = STATUS.CANCELLED;
    broadcast();
    if (entry.mediaPath) {
      await transport.cancel(entry.mediaPath);
    }
    log(`Cancel requested for ${entry.title}`);
    return true;
  }

  async function removeDownload(itemId) {
    const entry = findEntry(itemId);
    if (!entry) {
      log(`Cannot remove unknown download: ${itemId}`);
      return false;
    }
    if (entry.status === STATUS.QUEUED || entry.status === STATUS.DOWNLOADING) {
      return cancelDownload(itemId);
    }
    await deleteEntryFiles(entry);
    removeEntry(itemId);
    log(`Removed offline download: ${entry.title}`);
    await persistAndBroadcast();
    return true;
  }

  async function retryDownload({ itemId, serverUrl, accessToken } = {}) {
    const entry = findEntry(itemId);
    if (!entry) {
      log(`Cannot retry unknown download: ${itemId}`);
      return false;
    }
    if (entry.status === STATUS.QUEUED || entry.status === STATUS.DOWNLOADING) {
      log(`Download ${itemId} is already ${entry.status}`);
      return false;
    }
    if (entry.status === STATUS.COMPLETED && fileExists(entry.mediaPath)) {
      log(`Download ${itemId} is complete, nothing to retry`);
      return false;
    }

    const sameServer =
      serverUrl && normalizeServerUrl(serverUrl) === entry.serverUrl ? accessToken : null;
    const token = sameServer || resolveStoredToken(entry.serverUrl);
    if (!token) {
      osd(`Cannot retry ${entry.title}: not connected to ${entry.serverUrl}`);
      return false;
    }

    await deleteEntryFiles(entry);
    credentials[itemId] = token;
    entry.status = STATUS.QUEUED;
    entry.progress = 0;
    entry.error = null;
    entry.subtitles = [];
    entry.completedAt = null;
    log(`Retrying offline download: ${entry.title}`);
    await persistAndBroadcast();
    processQueue();
    return true;
  }

  function playDownload(itemId) {
    const entry = findEntry(itemId);
    if (!entry || entry.status !== STATUS.COMPLETED) {
      log(`Cannot play download ${itemId}: ${entry ? entry.status : 'unknown'}`);
      osd('This download is not ready to play');
      return false;
    }
    if (!fileExists(entry.mediaPath)) {
      log(`Downloaded file is missing: ${entry.mediaPath}`);
      osd(`Downloaded file is missing: ${entry.title}`);
      broadcast();
      return false;
    }
    log(`Playing offline download: ${entry.title}`);
    openMedia({
      streamUrl: entry.mediaAbsolutePath || utils.resolvePath(entry.mediaPath),
      title: entry.title,
    });
    return true;
  }

  /**
   * Called for every file IINA loads. When the file is one of the downloads,
   * its subtitles are attached and the title set. Returns whether it matched.
   */
  function handleFileLoaded(fileUrl) {
    const loadedPath = normalizeLoadedPath(fileUrl);
    if (!loadedPath || /^https?:\/\//i.test(loadedPath)) {
      return false;
    }

    const entry = getEntries().find(
      (candidate) =>
        candidate.status === STATUS.COMPLETED &&
        candidate.mediaPath &&
        utils.resolvePath(candidate.mediaPath) === loadedPath
    );
    if (!entry) {
      return false;
    }

    log(`Loaded offline download: ${entry.title}`);
    try {
      mpv.set('force-media-title', entry.title);
    } catch (error) {
      log(`Could not set title: ${error.message}`);
    }

    let loaded = 0;
    for (const subtitle of entry.subtitles || []) {
      const path = subtitle.absolutePath || utils.resolvePath(subtitle.path);
      if (!fileExists(subtitle.path)) {
        log(`Offline subtitle missing: ${path}`);
        continue;
      }
      try {
        core.subtitle.loadTrack(path);
        loaded++;
      } catch (error) {
        log(`Could not load subtitle ${path}: ${error.message}`);
      }
    }

    if (loaded > 0 && preferences.get('show_notifications')) {
      osd(`Loaded ${loaded} offline subtitle(s)`);
    }
    return true;
  }

  function getPlaybackMode() {
    const mode = preferences.get('offline_playback');
    return Object.values(PLAYBACK_MODES).includes(mode) ? mode : PLAYBACK_MODES.LOCAL;
  }

  /**
   * The finished download of an item, when its file is still there.
   */
  function findLocalCopy(itemId) {
    const entry = itemId ? findEntry(itemId) : null;
    if (!entry || entry.status !== STATUS.COMPLETED || !fileExists(entry.mediaPath)) {
      return null;
    }
    return {
      itemId: entry.itemId,
      title: entry.title,
      path: entry.mediaAbsolutePath || utils.resolvePath(entry.mediaPath),
    };
  }

  function askToPlayLocally(question) {
    try {
      return Boolean(utils.ask(question));
    } catch (error) {
      log(`Could not ask about the offline copy: ${error.message}`);
      return true;
    }
  }

  /**
   * What to open for a playback request from the browser: the downloaded
   * copy when there is one and the preference allows it (asking first in
   * "ask" mode), otherwise the stream URL as requested.
   */
  function resolvePlaybackSource({ streamUrl, title, itemId } = {}) {
    const stream = { url: streamUrl, title, offline: false };
    const mode = getPlaybackMode();
    if (mode === PLAYBACK_MODES.STREAM) {
      return stream;
    }
    const local = findLocalCopy(itemId || itemIdFromStreamUrl(streamUrl));
    if (!local) {
      return stream;
    }
    if (
      mode === PLAYBACK_MODES.ASK &&
      !askToPlayLocally(
        `"${local.title}" is downloaded. Play the offline copy?\n\nCancel streams it from the server instead.`
      )
    ) {
      log(`Streaming ${local.title} although an offline copy exists (user choice)`);
      return stream;
    }
    log(`Playing the offline copy of ${local.title}: ${local.path}`);
    return { url: local.path, title: title || local.title, offline: true };
  }

  /**
   * Same for a list (an album): downloaded tracks are swapped in; in "ask"
   * mode one question covers the whole list.
   */
  function resolvePlaybackList(items) {
    const list = Array.isArray(items) ? items : [];
    const mode = getPlaybackMode();
    if (mode === PLAYBACK_MODES.STREAM) {
      return list;
    }
    const copies = list.map((item) =>
      item ? findLocalCopy(item.itemId || itemIdFromStreamUrl(item.streamUrl)) : null
    );
    const available = copies.filter(Boolean).length;
    if (available === 0) {
      return list;
    }
    if (
      mode === PLAYBACK_MODES.ASK &&
      !askToPlayLocally(
        `${available} of ${list.length} tracks are downloaded. Play the offline copies?\n\nCancel streams everything from the server.`
      )
    ) {
      log('Streaming the list although offline copies exist (user choice)');
      return list;
    }
    log(
      `Playing ${available} offline cop${available === 1 ? 'y' : 'ies'} in a list of ${list.length}`
    );
    return list.map((item, index) =>
      copies[index] ? { ...item, streamUrl: copies[index].path, offline: true } : item
    );
  }

  /**
   * For the next episode queued by autoplay: the local file when the episode
   * is downloaded (never asking mid-playback), otherwise the given URL.
   */
  function resolveAutoplayUrl(itemId, streamUrl) {
    if (getPlaybackMode() === PLAYBACK_MODES.STREAM) {
      return streamUrl;
    }
    const local = findLocalCopy(itemId);
    if (!local) {
      return streamUrl;
    }
    log(`Autoplay will use the offline copy of ${local.title}`);
    return local.path;
  }

  async function showDownloadsFolder() {
    try {
      const directory = await ensureDirectory();
      file.showInFinder(directory);
      return true;
    } catch (error) {
      log(`Could not open downloads folder: ${error.message}`);
      osd('Could not open the offline downloads folder');
      return false;
    }
  }

  function showInFinder(itemId) {
    const entry = findEntry(itemId);
    if (!entry || !fileExists(entry.mediaPath)) {
      log(`Cannot reveal download ${itemId}: file not available`);
      return false;
    }
    try {
      file.showInFinder(entry.mediaPath);
      return true;
    } catch (error) {
      log(`Could not reveal ${entry.mediaPath}: ${error.message}`);
      return false;
    }
  }

  /**
   * Wire the offline messages of a webview (sidebar or standalone window).
   */
  function registerMessageHandlers(view) {
    const handlers = {
      'get-offline-downloads': () => view.postMessage('offline-downloads', snapshot()),
      'offline-download': (data) => startDownload(data || {}),
      'offline-cancel': (data) => cancelDownload(data && data.itemId),
      'offline-remove': (data) => removeDownload(data && data.itemId),
      'offline-retry': (data) => retryDownload(data || {}),
      'play-offline': (data) => playDownload(data && data.itemId),
      'offline-show-in-finder': (data) => showInFinder(data && data.itemId),
      'offline-open-folder': () => showDownloadsFolder(),
      'offline-choose-folder': () => chooseDownloadFolder(),
      'offline-set-quality': (data) => setQuality(data && data.quality),
    };
    for (const name of OFFLINE_MESSAGES) {
      const handler = handlers[name];
      view.onMessage(name, (data) => {
        // An exception in IINA's message callback is only visible in its log;
        // report it and keep the UI informed instead.
        const report = (error) => {
          log(`Message ${name} failed: ${error.message}`);
          osd(`Offline downloads: ${error.message}`);
        };
        try {
          Promise.resolve(handler(data)).catch(report);
        } catch (error) {
          report(error);
        }
      });
    }
  }

  return {
    STATUS,
    getDirectory,
    isDownloadable,
    listDownloads,
    snapshot,
    startDownload,
    cancelDownload,
    removeDownload,
    retryDownload,
    playDownload,
    findLocalCopy,
    getPlaybackMode,
    resolvePlaybackSource,
    resolvePlaybackList,
    resolveAutoplayUrl,
    handleFileLoaded,
    showDownloadsFolder,
    showInFinder,
    chooseDownloadFolder,
    setQuality,
    getQualityPreset,
    registerMessageHandlers,
  };
}

module.exports = {
  createOfflineDownloadManager,
  isPluginLocalPath,
  itemIdFromStreamUrl,
  OFFLINE_MESSAGES,
  sanitizeFileName,
  toMessageData,
  PLAYBACK_MODES,
  MKDIR_BINARY,
  RM_BINARY,
  isTextSubtitle,
  buildDisplayTitle,
  pickContainer,
  normalizeLoadedPath,
  normalizeServerUrl,
  sanitizeId,
  STATUS,
  DOWNLOADABLE_TYPES,
};
