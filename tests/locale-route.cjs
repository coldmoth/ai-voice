const fs = require('node:fs'), path = require('node:path');

module.exports = function serveLocale(url, res, root) {
  const match = /^\/locales\/(en|ru)\.json$/.exec(url.pathname);
  if (!match) return false;
  res.writeHead(200, {'Content-Type': 'application/json; charset=utf-8'});
  res.end(fs.readFileSync(path.join(root, 'src/ai_voice/locales', match[1] + '.json')));
  return true;
};
