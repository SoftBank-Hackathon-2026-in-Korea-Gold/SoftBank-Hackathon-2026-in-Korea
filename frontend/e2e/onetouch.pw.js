import { expect, test } from '@playwright/test';
import { RUN_ID } from './fixtures/ops.js';

// The one-touch screens ("#/deploy", "#/apps", "#/incidents"): same mocks as the dashboard suites (routes + EventSource stub).
const path = (name) => (url) => url.pathname === name;
const GB = {
  name: 'gb', source: 'sample-apps/guestbook', ref: null, targets: ['local'], last_deployment_id: 'dep-gb',
  last_status: 'completed', trigger: 'api', updated_at: Date.now() / 1000 - 60, history: ['dep-gb'],
  urls: { local: 'http://127.0.0.1:61009' },
  live: { local: { container_name: 'gb-local-1', local_url: 'http://127.0.0.1:61009' } },
  analysis: { target: 'local', language: 'python', framework: 'flask', port: 8080, service_models: ['iaas'], stores: { postgres: 'DATABASE_URL' }, notes: [] },
};
const DECISION = {
  kind: 'decision', service: 'serverless_container', target: 'cloudrun', label: 'Cloud Run (서버리스 컨테이너)',
  explain: '요청이 올 때만 켜지는 서버예요.', probabilities: { serverless_container: 0.74, vm: 0.21, static_site: 0.0, serverless_function: 0.05 },
  confidence: 0.4, ms: 532, prep_ms: 2, engine: 'clef-flash (로컬)', tokens: 254, why: ['서버 코드: app.py:11', 'SQLite: app.py:6'],
};

