import { test as base, expect } from '@playwright/test';

const test = base.extend({
  page: async ({ page }, runTest) => {
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('console', (message) => {
      if (message.type() === 'error' && !message.text().includes('Error creating WebGL context') && !message.text().startsWith('Failed to load resource:')) errors.push(message.text());
    });
    await runTest(page);
    expect(errors).toEqual([]);
  },
});

async function setup(page, { noGPU = false, fleetAvailable = true } = {}) {
  await page.addInitScript(({ noGPU }) => {
    if (noGPU) {
      const original = HTMLCanvasElement.prototype.getContext;
      HTMLCanvasElement.prototype.getContext = function (type, ...args) {
        return type.startsWith('webgl') ? null : original.call(this, type, ...args);
      };
    }
    window.testStreams = [];
    window.EventSource = class {
      constructor(url) { this.url = url; this.handlers = {}; this.closed = false; this.sequence = 0; window.testStreams.push(this); }
      addEventListener(type, fn) { this.handlers[type] = fn; }
      close() { this.closed = true; }
      emit(type, payload, stage) { this.handlers[type]?.({ data: JSON.stringify({ ts: ++this.sequence, payload, stage }) }); }
    };
  }, { noGPU });
  await page.route('**/health', (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.route('**/fleet', (route) => route.fulfill({ status: fleetAvailable ? 200 : 503, json: { nodes: [], apps: [], events: [] } }));
  await page.route('**/projects', (route) => route.fulfill({ json: { projects: [] } }));
  await page.route('**/deploy', (route) => route.fulfill({ json: { deployment_id: 'test-run' } }));
  await page.route('**/deploy/**', (route) => route.fulfill({ json: { accepted: true } }));
  await page.goto('/?fleetDemo=200');
  if (noGPU) await expect(page.getByText('3D 화면을 사용할 수 없습니다.', { exact: false })).toBeVisible();
  else await expect(page.getByRole('img', { name: /배포 여정: 소스/ })).toBeVisible();
}

async function start(page) {
  await page.getByRole('button', { name: '새 배포', exact: true }).click();
  await page.getByRole('button', { name: /One Action Deploy/ }).click();
  await expect(page.getByRole('dialog')).toBeHidden();
}

async function emit(page, type, payload = {}, stage) {
  await page.evaluate(({ type, payload, stage }) => window.testStreams.at(-1).emit(type, payload, stage), { type, payload, stage });
}

for (const [width, height] of [[1920, 1080], [1366, 768], [375, 667], [375, 320]]) {
  test(`one screen at ${width}×${height}`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await setup(page);
    const dimensions = await page.evaluate(() => ({ height: document.documentElement.scrollHeight, width: document.documentElement.scrollWidth, viewportHeight: innerHeight, viewportWidth: innerWidth }));
    expect(dimensions.height).toBeLessThanOrEqual(dimensions.viewportHeight);
    expect(dimensions.width).toBeLessThanOrEqual(dimensions.viewportWidth);
    const view = await page.locator('#scene-view').boundingBox();
    expect(view.height).toBeGreaterThan(80);
    await expect(page.getByRole('button', { name: '새 배포', exact: true })).toBeInViewport();
    await expect(page.getByRole('button', { name: '전체 로그 보기' })).toBeInViewport();
    await page.screenshot({ path: test.info().outputPath('journey.png') });
    await page.getByRole('tab', { name: '노드 풀' }).click();
    await expect(page.getByLabel('노드 200개 3D 상태 그리드')).toBeVisible();
    await expect(page.getByText('데모 200개 포함 · 드래그로 회전')).toBeVisible();
    // The canvas wrapper appears before Three.js draws its first frame.
    await page.waitForTimeout(500);
    await page.screenshot({ path: test.info().outputPath('fleet.png') });
  });
}

test('drawer preserves drafts, traps focus and restores its trigger', async ({ page }) => {
  await setup(page);
  const trigger = page.getByRole('button', { name: '새 배포', exact: true });
  await trigger.click();
  const dialog = page.getByRole('dialog');
  const source = dialog.getByPlaceholder('https://github.com/owner/repo');
  await source.fill('review/source-draft');
  await page.keyboard.press('Escape');
  await expect(trigger).toBeFocused();
  await trigger.click();
  await expect(source).toHaveValue('review/source-draft');
  await page.keyboard.press('Shift+Tab');
  await expect(dialog.getByRole('button', { name: /One Action Deploy/ })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(dialog.getByRole('button', { name: '상세 패널 닫기' })).toBeFocused();
});

test('input opens automatically and survives panel and scene changes with one SSE stream', async ({ page }) => {
  await setup(page);
  await start(page);
  await page.getByRole('tab', { name: '노드 풀' }).click();
  await emit(page, 'input_required', { items: [{ name: 'DB_HOST', scope: 'runtime', evidence: [], secret: false }], timeout_sec: 300 });
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  const input = dialog.locator('input:visible').first();
  await input.fill('test.example');
  await page.keyboard.press('Escape');
  await page.getByRole('tab', { name: '배포 여정' }).click();
  await page.getByRole('button', { name: '입력 필요' }).click();
  await expect(input).toHaveValue('test.example');
  await dialog.getByRole('button', { name: '값 넣고 배포 계속' }).click();
  await emit(page, 'log', { line: 'env: values received' });
  await expect(page.getByText('배포에 필요한 값', { exact: true })).toBeHidden();
  await expect.poll(() => page.evaluate(() => window.testStreams.length)).toBe(1);
});

test('healing question, full stage celebration, drawer-first Escape and return to fleet', async ({ page }) => {
  await setup(page);
  await start(page);
  await emit(page, 'stage', { target: 'local' }, 'deploy');
  await emit(page, 'log', { kind: 'question', question_id: 'q1', ask: true, rationale: 'review fix', diff: '+fixed', timeout_s: 300 });
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('button', { name: '고친 채로 계속' })).toBeVisible();
  await dialog.getByRole('button', { name: '고친 채로 계속' }).click();
  await emit(page, 'log', { kind: 'answer', choice: 'continue', reason: 'user' });
  await page.keyboard.press('Escape');
  await page.getByRole('tab', { name: '노드 풀' }).click();
  await emit(page, 'heal_diff', { attempt: 1, category: 'deps', rationale: 'review fix' });
  await emit(page, 'done', { target: 'local', url: 'https://local.example', records: [{}] });
  await emit(page, 'done', { target: 'cloudrun', url: 'https://cloud.example' });
  const victory = page.getByRole('region', { name: 'AI 자가치유 성공 축하' });
  await expect(victory).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('victory.png') });
  await expect(page.getByRole('link', { name: 'Local Docker 열기' })).toBeVisible();
  await page.getByRole('button', { name: '전체 로그 보기' }).click();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(victory).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(victory).toBeHidden();
  await expect(page.getByLabel('노드 200개 3D 상태 그리드')).toBeVisible();
});

