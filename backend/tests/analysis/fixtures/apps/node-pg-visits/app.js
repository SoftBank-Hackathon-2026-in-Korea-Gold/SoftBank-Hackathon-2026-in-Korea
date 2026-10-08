const express = require('express');
const { Pool } = require('pg');

const pool = new Pool({ connectionString: process.env.DATABASE_URL });
const greeting = process.env.SESSION_SECRET ? 'secret loaded' : 'no secret';
const app = express();

app.get('/health', async (req, res) => {
  try {
    await pool.query('SELECT 1');
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.get('/', async (req, res) => {
  const email = req.query.email || `guest${Math.floor(Math.random() * 10000)}@example.com`;
  await pool.query('INSERT INTO visits (email) VALUES ($1)', [email]);
  const { rows } = await pool.query('SELECT count(*)::int AS n FROM visits');
  const recent = await pool.query('SELECT email FROM visits ORDER BY id DESC LIMIT 3');
  res.send(`<h1>Visit Counter</h1><p>방문 ${rows[0].n}회 · ${greeting}</p><p>최근 방문자: ${recent.rows.map((r) => r.email).join(', ')}</p>`);
});

const port = process.env.PORT || 3000;
pool
  .query('CREATE TABLE IF NOT EXISTS visits (id serial PRIMARY KEY, at timestamptz DEFAULT now(), email text)')
  .then(() => pool.query('ALTER TABLE visits ADD COLUMN IF NOT EXISTS email text'))
  .then(() => app.listen(port, () => console.log(`listening on ${port}`)))
  .catch((e) => { console.error(e); process.exit(1); });
