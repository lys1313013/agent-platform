/* global localStorage, window, document */
try {
  var stored = JSON.parse(localStorage.getItem('aio-theme') || '{}');
  var theme = stored.state && stored.state.theme ? stored.state.theme : 'dark';
  var resolved = theme === 'system'
    ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : theme;
  document.documentElement.classList.remove('light', 'dark');
  document.documentElement.classList.add(resolved);
} catch {}
