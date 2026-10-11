// Thin client for the CloudMorph backend. Same-origin: the Vite dev server (and the demo tunnel)
// proxy /deploy, /health, /fleet, /projects and /webhook to FastAPI, so no base URL and no CORS.
const TOKEN_KEY = 'cloudmorph.apiToken'

export const getToken = () => localStorage.getItem(TOKEN_KEY) || ''
export const setToken = (t) => {
  const v = (t || '').trim() // pasted tokens often carry a trailing newline
  return v ? localStorage.setItem(TOKEN_KEY, v) : localStorage.removeItem(TOKEN_KEY)
}

const headers = () => ({
  'Content-Type': 'application/json',
  ...(getToken() ? { 'X-API-Token': getToken() } : {}),
})

// --- ID/password accounts: a login returns a token that is used exactly like an API token ---------------
const AUTH_ERRORS = {
  401: '아이디 또는 비밀번호가 맞지 않습니다',
  403: '가입 코드가 맞지 않습니다',
  409: '이미 사용 중인 아이디입니다',
  429: '로그인을 너무 많이 실패했습니다. 몇 분 뒤에 다시 시도하세요',
}

async function authPost(path, body) {
  const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : '입력값을 확인하세요'
    throw new Error(AUTH_ERRORS[res.status] || (res.status === 422 ? detail : `요청 실패 (${res.status})`))
  }
  setToken(data.token)
  return data
}

export const signUp = ({ username, password, code }) => authPost('/auth/signup', { username, password, ...(code ? { code } : {}) })
export const logIn = ({ username, password }) => authPost('/auth/login', { username, password })

export async function logOut() {
  await fetch('/auth/logout', { method: 'POST', headers: headers() }).catch(() => {})
  setToken('')
}

/** { signup_code: bool }: whether sign-up asks for an invite code. */
export async function getAuthConfig() {
  const res = await fetch('/auth/config').catch(() => null)
  return res?.ok ? res.json() : { signup_code: false }
}

async function post(path, body) {
  const res = await fetch(path, { method: 'POST', headers: headers(), body: JSON.stringify(body || {}) })
  if (!res.ok) {
    const text = await res.text()
    let detail = text
    try { detail = JSON.parse(text).detail || text } catch { /* not JSON */ }
    throw new Error(typeof detail === 'string' ? detail.slice(0, 300) : `${res.status}`)
  }
  return res.json()
}

/** What the incident guard sees and did: { guard, health, incidents }. */
export async function getIncidents() {
  const res = await fetch('/incidents', { headers: headers() })
  if (!res.ok) return null
  return res.json()
}

/** Disaster drill: kind = crash | db_down | outage. */
export const drill = (name, kind) => post(`/projects/${encodeURIComponent(name)}/drill`, { kind })
/** Drag & drop: { kind: postgres|mysql|redis, mode: new|url|none, url? } -> { deployment_id, warning } */
export const setResource = (name, body) => post(`/projects/${encodeURIComponent(name)}/resources`, body)
/** Run the app somewhere else, then take the old place down. */
export const moveProject = (name, to) => post(`/projects/${encodeURIComponent(name)}/move`, { to })
/** Answer input_required with values and, when asked, where to deploy. */
export const submitAnswers = (id, values, targets) => post(`/deploy/${id}/env`, targets ? { values, targets } : { values })

export async function startDeploy({ source, targets, name, ref }) {
  // ask_env: pause after analysis and ask for the values the code needs (EnvRequestCard answers)
  const body = { source, targets, ask_env: true, ...(name ? { name } : {}), ...(ref ? { ref } : {}) }
  const res = await fetch('/deploy', { method: 'POST', headers: headers(), body: JSON.stringify(body) })
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 300)}`)
  return (await res.json()).deployment_id
}

/** Answer the healer's question (asked before any PR): 'continue' (PR + deploy), 'pr_only' or 'stop'. */
export async function answerQuestion(id, questionId, choice) {
  const res = await fetch(`/deploy/${id}/answer`, { method: 'POST', headers: headers(), body: JSON.stringify({ question_id: questionId, choice }) })
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 300)}`)
}

