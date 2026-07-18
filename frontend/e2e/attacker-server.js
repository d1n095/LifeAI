// Minimal static server serving fixtures/attacker.html on a genuinely separate origin/port
// (see playwright.config.ts's ATTACKER_URL) — simulates a real third-party site for the
// cross-origin CSRF attack test, not just a different path on the same origin.
const http = require("http");
const fs = require("fs");
const path = require("path");

const PORT = process.env.ATTACKER_PORT || 9099;
const html = fs.readFileSync(path.join(__dirname, "fixtures", "attacker.html"));

http
  .createServer((req, res) => {
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(html);
  })
  .listen(PORT, "127.0.0.1", () => {
    console.log(`attacker origin listening on http://127.0.0.1:${PORT}`);
  });
