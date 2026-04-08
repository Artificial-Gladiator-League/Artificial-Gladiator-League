(function () {
  'use strict';

  var TYPE_DEFAULTS = {
    small: { capacity: 128, rounds_total: 7 },
    large: { capacity: 1024, rounds_total: 10 },
    qa:    { capacity: 2,   rounds_total: 1 }
  };

  function setup() {
    var typeField     = document.getElementById('id_type');
    var capacityField = document.getElementById('id_capacity');
    var roundsField   = document.getElementById('id_rounds_total');

    if (!typeField) return;

    function applyDefaults() {
      var t = typeField.value;
      var d = TYPE_DEFAULTS[t];
      if (!d) return;

      if (capacityField) {
        capacityField.value = d.capacity;
        capacityField.readOnly = (t === 'qa');
      }
      if (roundsField) {
        roundsField.value = d.rounds_total;
        roundsField.readOnly = (t === 'qa');
      }
    }

    typeField.addEventListener('change', applyDefaults);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setup);
  } else {
    setup();
  }
})();
