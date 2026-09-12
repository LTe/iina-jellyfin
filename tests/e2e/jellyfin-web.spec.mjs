import fs from 'node:fs';
import path from 'node:path';
import { test, expect } from './harness/fixtures.mjs';
import { PASSWORD, TOKEN, USER } from './harness/mock-jellyfin.mjs';

/**
 * The official Jellyfin Web client, vendored into the plugin, with the IINA
 * integration loaded in front of it: playback goes to IINA, downloads to
 * the plugin, and the state comes back into the page.
 */
test.describe('jellyfin web browser', () => {
  const offlineButton = (page) => page.locator('.btnIinaOffline:not(.hide)').first();

  async function openMovie(page) {
    await expect(page.locator('.cardText, .cardOverlayButton, .card').first()).toBeVisible();
    await page
      .locator('.card[data-id="movie-1"] .cardImageContainer, .card[data-id="movie-1"]')
      .first()
      .click();
    await expect(page).toHaveURL(/#\/details\?id=movie-1/);
    await expect(page.locator('.mainDetailButtons').first()).toBeVisible();
  }

  test('connects with saved credentials and hands the session to the plugin', async ({
    page,
    plugin,
  }) => {
    await plugin.open({ ui: 'web' });
    await expect(page).toHaveURL(/#\/home/);
    await expect(page.locator('body')).toContainText('Big Film');

    // The plugin learns the server from the web client (autoplay, reporting, downloads)
    await expect
      .poll(() => JSON.parse(plugin.host.prefs.get('jellyfin_servers') || '[]'))
      .toEqual([
        expect.objectContaining({
          accessToken: TOKEN,
          userId: USER.Id,
          serverUrl: expect.any(String),
        }),
      ]);
    // The web client identifies itself with the plugin's device id
    const identity = await page.evaluate(() => window.NativeShell.AppHost.deviceId());
    expect(identity).toBe(plugin.host.prefs.get('jellyfin_device_id') || identity);
  });

  test('plays an item in IINA instead of the page', async ({ page, plugin, jellyfin }) => {
    await plugin.open({ ui: 'web' });
    await openMovie(page);

    await page
      .locator('.mainDetailButtons .btnPlay:not(.hide), .mainDetailButtons .btnReplay:not(.hide)')
      .first()
      .click();

    await expect.poll(() => plugin.host.record.opened).toHaveLength(1);
    expect(plugin.host.record.opened[0]).toBe(
      `${jellyfin.baseUrl}/Videos/movie-1/stream?static=true&ApiKey=${TOKEN}`
    );
    expect(plugin.host.record.osd).toContain('Opening: Big Film');
    // No in-page playback started
    expect(await page.locator('video').count()).toBe(0);
  });

  test('downloads from the item page and plays the copy from the Downloads panel', async ({
    page,
    plugin,
  }) => {
    await plugin.open({ ui: 'web' });
    await openMovie(page);

    await expect(offlineButton(page)).toHaveAttribute('data-offline-state', 'idle');
    await offlineButton(page).click();
    await expect(offlineButton(page)).toHaveAttribute('data-offline-state', 'completed', {
      timeout: 30000,
    });
    const mediaPath = path.join(plugin.dataDir, 'offline/Big Film (2020).mkv');
    expect(fs.existsSync(mediaPath)).toBe(true);
    expect(plugin.host.manifest()[0]).toMatchObject({ itemId: 'movie-1', status: 'completed' });

    // The user menu's Downloads entry opens the panel
    await page.getByRole('button', { name: 'User Menu' }).click();
    await page.getByRole('menuitem', { name: 'Downloads' }).click();
    const panel = page.locator('.iina-downloads:not(.hide)');
    await expect(panel).toBeVisible();
    await expect(panel.locator('.iina-downloads-item[data-download-id="movie-1"]')).toContainText(
      'Ready to play offline'
    );
    await panel.locator('[data-action="play"][data-item-id="movie-1"]').click();
    await expect.poll(() => plugin.host.record.opened).toEqual([mediaPath]);

    // The offline button plays the copy too, and the item page Play uses it as well
    await panel.locator('.iina-downloads-close').click();
    await expect(panel).toBeHidden();
    await offlineButton(page).click();
    await expect.poll(() => plugin.host.record.opened).toEqual([mediaPath, mediaPath]);
  });

  test('signs in through the web client', async ({ page, plugin, jellyfin }) => {
    await plugin.open({ ui: 'web', signedIn: false });
    // The client is served by the Jellyfin server itself, so it already
    // knows the server and goes straight to the login page.
    await expect(page).toHaveURL(/#\/login/);
    await page.locator('.btnManual').click();
    await page.locator('#txtManualName').fill(USER.Name);
    await page.locator('#txtManualPassword').fill(PASSWORD);
    await page.locator('#txtManualPassword').press('Enter');

    await expect(page).toHaveURL(/#\/home/);
    expect(jellyfin.state.logins).toEqual([expect.objectContaining({ Username: USER.Name })]);
    await expect
      .poll(() => JSON.parse(plugin.host.prefs.get('jellyfin_servers') || '[]'))
      .toEqual([expect.objectContaining({ accessToken: TOKEN, username: USER.Name })]);
  });
});
