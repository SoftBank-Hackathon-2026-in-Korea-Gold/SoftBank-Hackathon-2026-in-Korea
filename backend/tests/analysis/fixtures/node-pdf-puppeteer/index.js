const express = require('express');
const puppeteer = require('puppeteer');

const app = express();
app.get('/pdf', async (req, res) => {
  const browser = await puppeteer.launch();
  const page = await browser.newPage();
  await page.goto(req.query.url);
  res.type('pdf').send(await page.pdf());
  await browser.close();
});
app.listen(process.env.PORT || 3000);
