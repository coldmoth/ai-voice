// EN/RU switch: elements with data-ru swap their innerHTML; the English original is kept in data-en.
(function () {
  var nodes = document.querySelectorAll('[data-ru]');
  var buttons = document.querySelectorAll('.lang button');
  function set(lang) {
    nodes.forEach(function (n) {
      if (!n.dataset.en) n.dataset.en = n.innerHTML;
      n.innerHTML = lang === 'ru' ? n.dataset.ru : n.dataset.en;
    });
    document.documentElement.lang = lang;
    buttons.forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.lang === lang)); });
    try { localStorage.setItem('lang', lang); } catch (e) {}
  }
  buttons.forEach(function (b) { b.addEventListener('click', function () { set(b.dataset.lang); }); });
  var saved = null;
  try { saved = localStorage.getItem('lang'); } catch (e) {}
  set(saved || ((navigator.language || '').slice(0, 2) === 'ru' ? 'ru' : 'en'));
})();
