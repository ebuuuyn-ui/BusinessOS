// Enhance the existing supplier select; the submitted field and server validation stay unchanged.
(() => {
  const norm = value => String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[Iİı]/g, 'i').toLowerCase();
  document.querySelectorAll('select[name="direct_supplier_id"]').forEach((select, index) => {
    if (select.dataset.searchEnhanced) return;
    select.dataset.searchEnhanced = 'true';
    const form = select.form;
    const wrapper = document.createElement('div');
    wrapper.className = 'supplier-search';
    const input = document.createElement('input');
    input.type = 'search';
    input.autocomplete = 'off';
    input.placeholder = 'Tedarikçi adı veya cari kodu yazın';
    input.setAttribute('aria-label', 'Tedarikçi ara');
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false');
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'supplier-list-toggle';
    toggle.textContent = '⌄';
    toggle.title = 'Tüm tedarikçileri listele';
    toggle.setAttribute('aria-label', 'Tedarikçi listesini aç');
    const list = document.createElement('div');
    list.id = `supplier-search-list-${index}`;
    list.className = 'supplier-search-results';
    list.setAttribute('role', 'listbox');
    list.setAttribute('aria-label', 'Tedarikçiler');
    list.hidden = true;
    input.setAttribute('aria-controls', list.id);
    toggle.setAttribute('aria-controls', list.id);
    toggle.setAttribute('aria-expanded', 'false');
    const empty = document.createElement('div');
    empty.className = 'supplier-search-empty';
    empty.textContent = 'Eşleşen tedarikçi bulunamadı';
    empty.setAttribute('role', 'status');
    const options = [...select.options].filter(o => o.value).map((option, n) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.tabIndex = -1;
      button.id = `${list.id}-option-${n}`;
      const name = document.createElement('span');
      name.className = 'supplier-option-name';
      name.textContent = option.dataset.name || option.textContent;
      button.appendChild(name);
      if (option.dataset.code) {
        const code = document.createElement('small');
        code.className = 'supplier-option-code';
        code.textContent = option.dataset.code;
        button.appendChild(code);
      }
      button.dataset.label = option.textContent;
      button.dataset.value = option.value;
      button.dataset.search = norm(option.textContent);
      button.setAttribute('role', 'option');
      button.setAttribute('aria-selected', String(select.value === option.value));
      list.appendChild(button);
      return button;
    });
    list.appendChild(empty);
    wrapper.append(input, toggle, list);
    select.after(wrapper);
    select.hidden = true;
    let active = -1;
    let visible = options;
    function close() {
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      toggle.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      options.forEach(o => o.classList.remove('active'));
      active = -1;
    }
    function show(all = false) {
      const terms = all ? [] : norm(input.value).split(/\s+/).filter(Boolean);
      visible = options.filter(o => {
        o.hidden = !terms.every(term => o.dataset.search.includes(term));
        o.classList.remove('active');
        return !o.hidden;
      });
      empty.hidden = visible.length > 0;
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      toggle.setAttribute('aria-expanded', 'true');
      input.removeAttribute('aria-activedescendant');
      list.scrollTop = 0;
      active = -1;
    }
    function choose(option) {
      select.value = option.dataset.value;
      input.value = option.dataset.label;
      input.setCustomValidity('');
      options.forEach(o => o.setAttribute('aria-selected', String(o === option)));
      select.dispatchEvent(new Event('change', {bubbles: true}));
      input.focus();
      close();
    }
    options.forEach(o => o.addEventListener('click', () => choose(o)));
    input.addEventListener('focus', () => show(Boolean(select.value)));
    input.addEventListener('input', () => {
      select.value = '';
      input.setCustomValidity('');
      options.forEach(o => o.setAttribute('aria-selected', 'false'));
      select.dispatchEvent(new Event('change', {bubbles: true}));
      show();
    });
    toggle.addEventListener('click', () => {
      if (!list.hidden && !input.value) close();
      else { input.focus(); show(true); }
    });
    input.addEventListener('keydown', e => {
      if (e.key === 'Escape') { close(); return; }
      if (e.key === 'Tab') { close(); return; }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (list.hidden) show(Boolean(select.value));
        if (!visible.length) return;
        active = (active + (e.key === 'ArrowDown' ? 1 : -1) + visible.length) % visible.length;
        options.forEach(o => o.classList.remove('active'));
        visible[active].classList.add('active');
        input.setAttribute('aria-activedescendant', visible[active].id);
        visible[active].scrollIntoView({block: 'nearest'});
      } else if (e.key === 'Enter' && !list.hidden) {
        e.preventDefault();
        if (visible.length) choose(visible[active < 0 ? 0 : active]);
      }
    });
    document.addEventListener('click', e => { if (!wrapper.contains(e.target)) close(); });
    form.addEventListener('change', () => input.setCustomValidity(''));
    form.addEventListener('submit', e => {
      if (!wrapper.closest('[hidden]') && !select.value) {
        e.preventDefault();
        input.focus();
        input.setCustomValidity('Lütfen arama sonuçlarından veya listeden bir tedarikçi seçin.');
        input.reportValidity();
      }
    });
    form.addEventListener('reset', () => setTimeout(() => {input.value = select.value ? select.selectedOptions[0].textContent : '';input.setCustomValidity('');close();}, 0));
    if (select.value) input.value = select.selectedOptions[0].textContent;
  });
})();
