const express = require('express');
const session = require('express-session');
const multer = require('multer');
const cron = require('node-cron');

const upload = multer({ dest: 'uploads/' });
const app = express();
app.use(session({ secret: process.env.SESSION_SECRET, resave: false, saveUninitialized: true }));

let uploads = 0;

app.post('/photos', upload.single('photo'), (req, res) => {
  uploads++;
  req.session.last = req.file.filename;
  res.json({ ok: true, uploads });
});

cron.schedule('0 3 * * *', () => console.log('nightly cleanup'));

app.listen(3000, '127.0.0.1');
