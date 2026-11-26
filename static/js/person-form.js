/* Person form: living/death toggle and "link an existing person" picker. */
(function () {
  'use strict';

  const form = document.getElementById('person-form');
  if (!form) return;

  const living = document.getElementById('living');
  const deathFields = document.getElementById('death-fields');
  if (living && deathFields) {
    const sync = function () {
      deathFields.classList.toggle('is-hidden', living.checked);
    };
    living.addEventListener('change', sync);
    sync();
  }

  const picker = form.querySelector('[data-picker]');
  if (!picker) return;
  const input = picker.querySelector('[data-picker-input]');
  const value = picker.querySelector('[data-picker-value]');
  const results = picker.querySelector('[data-picker-results]');
  const selected = form.querySelector('[data-picker-selected]');
  const selectedName = form.querySelector('[data-picker-name]');
  const clearButton = form.querySelector('[data-picker-clear]');
  const newFields = document.getElementById('new-person-fields');
  let timer = 0;

  function hideResults() {
    results.hidden = true;
    results.replaceChildren();
  }

  function choose(person) {
    value.value = person.id;
    selectedName.textContent = person.name;
    selected.hidden = false;
    newFields.hidden = true;
    picker.hidden = true;
    hideResults();
    input.value = '';
  }

  function clear() {
    value.value = '';
    selected.hidden = true;
    newFields.hidden = false;
    picker.hidden = false;
  }

  function render(resultsData) {
    results.replaceChildren();
    if (!resultsData.length) {
      const empty = document.createElement('div');
      empty.className = 'picker-empty';
      empty.textContent = 'No matches — you can create a new person below.';
      results.appendChild(empty);
    } else {
      resultsData.forEach(function (person) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'picker-result';
        const strong = document.createElement('strong');
        strong.textContent = person.name;
        const small = document.createElement('small');
        small.textContent = person.subtitle;
        button.appendChild(strong);
        button.appendChild(small);
        button.addEventListener('click', function () { choose(person); });
        results.appendChild(button);
      });
    }
    results.hidden = false;
  }

  input.addEventListener('input', function () {
    window.clearTimeout(timer);
    const query = input.value.trim();
    if (!query) { hideResults(); return; }
    timer = window.setTimeout(async function () {
      try {
        const response = await fetch('/api/search?q=' + encodeURIComponent(query));
        if (!response.ok) return;
        const data = await response.json();
        render(data.results || []);
      } catch (err) { /* ignore */ }
    }, 180);
  });

  input.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') hideResults();
  });

  document.addEventListener('click', function (event) {
    if (!picker.contains(event.target)) hideResults();
  });

  if (clearButton) clearButton.addEventListener('click', clear);
})();
