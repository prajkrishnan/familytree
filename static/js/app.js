/* Small shared behaviours: flashes, confirmations, menus. */
(function () {
  'use strict';

  if (document.body.getAttribute('data-refreshed') === '1') {
    var notice = document.createElement('div');
    notice.className = 'flash flash-warn';
    notice.setAttribute('role', 'status');
    notice.textContent = 'Your previous session had ended. The page was refreshed — please submit again.';
    document.body.insertBefore(notice, document.body.firstChild);
    window.setTimeout(function () { notice.remove(); }, 9000);
  }

  document.querySelectorAll('[data-flash]').forEach(function (flash) {
    var close = flash.querySelector('.flash-close');
    function dismiss() {
      flash.classList.add('leaving');
      window.setTimeout(function () { flash.remove(); }, 220);
    }
    if (close) close.addEventListener('click', dismiss);
    window.setTimeout(dismiss, 6500);
  });

  document.addEventListener('submit', function (event) {
    var form = event.target;
    var message = form.getAttribute('data-confirm');
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });

  document.addEventListener('click', function (event) {
    document.querySelectorAll('details.menu[open]').forEach(function (menu) {
      if (!menu.contains(event.target)) menu.removeAttribute('open');
    });
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      document.querySelectorAll('details.menu[open]').forEach(function (menu) {
        menu.removeAttribute('open');
      });
    }
  });

  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-copy]');
    if (!button) return;
    var input = document.querySelector(button.getAttribute('data-copy'));
    if (!input) return;
    var done = function () {
      var old = button.textContent;
      button.textContent = 'Copied';
      window.setTimeout(function () { button.textContent = old; }, 1600);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(input.value).then(done).catch(function () {
        input.select();
        document.execCommand('copy');
        done();
      });
    } else {
      input.select();
      document.execCommand('copy');
      done();
    }
  });
})();
