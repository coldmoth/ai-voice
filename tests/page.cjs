const fs = require('fs');
const path = require('path');
// Same assembly as desktop.render_page(): inline style + scripts into index.html.
module.exports = function renderPage(root, token = 'qa-token') {
  const dir = path.join(root, 'macos/desktop');
  const read = name => fs.readFileSync(path.join(dir, name), 'utf8');
  return read('index.html')
    .replace('__STYLE__', () => read('style.css') + '\n' + read('onboarding.css'))
    .replace('__SCRIPT__', () => 'const APP_TOKEN = ' + JSON.stringify(token) + ';\n' + read('app.js') + '\n' + read('onboarding.js'));
};