async function setup(page) {
  const sent = { deploys: [], answers: [], resources: [], drills: [] };
  await page.addInitScript(() => {
    window.testStreams = [];
    window.EventSource = class {
      constructor(url) { this.url = url; this.handlers = {}; this.sequence = 0; window.testStreams.push(this); }
      addEventListener(type, fn) { this.handlers[type] = fn; }
      close() { this.closed = true; }
      emit(type, payload, stage) { this.handlers[type]?.({ data: JSON.stringify({ ts: ++this.sequence, payload, stage }) }); }
    };
  });
  await page.route(path('/health'), (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.route(path('/projects/me'), (route) => route.fulfill({ json: { user: 'admin', admin: true } }));
  await page.route(path('/projects'), (route) => route.fulfill({ json: { projects: [GB] } }));
  await page.route(path('/incidents'), (route) => route.fulfill({ json: {
    guard: { running: true, interval: 3, engine: 'Jev', jev: true },
    health: { gb: { local: { ok: true, http: 200, ms: 12, container: 'running' } } },
    incidents: [{ id: 'i1', app: 'gb', target: 'local', opened: 1000, status: 'resolved', drill: 'crash', resolved: 1002.8, probe: {}, actions: [],
      verdicts: [{ engine: 'Jev', ms: 410, tokens: 600, cause: 'app_crashed', cause_p: 0.92, action: 'restart', action_p: 0.91, severity_label: '서비스 중단' }],
      timeline: [{ at: 1000, kind: 'alert', text: 'gb 앱(내 컴퓨터)에 문제가 생겼어요: 앱 컨테이너가 꺼졌어요' }, { at: 1002.8, kind: 'resolved', text: '다시 정상이에요. 문제를 알아챈 뒤 2.8초 만에 돌아왔어요.' }] }],
  } }));
  await page.route(path('/deploy'), (route) => { sent.deploys.push(route.request().postDataJSON()); return route.fulfill({ json: { deployment_id: RUN_ID } }); });
  await page.route(path(`/deploy/${RUN_ID}/env`), (route) => { sent.answers.push(route.request().postDataJSON()); return route.fulfill({ json: { accepted: [], targets: ['cloudrun'] } }); });
  await page.route(path(`/deploy/${RUN_ID}`), (route) => route.fulfill({ json: { deployment_id: RUN_ID, status: 'completed', targets: {}, completed: true } }));
  await page.route(path('/projects/gb/resources'), (route) => { sent.resources.push(route.request().postDataJSON()); return route.fulfill({ json: { name: 'gb', deployment_id: RUN_ID, warning: null } }); });
  await page.route(path('/projects/gb/drill'), (route) => { sent.drills.push(route.request().postDataJSON()); return route.fulfill({ json: { app: 'gb', target: 'local', kind: 'crash' } }); });
  return sent;
}

const emit = (page, type, payload = {}, stage) =>
  page.evaluate(({ type, payload, stage }) => window.testStreams.at(-1).emit(type, payload, stage), { type, payload, stage });

test('healed source diff stays reviewable on the one-touch page', async ({ page }) => {
  await setup(page);
  await page.goto('/#/deploy');
  await page.getByRole('button', { name: '배포하기', exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.testStreams.length)).toBe(1);
  await emit(page, 'stage', { target: 'local' }, 'deploy');
  await emit(page, 'heal_diff', {
    attempt: 1, category: 'missing_dependency', source: 'rule', rationale: 'flask 설치',
    diff: '--- Dockerfile (before)\n+++ Dockerfile (after)\n@@ -1 +1 @@\n-RUN pip install gunicorn\n+RUN pip install gunicorn flask',
  });
  await expect(page.getByText('자가치유 패치', { exact: true })).toBeVisible();
  await expect(page.getByText('#1 missing_dependency', { exact: true })).toBeVisible();
  await expect(page.getByText('규칙', { exact: true })).toBeVisible();
  await expect(page.getByText('Dockerfile', { exact: true })).toBeVisible();
  await expect(page.getByText('− 수정 전 · + 수정 후', { exact: true })).toBeVisible();
  await expect(page.locator('[data-diff-kind="removed"]')).toHaveText('-RUN pip install gunicorn');
  await expect(page.locator('[data-diff-kind="added"]')).toHaveText('+RUN pip install gunicorn flask');
  const background = await page.locator('[data-diff-kind="added"]').evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(background).not.toBe('rgba(0, 0, 0, 0)');
  await emit(page, 'done', { target: 'local', url: 'https://healed.example' });
  await expect(page.getByText('자가치유 패치', { exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('healed-diff.png'), fullPage: true });
});

test('front door: one click, a 1-second pick, a question only for the risky choice, then done', async ({ page }) => {
  const sent = await setup(page);
  await page.goto('/#/deploy');
  await expect(page.getByRole('heading', { name: '코드만 있으면 바로 배포해 드릴게요' })).toBeVisible();
  await page.getByRole('button', { name: '배포하기', exact: true }).click();
  await expect.poll(() => sent.deploys.length).toBe(1);
  expect(sent.deploys[0]).toMatchObject({ source: 'sample-apps/guestbook', targets: [], ask_env: true });
  // the page subscribes once the POST returns; events sent before that would have nowhere to go
  await expect.poll(() => page.evaluate(() => window.testStreams.length)).toBe(1);

  await emit(page, 'stage', {}, 'analyze');
  await emit(page, 'log', DECISION, 'analyze');
  await expect(page.getByText('코드를 읽고 0.53초 만에 정했어요')).toBeVisible();
  await emit(page, 'log', { line: "analyzer: target=local language=python framework=flask port=8080 service_models=['iaas']" }, 'analyze');
  await emit(page, 'input_required', { items: [], timeout_sec: 900, choose_target: {
    recommended: 'local', options: ['local', 'cloudrun'], decision: DECISION, held_back: true,
    risks: ['동의를 받지 못해 위험으로 두었습니다: SQLite — SQLite 파일은 컨테이너를 다시 만들 때 사라집니다'],
  } }, 'analyze');
  await expect(page.getByRole('heading', { name: '배포 전에 몇 가지만 여쭤볼게요' })).toBeVisible();
  await expect(page.getByText('AI는 Cloud Run을 추천했어요')).toBeVisible();
  await page.getByRole('radio', { name: /클라우드/ }).click();
  await page.getByRole('button', { name: '위험을 알고 이대로 배포하기' }).click();
  await expect.poll(() => sent.answers.length).toBe(1);
  expect(sent.answers[0]).toEqual({ values: {}, targets: ['cloudrun'] });

  await emit(page, 'stage', { target: 'cloudrun' }, 'deploy');
  await emit(page, 'done', { target: 'cloudrun', url: 'https://gb-abc.a.run.app' });
  await expect(page.getByRole('heading', { name: '배포를 마쳤어요' })).toBeVisible();
  await expect(page.getByRole('link', { name: /열어 보기/ })).toHaveAttribute('href', 'https://gb-abc.a.run.app');
});

test('server map: drop PostgreSQL on the app to connect it', async ({ page }) => {
  const sent = await setup(page);
  await page.goto('/#/apps');
  await expect(page.getByText('어디에서 무엇이 돌아가고 있나요?')).toBeVisible();
  await expect(page.getByText('잘 돌아가요 · 응답 12ms')).toBeVisible();
  await page.getByRole('button', { name: /PostgreSQL 새로 만들기/ }).dragTo(page.getByText('DB 없음'));
  const dialog = page.getByRole('dialog', { name: 'PostgreSQL을 새로 만들어 연결할까요?' });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: '연결하기' }).click();
  await expect.poll(() => sent.resources.length).toBe(1);
  expect(sent.resources[0]).toEqual({ kind: 'postgres', mode: 'new' });
});

test('incidents: the timeline and a drill', async ({ page }) => {
  const sent = await setup(page);
  await page.goto('/#/incidents');
  await expect(page.getByText('3초마다 앱을 지켜보고 있어요')).toBeVisible();
  await expect(page.getByText('다시 정상이에요. 문제를 알아챈 뒤 2.8초 만에 돌아왔어요.')).toBeVisible();
  await page.getByRole('button', { name: '앱 강제로 끄기' }).click();
  await expect.poll(() => sent.drills.length).toBe(1);
  expect(sent.drills[0]).toEqual({ kind: 'crash' });
});

test('the 3D dashboard is the front door and the one-touch screens are one link away', async ({ page }) => {
  await setup(page);
  await page.goto('/');
  await expect(page.getByRole('tab', { name: '배포 여정' })).toBeVisible();
  await page.getByRole('link', { name: '원터치' }).click();
  await expect(page.getByRole('heading', { name: '코드만 있으면 바로 배포해 드릴게요' })).toBeVisible();
  await page.getByRole('link', { name: '3D 대시보드' }).click();
  await expect(page.getByRole('tab', { name: '배포 여정' })).toBeVisible();
});
