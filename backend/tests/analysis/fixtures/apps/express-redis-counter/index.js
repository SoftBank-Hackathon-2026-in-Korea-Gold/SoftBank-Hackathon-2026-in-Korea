const express = require('express');
const { createClient } = require('redis');

const redis = createClient({ url: process.env.REDIS_URL });
redis.on('error', (err) => console.error('redis error', err.message));

const app = express();

app.get('/', async (req, res) => {
  const count = await redis.incr('page:views');
  res.send(`<h1>Redis Counter</h1><p>이 페이지는 ${count}번 조회되었습니다.</p>`);
});

app.get('/health', async (req, res) => {
  try {
    await redis.ping();
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.get('/admin', async (req, res) => {
  const token = req.get('Authorization');
  if (!process.env.API_TOKEN || token !== `Bearer ${process.env.API_TOKEN}`) {
    return res.status(401).json({ error: 'unauthorized' });
  }
  res.json({ views: Number(await redis.get('page:views')) || 0 });
});

const port = process.env.PORT || 3000;
redis.connect().then(() => {
  app.listen(port, () => console.log(`listening on ${port}`));
});
