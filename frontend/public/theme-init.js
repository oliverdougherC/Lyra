/* global localStorage, window, document */
;(function () {
  try {
    var theme = localStorage.getItem('lyra-theme')
    var dark =
      theme === 'dark' ||
      (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)
    document.documentElement.classList.toggle('dark', dark)
  } catch {
    // Storage can be unavailable in hardened webviews. The light default remains valid.
  }
})()
