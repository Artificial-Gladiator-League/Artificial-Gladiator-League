(function () {
  'use strict';

  var TYPE_DEFAULTS = {
    small: { capacity: 128, rounds_total: 7 },
    large: { capacity: 1024, rounds_total: 10 },
    qa:    { capacity: 2,   rounds_total: 1 },
    gauntlet:       { capacity: 16, rounds_total: 5 },
    gladiatormania: { capacity: 16, rounds_total: 5 }
  };

  function setup() {
    var typeField     = document.getElementById('id_type');
    var capacityField = document.getElementById('id_capacity');
    var roundsField   = document.getElementById('id_rounds_total');

    if (!typeField) return;

    var isAddForm = /\/add\/?$/.test(window.location.pathname);
    var prevType = typeField.value;

    // Fill on the add form, or when the field is empty or still holds the
    // previous type's default; never overwrite a value set on an existing tournament.
    function maybeFill(field, newValue, prevValue) {
      if (!field) return;
      var current = field.value.trim();
      if (isAddForm || current === '' || current === String(prevValue)) {
        field.value = newValue;
      }
    }

    function applyDefaults() {
      var t = typeField.value;
      var d = TYPE_DEFAULTS[t];
      var prev = TYPE_DEFAULTS[prevType] || {};
      prevType = t;
      if (!d) return;

      var locked = (t === 'qa');
      if (locked) {
        // QA is forced to 2 players / 1 round by the model on save.
        if (capacityField) capacityField.value = d.capacity;
        if (roundsField) roundsField.value = d.rounds_total;
      } else {
        maybeFill(capacityField, d.capacity, prev.capacity);
        maybeFill(roundsField, d.rounds_total, prev.rounds_total);
      }
      if (capacityField) capacityField.readOnly = locked;
      if (roundsField) roundsField.readOnly = locked;
    }

    typeField.addEventListener('change', applyDefaults);

    // Terms only apply to Gauntlet-like types; hide and clear the dropdown otherwise.
    var MONEY_LIKE = ['gauntlet', 'gladiatormania'];
    var termsField = document.getElementById('id_terms');
    var termsRow = termsField && termsField.closest('.form-row');
    function toggleTerms() {
      if (!termsRow) return;
      var show = MONEY_LIKE.indexOf(typeField.value) !== -1;
      termsRow.style.display = show ? '' : 'none';
      if (!show) termsField.value = '';
    }
    typeField.addEventListener('change', toggleTerms);
    toggleTerms();

    // Tournaments open to India are chess only: lock the game type while IN is listed.
    var countriesField = document.getElementById('id_allowed_countries');
    var gameField = document.getElementById('id_game_type');
    function lockChessForIndia() {
      if (!countriesField || !gameField) return;
      var codes = countriesField.value.toUpperCase().split(/[\s,;]+/);
      var locked = codes.indexOf('IN') !== -1;
      if (locked) gameField.value = 'chess';
      Array.prototype.forEach.call(gameField.options, function (o) {
        o.disabled = locked && o.value !== 'chess';
      });
    }
    if (countriesField) countriesField.addEventListener('input', lockChessForIndia);
    lockChessForIndia();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setup);
  } else {
    setup();
  }
})();