test('WebGL failure preserves controls, success links and celebration return', async ({ page }) => {
  await setup(page, { noGPU: true });
  await expect(page.getByText('3D 화면을 사용할 수 없습니다.', { exact: false })).toBeVisible();
  await start(page);
  await emit(page, 'heal_diff', { attempt: 1, category: 'deps' });
  await emit(page, 'done', { target: 'local', url: 'https://local.example', records: [{}] });
  await emit(page, 'done', { target: 'cloudrun', url: 'https://cloud.example' });
  await expect(page.getByRole('button', { name: '돌아가기' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Local Docker 열기' })).toBeVisible();
  await page.getByRole('button', { name: '돌아가기' }).click();
  await expect(page.getByRole('button', { name: '새 배포', exact: true })).toBeVisible();
});

test('unavailable fleet gives an accessible empty state', async ({ page }) => {
  await setup(page, { fleetAvailable: false });
  await page.getByRole('tab', { name: '노드 풀' }).click();
  await expect(page.getByText('노드 풀 데이터를 불러오지 못했습니다.', { exact: false }).first()).toBeVisible();
});

test('short mobile sheet scrolls internally and keeps partial results reachable', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 320 });
  await setup(page);
  await start(page);
  await emit(page, 'input_required', { items: [{ name: 'DB_HOST', scope: 'runtime', evidence: [], secret: false }], timeout_sec: 300 });
  const dialog = page.getByRole('dialog');
  await dialog.locator('input:visible').fill('mobile.example');
  await dialog.getByRole('button', { name: '값 넣고 배포 계속' }).click();
  await emit(page, 'log', { line: 'env: values received' });
  await page.keyboard.press('Escape');
  await emit(page, 'done', { target: 'local', url: 'https://local.example' });
  await emit(page, 'error', { target: 'cloudrun', message: 'cloud unavailable' });
  await expect(page.getByText('일부 타깃 실패', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Local Docker 열기' })).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBe(320);
  await expect(page.getByRole('button', { name: '돌아가기' })).toBeHidden();
});

test('fullscreen rejection keeps the viewport dashboard usable', async ({ page }) => {
  await page.addInitScript(() => { Element.prototype.requestFullscreen = () => Promise.reject(new Error('unavailable')); });
  await setup(page);
  await page.getByRole('button', { name: '전체화면', exact: true }).click();
  await expect(page.getByText('전체화면을 열지 못했습니다.', { exact: false })).toBeVisible();
  await start(page);
});

test('opening logs follows the latest events without scrolling the drawer or page', async ({ page }) => {
  await setup(page);
  await start(page);
  const append = () => page.evaluate(() => {
    for (let i = 0; i < 40; i++) window.testStreams.at(-1).emit('stage', {}, 'analyze');
  });
  await append();
  await page.getByRole('button', { name: '전체 로그 보기' }).click();
  const box = page.getByRole('dialog').locator('.overflow-y-auto');
  const gap = () => box.evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight);
  await expect.poll(gap).toBeLessThan(40);
  await page.locator('.drawer-body').evaluate((el) => { el.scrollTop = 0; });
  await append();
  await expect.poll(gap).toBeLessThan(40);
  expect(await page.locator('.drawer-body').evaluate((el) => el.scrollTop)).toBe(0);
  await page.keyboard.press('Escape');
  await append();
  await page.getByRole('button', { name: '전체 로그 보기' }).click();
  await expect.poll(gap).toBeLessThan(40);
  expect(await page.evaluate(() => scrollY)).toBe(0);
});
