/* v185 -- Meters <-> Roll weight helper for the stretch-film rows (Export + Local).
 * Same formulas as the owner's Material_Calculator sheet:
 *   meter weight (g) = micron x (width/1000) x 0.925
 *   Pre-Stretch: effective width is 420 mm for widths up to 400, 450 mm above
 *   net kg = meter weight x meters / 1000;  gross (roll) kg = net + core kg
 * Type Meters -> Roll kg is filled; type Roll kg -> Meters is filled.
 * Only real keystrokes (isTrusted) drive it, so the autosave restore and the
 * page's own prefills never get overwritten. */
(function () {
  var DENSITY = 0.925;
  function num(el) { var v = parseFloat(el && el.value); return isFinite(v) ? v : 0; }
  function meterWeight(micron, width, prestretch) {
    if (!(micron > 0) || !(width > 0)) return 0;
    var eff = prestretch ? (width <= 400 ? 420 : 450) : width;
    return micron * (eff / 1000) * DENSITY;
  }
  window.MeterSync = {
    meterWeight: meterWeight,
    /* ctx.product() -> product object (micron, width_mm); ctx.prestretch() -> bool */
    attach: function (tr, ctx) {
      var m = tr.querySelector('.f-meters'), w = tr.querySelector('.f-rollwt'),
          c = tr.querySelector('.f-corewt'), wd = tr.querySelector('.f-width');
      if (!m || !w) return;
      function mw() {
        var p = ctx.product(); if (!p) return 0;
        var width = num(wd) || parseFloat(p.width_mm) || (ctx.prestretch() ? 0 : 500);
        return meterWeight(parseFloat(p.micron), width, ctx.prestretch());
      }
      function core() {
        if (c && c.value !== '') return num(c);
        var p = ctx.product(); return p ? (parseFloat(p.core_weight_kg) || 0) : 0;
      }
      function roll() {
        if (w.value !== '') return num(w);
        var p = ctx.product(); return p ? (parseFloat(p.roll_weight_kg) || 0) : 0;
      }
      function fire(el) { el.dispatchEvent(new Event('input', {bubbles: true})); }
      function metersToWeight() {
        var k = mw(), L = num(m);
        if (!k || !L) return;
        w.value = (k * L / 1000 + core()).toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
        fire(w);
      }
      function weightToMeters() {
        var k = mw(), g = roll();
        if (!k || !g) { m.value = ''; return; }
        m.value = Math.max(Math.round((g - core()) * 1000 / k), 0) || '';
      }
      m.addEventListener('input', function (e) {
        if (!e.isTrusted) return;
        tr.dataset.metersDriver = 'm'; metersToWeight();
      });
      w.addEventListener('input', function (e) {
        if (!e.isTrusted) { if (!m.value) weightToMeters(); return; }
        tr.dataset.metersDriver = 'w'; weightToMeters();
      });
      [c, wd].forEach(function (el) {
        if (!el) return;
        el.addEventListener('input', function (e) {
          if (!e.isTrusted) return;
          if (tr.dataset.metersDriver === 'm') metersToWeight(); else weightToMeters();
        });
      });
      tr.addEventListener('change', function (e) {
        if (e.target && e.target.classList && e.target.classList.contains('f-product')) {
          setTimeout(function () { tr.dataset.metersDriver = 'w'; weightToMeters(); }, 0);
        }
      });
      tr._metersRefresh = function () { weightToMeters(); };
      setTimeout(weightToMeters, 0);
    }
  };
})();
