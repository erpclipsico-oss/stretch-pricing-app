/* v181 -- autosave of the quotation builder (Export + Local pricing pages).
 *
 * Owner request: work in the pricing screen must not disappear because the
 * browser went Back/Forward, the page was refreshed, or the tab/sheet was
 * closed -- it stays saved exactly as it was last left, until the rep
 * presses the "new quotation" (trash) button.
 *
 * Generic on purpose: it snapshots every <input>/<select> in the header
 * fields and in each lines table row (by their own class names, in DOM
 * order), and replays them through the page's own addLine()/addStrapLine()
 * and the real 'change' events, so all of the page's existing logic (roll
 * spec prefills, Pre-Stretch sub-rows, recalculation) runs exactly as if
 * the rep had picked those values by hand. Stored per user in this
 * browser's localStorage (never sent anywhere); every storage call is
 * wrapped in try/catch so a blocked/private-mode browser just behaves as
 * before (no draft). */
(function () {
  function fieldKey(el) {
    const cls = (el.className || '').toString().split(/\s+/).filter(c => c.indexOf('f-') === 0);
    return cls.length ? cls[0] : null;
  }

  function snapshotRow(tr) {
    const out = [];
    const seen = {};
    tr.querySelectorAll('input, select').forEach(el => {
      const k = fieldKey(el);
      if (!k) return;
      seen[k] = (seen[k] || 0) + 1;
      out.push({k: k, n: seen[k], t: el.type === 'checkbox' ? 'c' : 'v',
                v: el.type === 'checkbox' ? el.checked : el.value});
    });
    return {c: out, rpp: tr.dataset ? (tr.dataset.rppEdited || '') : ''};
  }

  function restoreRow(tr, saved) {
    const seen = {};
    tr.querySelectorAll('input, select').forEach(el => {
      const k = fieldKey(el);
      if (!k) return;
      seen[k] = (seen[k] || 0) + 1;
      const s = saved.c.find(x => x.k === k && x.n === seen[k]);
      if (!s) return;
      if (s.t === 'c') {
        if (el.checked !== !!s.v) {
          el.checked = !!s.v;
          el.dispatchEvent(new Event('change', {bubbles: true}));
        }
      } else if (el.value !== String(s.v)) {
        if (el.tagName === 'SELECT') {
          const has = [...el.options].some(o => o.value === String(s.v));
          if (!has) return;
          el.value = String(s.v);
          el.dispatchEvent(new Event('change', {bubbles: true}));
        } else {
          el.value = s.v;
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
        }
      }
    });
    if (tr.dataset && saved.rpp !== undefined) tr.dataset.rppEdited = saved.rpp;
  }

  window.DraftSaver = function (cfg) {
    const KEY = cfg.key;
    let restoring = false;
    let disabled = false;
    let timer = null;

    function read() {
      try { const raw = localStorage.getItem(KEY); return raw ? JSON.parse(raw) : null; } catch (e) { return null; }
    }
    function write(obj) {
      try { localStorage.setItem(KEY, JSON.stringify(obj)); } catch (e) {}
    }
    function clear() {
      disabled = true;
      try { localStorage.removeItem(KEY); } catch (e) {}
    }

    function snapshot() {
      const header = {};
      cfg.headerIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) header[id] = el.value;
      });
      const tables = cfg.tables.map(t => {
        const body = document.querySelector(t.body);
        const rows = [];
        if (body) {
          [...body.children].forEach(tr => {
            if (tr.classList.contains('prestretch-row')) return;
            const rec = snapshotRow(tr);
            const next = tr.nextElementSibling;
            if (next && next.classList.contains('prestretch-row')) rec.ps = snapshotRow(next);
            rows.push(rec);
          });
        }
        return rows;
      });
      return {v: 1, ts: Date.now(), header: header, tables: tables, extra: cfg.getExtra ? cfg.getExtra() : {}};
    }

    function save() {
      if (restoring || disabled) return;
      write(snapshot());
    }
    function scheduleSave() {
      if (restoring || disabled) return;
      clearTimeout(timer);
      timer = setTimeout(save, 300);
    }

    function restore(d) {
      restoring = true;
      try {
        cfg.headerIds.forEach(id => {
          const el = document.getElementById(id);
          if (!el || d.header[id] === undefined) return;
          if (el.tagName === 'SELECT' && ![...el.options].some(o => o.value === String(d.header[id]))) return;
          el.value = d.header[id];
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
        });
        cfg.tables.forEach((t, ti) => {
          const body = document.querySelector(t.body);
          const rows = d.tables[ti] || [];
          if (!body) return;
          if (!rows.length && t.keepFirstIfEmpty) return;
          body.innerHTML = '';
          rows.forEach(rec => {
            t.add();
            // the row just added is the last non-prestretch child
            const mains = [...body.children].filter(r => !r.classList.contains('prestretch-row'));
            const tr = mains[mains.length - 1];
            restoreRow(tr, rec);
            const next = tr.nextElementSibling;
            if (rec.ps && next && next.classList.contains('prestretch-row')) restoreRow(next, rec.ps);
          });
        });
        if (cfg.setExtra) cfg.setExtra(d.extra || {});
      } finally {
        restoring = false;
      }
      if (cfg.afterRestore) cfg.afterRestore();
      const bar = document.createElement('div');
      bar.className = 'hint';
      bar.style.cssText = 'background:#eff6ff;border:1px solid #bfdbfe;color:#1e40af;border-radius:6px;padding:8px 12px;margin:0 0 12px;';
      bar.textContent = d.note ? ('↺ ' + d.note) : '↺ Restored your last unsaved work on this screen. Use the 🗑 button to start a fresh quotation.';
      const host = document.querySelector('.panel');
      if (host) host.insertBefore(bar, host.firstChild.nextSibling || host.firstChild);
    }

    // public
    this.clear = clear;
    this.save = save;
    this.start = function () {
      const d = read();
      if (d && d.v === 1) {
        try { restore(d); } catch (e) { restoring = false; }
      }
      ['input', 'change', 'click'].forEach(ev => document.addEventListener(ev, scheduleSave, true));
      cfg.tables.forEach(t => {
        const body = document.querySelector(t.body);
        if (body) new MutationObserver(scheduleSave).observe(body, {childList: true});
      });
      window.addEventListener('pagehide', save);
      document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') save(); });
      scheduleSave();
    };
  };
})();
