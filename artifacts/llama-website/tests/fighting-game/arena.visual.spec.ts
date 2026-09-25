import { expect, test, type Locator, type Page } from '@playwright/test';
import { FIGHTER_VISUAL_STATES } from '../../src/games/fighting-game/visualFixtures';

const routeFor = (state: string) =>
  `/llama-website/fighting-game?visualState=${state}`;

async function expectNoOverlap(a: Locator, b: Locator) {
  const [aBox, bBox] = await Promise.all([a.boundingBox(), b.boundingBox()]);
  expect(aBox).not.toBeNull();
  expect(bBox).not.toBeNull();
  const overlaps = !(
    aBox!.x + aBox!.width <= bBox!.x
    || bBox!.x + bBox!.width <= aBox!.x
    || aBox!.y + aBox!.height <= bBox!.y
    || bBox!.y + bBox!.height <= aBox!.y
  );
  expect(overlaps).toBe(false);
}

async function assertHud(page: Page) {
  const healthP1 = page.getByTestId('health-p1');
  const healthP2 = page.getByTestId('health-p2');
  const timer = page.getByTestId('text-timer');
  const nameP1 = page.getByTestId('name-p1');
  const nameP2 = page.getByTestId('name-p2');
  const hud = [
    healthP1,
    healthP2,
    timer,
    nameP1,
    nameP2,
    page.getByTestId('game-canvas'),
  ];
  await Promise.all(hud.map((item) => expect(item).toBeVisible()));
  await expectNoOverlap(nameP1, timer);
  await expectNoOverlap(nameP2, timer);
  await expectNoOverlap(healthP1, timer);
  await expectNoOverlap(healthP2, timer);
  await expectNoOverlap(healthP1, healthP2);
}

test.describe('isolated Layer 2 fighting arena', () => {
  for (const state of FIGHTER_VISUAL_STATES) {
    test(`${state} visual baseline`, async ({ page }, testInfo) => {
      await page.goto(routeFor(state));
      await assertHud(page);

      await expect(page.getByTestId('card-profile')).toHaveCount(0);
      await expect(page.getByTestId('section-tickets')).toHaveCount(0);
      await expect(page.locator('canvas')).toHaveCount(1);

      if (testInfo.project.name === 'phone') {
        const controls = [
          page.getByTestId('touch-dpad'),
          page.getByTestId('touch-heavy_kick'),
          page.getByTestId('touch-light_punch'),
          page.getByTestId('touch-block'),
        ];
        await Promise.all(controls.map((item) => expect(item).toBeVisible()));
        await expectNoOverlap(page.getByTestId('touch-dpad'), page.getByTestId('touch-heavy_kick'));
        await expectNoOverlap(page.getByTestId('touch-dpad'), page.getByTestId('touch-light_punch'));
        await expectNoOverlap(page.getByTestId('touch-dpad'), page.getByTestId('touch-block'));
        await expectNoOverlap(page.getByTestId('touch-heavy_kick'), page.getByTestId('touch-light_punch'));
        await expectNoOverlap(page.getByTestId('touch-heavy_kick'), page.getByTestId('touch-block'));
        await expectNoOverlap(page.getByTestId('touch-light_punch'), page.getByTestId('touch-block'));
      } else {
        await expect(page.getByTestId('touch-dpad')).toBeHidden();
      }

      if (state === 'result') {
        await expect(page.getByRole('heading', { name: 'DEFEAT' })).toBeVisible();
        await expect(page.getByTestId('btn-rematch')).toBeVisible();
      }

      await expect(page).toHaveScreenshot(`${state}.png`, {
        fullPage: true,
      });
    });
  }
});