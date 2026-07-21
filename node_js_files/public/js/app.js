// Tiny client-side enhancement: mark the active nav link.
document.addEventListener('DOMContentLoaded', function () {
  var path = window.location.pathname;
  document.querySelectorAll('.topbar nav a').forEach(function (link) {
    if (link.getAttribute('href') === path) {
      link.style.fontWeight = '700';
    }
  });
});
