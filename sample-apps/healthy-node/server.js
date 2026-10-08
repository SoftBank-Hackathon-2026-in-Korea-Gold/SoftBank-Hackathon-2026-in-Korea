const express = require("express");
const app = express();
const port = process.env.PORT || 8080;

app.get("/", (req, res) => {
  res.json({ message: "hello from express", runtime: process.version });
});

app.listen(port, "0.0.0.0", () => console.log(`listening on ${port}`));