/** Answer an `input_required` event. Values only travel in this request; the backend never echoes them. */
export async function submitEnv(id, values) {
  const res = await fetch(`/deploy/${id}/env`, { method: 'POST', headers: headers(), body: JSON.stringify({ values }) })
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 300)}`)
  return res.json()
}

export async function getDeployment(id) {
  const res = await fetch(`/deploy/${id}`, { headers: headers() })
  if (!res.ok) throw new Error(`${res.status}`)
  return res.json()
}

export async function getHealth() {
  try {
    const res = await fetch('/health')
    return res.ok
  } catch {
    return false
  }
}

export async function getFleet() {
  const res = await fetch('/fleet', { headers: headers() })
  if (!res.ok) return null // backend without the node pool
  return res.json()
}

/** Who the current token belongs to: { user, admin } or null (no/invalid token). */
export async function getMe() {
  const res = await fetch('/projects/me', { headers: headers() })
  return res.ok ? res.json() : null
}

/** Whether `token` maps to a user, checked before it replaces the stored one. */
export async function checkToken(token) {
  const res = await fetch('/projects/me', { headers: { 'X-API-Token': token } }).catch(() => null)
  if (!res) throw new Error('백엔드에 연결할 수 없습니다')
  return res.ok
}

async function postJson(path, body) {
  const res = await fetch(path, { method: 'POST', headers: headers(), body: JSON.stringify(body) })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || `${res.status}`)
  return data
}

/** Connect push-to-deploy. githubToken (optional) registers the webhook on GitHub; it is not stored. */
export const linkCd = (name, { githubToken, branch } = {}) =>
  postJson(`/projects/${encodeURIComponent(name)}/cd`, { github_token: githubToken || null, branch: branch || null })

export const unlinkCd = (name, { githubToken } = {}) =>
  postJson(`/projects/${encodeURIComponent(name)}/cd/unlink`, { github_token: githubToken || null })

/** The caller's on-premises agents: [{ name, online, busy, info }]. */
export async function getAgents() {
  const res = await fetch('/agent/status', { headers: headers() })
  return res.ok ? (await res.json()).agents : []
}

export async function getProjects() {
  const res = await fetch('/projects', { headers: headers() })
  if (!res.ok) return null
  return (await res.json()).projects
}

/** Stop a running deployment at its next step (an in-flight docker build is killed right away). */
export async function cancelDeploy(id) {
  const res = await fetch(`/deploy/${id}/cancel`, { method: 'POST', headers: headers() })
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 300)}`)
  return res.json()
}

/** Take down everything the app is serving (local container + tunnel, Cloud Run service, node replicas). */
export async function stopProject(name) {
  const res = await fetch(`/projects/${encodeURIComponent(name)}/stop`, { method: 'POST', headers: headers() })
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 300)}`)
  return res.json()
}

/** Subscribe to the SSE stream. `onEvent(type, payload, raw)`; returns a close() function. */
export function subscribe(id, onEvent, onFail) {
  const q = getToken() ? `?token=${encodeURIComponent(getToken())}` : ''
  const es = new EventSource(`/deploy/${id}/events${q}`)
  for (const type of ['stage', 'log', 'heal_diff', 'input_required', 'done', 'error']) {
    es.addEventListener(type, (ev) => {
      // Native connection errors share the name "error" but carry no SSE payload.
      if (!ev.data) return
      try {
        const raw = JSON.parse(ev.data)
        onEvent(type, raw.payload || {}, raw)
      } catch (e) {
        onEvent('log', { line: `[client] bad event: ${e}` }, null)
      }
    })
  }
  es.onerror = (ev) => !ev.data && onFail && onFail()
  return () => es.close()
}

// --- admin console (#/admin). Apps come from getProjects()/stopProject(): admin already sees every app. ---
/** Everyone who can own apps: [{ name, kind: admin|account|token|owner, apps, live, sessions, created_at, last_login }]. */
export async function getAdminUsers() {
  const res = await fetch('/admin/users', { headers: headers() })
  if (!res.ok) throw new Error(`${res.status}`)
  return (await res.json()).users
}

const adminUser = (name, action) => `/admin/users/${encodeURIComponent(name)}/${action}`
/** New password for an account; its current logins end. */
export const resetUserPassword = (name, password) => postJson(adminUser(name, 'password'), { password })
/** Log the account out everywhere -> { ended }. */
export const logOutUser = (name) => postJson(adminUser(name, 'logout'), {})
/** Remove the account. Its apps stay (and keep the id reserved). */
export const deleteUser = (name) => postJson(adminUser(name, 'delete'), {})
