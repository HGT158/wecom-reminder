// 日期/时间滚轮选择器与自定义下拉弹窗共享脚本（供新建页与编辑页复用）
(function () {
  const pad = n => String(n).padStart(2, '0');
  const fmtDate = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

  function closePopEl(el) {
    if (!el || el.hidden) return;
    el.classList.add('closing');
    setTimeout(() => { el.hidden = true; el.classList.remove('closing'); }, 150);
  }

  window.initReminderPicker = function (opts) {
    const cfg = opts || {};
    const dtField = document.getElementById('dtField');
    const dtDisplay = document.getElementById('dtDisplay');
    const dtPop = document.getElementById('dtPop');
    const dtGrid = document.getElementById('dtGrid');
    const dtMonth = document.getElementById('dtMonth');
    const wheelH = document.getElementById('wheelH');
    const wheelM = document.getElementById('wheelM');
    const remindAt = document.getElementById('remindAt');
    const dtHint = document.getElementById('dtHint');
    const ITEM_H = () => (wheelH.children[0] ? wheelH.children[0].offsetHeight : (cfg.fallbackItemH || 36));
    const chips = cfg.quickSelector ? document.querySelectorAll(cfg.quickSelector) : [];

    let view = new Date();
    view.setDate(1);
    let sel = null;
    let hintTimer = null;

    function buildWheel(el, count, def, minV) {
      el.dataset.min = minV;
      const start = Math.max(0, Math.min(count - 1, Math.max(def, minV)));
      el.dataset.v = start;
      el.innerHTML = '';
      for (let i = 0; i < count; i++) {
        const d = document.createElement('div');
        d.className = 'wheel-item' + (i < minV ? ' disabled' : '');
        d.textContent = pad(i);
        d.dataset.v = i;
        el.appendChild(d);
      }
      if (!el.dataset.bound) {
        el.dataset.bound = '1';
        let raf = null;
        el.addEventListener('scroll', () => {
          if (raf) return;
          raf = requestAnimationFrame(() => {
            raf = null;
            const total = el.children.length;
            let idx = Math.max(0, Math.min(total - 1, Math.round(el.scrollTop / ITEM_H())));
            // 滑进禁用区（过去的时间）→ 自动弹回最早可选位置
            if (el.children[idx] && el.children[idx].classList.contains('disabled')) {
              idx = +el.dataset.min;
              el.scrollTo({ top: idx * ITEM_H() });
            }
            if (el.dataset.v !== String(idx)) {
              el.dataset.v = idx;
              el.querySelectorAll('.wheel-item.sel').forEach(x => x.classList.remove('sel'));
              if (el.children[idx]) el.children[idx].classList.add('sel');
              if (el === wheelH) applyMinuteMin();
              syncValue();
            }
          });
        });
        el.addEventListener('click', e => {
          const it = e.target.closest('.wheel-item');
          if (!it || it.classList.contains('disabled')) return;
          el.scrollTo({ top: (+it.dataset.v) * ITEM_H(), behavior: 'smooth' });
        });
      }
    }

    function setWheel(el, v) {
      v = Math.max(+el.dataset.min || 0, v);
      el.dataset.v = v;
      el.scrollTo({ top: v * ITEM_H() });
      el.querySelectorAll('.wheel-item.sel').forEach(x => x.classList.remove('sel'));
      if (el.children[v]) el.children[v].classList.add('sel');
    }

    // 选中“今天”时，过去的小时/分钟不可选；未来日期全部可选
    function wheelLimits() {
      if (!sel) return { minH: 0, minM: 0 };
      const n = new Date();
      if (sel.getFullYear() !== n.getFullYear() || sel.getMonth() !== n.getMonth() || sel.getDate() !== n.getDate()) {
        return { minH: 0, minM: 0 };
      }
      return { minH: n.getHours(), minM: n.getMinutes() };
    }

    function applyMinuteMin() {
      const { minH, minM } = wheelLimits();
      const min = (+wheelH.dataset.v === minH) ? minM : 0;
      if (+wheelM.dataset.min !== min) {
        buildWheel(wheelM, 60, Math.max(+wheelM.dataset.v || 0, min), min);
        setWheel(wheelM, +wheelM.dataset.v);
      }
    }

    function applyLimits() {
      const { minH } = wheelLimits();
      buildWheel(wheelH, 24, +wheelH.dataset.v || 0, minH);
      setWheel(wheelH, +wheelH.dataset.v);
      applyMinuteMin();
      setWheel(wheelM, +wheelM.dataset.v);
    }

    function syncValue() {
      if (!sel) {
        remindAt.value = '';
        dtDisplay.value = '';
        return;
      }
      const h = pad(+wheelH.dataset.v || 0);
      const m = pad(+wheelM.dataset.v || 0);
      remindAt.value = `${fmtDate(sel)}T${h}:${m}`;
      dtDisplay.value = `${fmtDate(sel)} ${h}:${m}`;
    }

    function applyIso(iso) {
      const [dp, tp] = iso.split('T');
      const [Y, Mo, D] = dp.split('-').map(Number);
      const [H, M] = (tp || '00:00').split(':').map(Number);
      sel = new Date(Y, Mo - 1, D);
      view = new Date(Y, Mo - 1, 1);
      renderGrid();
      applyLimits();
      setWheel(wheelH, H);
      applyMinuteMin();
      setWheel(wheelM, M);
      syncValue();
    }

    function renderGrid() {
      dtMonth.textContent = `${view.getFullYear()}年${view.getMonth() + 1}月`;
      dtGrid.innerHTML = '';
      const startDow = new Date(view.getFullYear(), view.getMonth(), 1).getDay();
      const daysIn = new Date(view.getFullYear(), view.getMonth() + 1, 0).getDate();
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      const now = new Date();
      document.getElementById('dtPrev').disabled =
        view.getFullYear() === now.getFullYear() && view.getMonth() === now.getMonth();
      for (let i = 0; i < startDow; i++) dtGrid.appendChild(document.createElement('span'));
      for (let d = 1; d <= daysIn; d++) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = d;
        const cur = new Date(view.getFullYear(), view.getMonth(), d);
        if (cur.getTime() < today.getTime()) {
          btn.classList.add('disabled');
        } else {
          if (sel && cur.getTime() === sel.getTime()) btn.classList.add('sel');
          if (cur.getTime() === today.getTime()) btn.classList.add('today');
          btn.addEventListener('click', () => {
            sel = cur;
            applyLimits();
            renderGrid();
            syncValue();
          });
        }
        dtGrid.appendChild(btn);
      }
    }

    function showHint(msg) {
      dtHint.textContent = msg;
      dtHint.hidden = false;
      clearTimeout(hintTimer);
      hintTimer = setTimeout(() => { dtHint.hidden = true; }, 2200);
    }

    buildWheel(wheelH, 24, 9, 0);
    buildWheel(wheelM, 60, 0, 0);

    if (cfg.initialIso) {
      applyIso(cfg.initialIso);
    }

    dtDisplay.addEventListener('click', () => {
      if (dtPop.hidden) {
        dtPop.classList.remove('closing');
        dtPop.hidden = false;
        if (remindAt.value) {
          applyIso(remindAt.value);
        } else {
          renderGrid();
          applyLimits();
          syncValue();
        }
      } else {
        closePopEl(dtPop);
      }
    });

    document.getElementById('dtPrev').addEventListener('click', () => { view.setMonth(view.getMonth() - 1); renderGrid(); });
    document.getElementById('dtNext').addEventListener('click', () => { view.setMonth(view.getMonth() + 1); renderGrid(); });
    document.getElementById('dtNow').addEventListener('click', () => {
      const n = new Date();
      sel = new Date(n.getFullYear(), n.getMonth(), n.getDate());
      applyLimits();
      setWheel(wheelH, n.getHours());
      applyMinuteMin();
      setWheel(wheelM, n.getMinutes());
      renderGrid();
      syncValue();
    });
    document.getElementById('dtClear').addEventListener('click', () => {
      sel = null;
      syncValue();
      closePopEl(dtPop);
      chips.forEach(x => x.classList.remove('active'));
    });
    document.getElementById('dtOk').addEventListener('click', () => {
      if (!sel) sel = new Date(new Date().setHours(0, 0, 0, 0));
      const chosen = new Date(sel.getFullYear(), sel.getMonth(), sel.getDate(),
                              +wheelH.dataset.v || 0, +wheelM.dataset.v || 0);
      const now = new Date();
      if (chosen <= now) {
        // 只能选未来：自动顺延到 1 分钟后（跨天则连日期一起推进）
        const t = new Date(now.getTime() + 60000);
        sel = new Date(t.getFullYear(), t.getMonth(), t.getDate());
        setWheel(wheelH, t.getHours());
        setWheel(wheelM, t.getMinutes());
        view = new Date(t.getFullYear(), t.getMonth(), 1);
        renderGrid();
        syncValue();
        showHint('已自动调整到最近的可选时间');
        return;
      }
      syncValue();
      closePopEl(dtPop);
    });

    document.addEventListener('click', e => {
      const path = e.composedPath();
      if (!path.includes(dtField)) closePopEl(dtPop);
      document.querySelectorAll('.dd').forEach(dd => {
        if (!path.includes(dd)) closePopEl(dd.querySelector('.dd-pop'));
      });
    });

    document.querySelectorAll('.dd').forEach(root => {
      const btn = root.querySelector('.dd-btn');
      const pop = root.querySelector('.dd-pop');
      const hidden = root.querySelector('input[type=hidden]');
      const val = root.querySelector('.dd-val');
      btn.addEventListener('click', () => {
        if (pop.hidden) {
          document.querySelectorAll('.dd-pop').forEach(p => { if (p !== pop) closePopEl(p); });
          pop.classList.remove('closing');
          pop.hidden = false;
        } else {
          closePopEl(pop);
        }
      });
      pop.querySelectorAll('.dd-opt').forEach(o => o.addEventListener('click', () => {
        pop.querySelectorAll('.dd-opt.sel').forEach(x => x.classList.remove('sel'));
        o.classList.add('sel');
        hidden.value = o.dataset.v;
        val.textContent = o.textContent;
        closePopEl(pop);
      }));
    });

    chips.forEach(b => b.addEventListener('click', () => {
      const now = new Date();
      let d = new Date(now);
      if (b.dataset.act === '1h') d = new Date(now.getTime() + 3600e3);
      if (b.dataset.act === 't9') { d = new Date(now); d.setDate(d.getDate() + 1); d.setHours(9, 0, 0, 0); }
      if (b.dataset.act === 't20') { d = new Date(now); d.setDate(d.getDate() + 1); d.setHours(20, 0, 0, 0); }
      if (b.dataset.act === 'tomo') { d = new Date(now); d.setDate(d.getDate() + 1); }
      sel = new Date(d.getFullYear(), d.getMonth(), d.getDate());
      applyLimits();
      setWheel(wheelH, d.getHours());
      applyMinuteMin();
      setWheel(wheelM, d.getMinutes());
      view = new Date(d.getFullYear(), d.getMonth(), 1);
      renderGrid();
      syncValue();
      chips.forEach(x => x.classList.remove('active'));
      b.classList.add('active');
    }));

    const form = document.querySelector(cfg.formSelector || 'form');
    if (form) {
      form.addEventListener('submit', e => {
        if (!remindAt.value) {
          e.preventDefault();
          alert('请先选择提醒时间');
          dtPop.classList.remove('closing');
          dtPop.hidden = false;
          renderGrid();
        }
      });
    }
  };
})();
