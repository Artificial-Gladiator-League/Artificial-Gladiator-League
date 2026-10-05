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

    // Tournaments open to India (or to all countries) are chess only: lock the game type.
    var countriesField = document.getElementById('id_allowed_countries');
    var gameField = document.getElementById('id_game_type');
    function lockChessForIndia() {
      if (!countriesField || !gameField) return;
      var codes = countriesField.value.toUpperCase().split(/[\s,;]+/).filter(Boolean);
      var locked = codes.length === 0 || codes.indexOf('IN') !== -1;
      if (locked) gameField.value = 'chess';
      Array.prototype.forEach.call(gameField.options, function (o) {
        o.disabled = locked && o.value !== 'chess';
      });
    }
    if (countriesField) countriesField.addEventListener('input', lockChessForIndia);
    lockChessForIndia();

    // Countries: blank = all countries. A new Gauntlet starts open; QA and Gladiatormania start
    // Israel-only. Only an untouched field on the add form follows the type.
    var COUNTRY_DEFAULTS = { gauntlet: '', gladiatormania: 'IL', qa: 'IL' };
    var countriesTouched = false;
    if (countriesField) {
      countriesField.addEventListener('input', function () { countriesTouched = true; });
      typeField.addEventListener('change', function () {
        if (!isAddForm || countriesTouched) return;
        var d = COUNTRY_DEFAULTS[typeField.value];
        if (d === undefined) return;
        countriesField.value = d;
        lockChessForIndia();
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setup);
  } else {
    setup();
  }
})();
