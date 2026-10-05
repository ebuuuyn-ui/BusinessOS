// Mobile presentation reuses existing records, forms and event handlers.
(() => {
  const phone = matchMedia('(max-width:700px)');
  const drawer = matchMedia('(max-width:900px)');
  const sidebar = document.getElementById('main-sidebar');
  const backdrop = document.querySelector('.mobile-nav-backdrop');
  const menuButtons = [...document.querySelectorAll('[data-mobile-menu]')];
  let menuOrigin;
  const closeMenu = (restore = true) => {
    document.body.classList.remove('nav-open');
    backdrop.hidden = true;
    menuButtons.forEach(button => button.setAttribute('aria-expanded','false'));
    if (drawer.matches) sidebar.inert = true;
    document.querySelector('main').inert = false;
    document.querySelector('.mobile-bottom-nav').inert = false;
    if (restore) menuOrigin?.focus();
  };
  const openMenu = button => {
    menuOrigin = button;
    sidebar.inert = false;
    document.body.classList.add('nav-open');
    backdrop.hidden = false;
    menuButtons.forEach(item => item.setAttribute('aria-expanded','true'));
    document.querySelector('main').inert = true;
    document.querySelector('.mobile-bottom-nav').inert = true;
    sidebar.querySelector('[data-mobile-menu-close]').focus();
  };
  menuButtons.forEach(button => button.addEventListener('click', () => openMenu(button)));
  document.querySelectorAll('[data-mobile-menu-close]').forEach(button => button.addEventListener('click', () => closeMenu()));
  document.addEventListener('keydown',event => {
    if (!document.body.classList.contains('nav-open')) return;
    if (event.key === 'Escape') closeMenu();
    if (event.key === 'Tab') {
      const focusable = [...sidebar.querySelectorAll('a,button,summary')].filter(el => el.getClientRects().length);
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) {event.preventDefault();last.focus();}
      else if (!event.shiftKey && document.activeElement === last) {event.preventDefault();first.focus();}
    }
  });
  drawer.addEventListener('change', () => {
    closeMenu(false);
    sidebar.inert = drawer.matches || document.body.classList.contains('sidebar-collapsed');
  });
  if (drawer.matches) sidebar.inert = true;

  // Collapse only filter forms. Desktop forms remain open, with the same input nodes.
  document.querySelectorAll('form.filters').forEach(form => {
    if (form.method.toLowerCase() !== 'get') return;
    const disclosure = document.createElement('details');
    disclosure.className = 'mobile-filters';
    const summary = document.createElement('summary');
    summary.textContent = 'Ara ve Filtrele';
    form.before(disclosure);disclosure.append(summary,form);
    const apply = () => {disclosure.open = !phone.matches;};
    phone.addEventListener('change',apply);apply();
    const active = [...form.elements].filter(el => el.name && el.type !== 'hidden' && ((el.type === 'checkbox' || el.type === 'radio') ? el.checked : el.value.trim()));
    if (active.length) summary.textContent = 'Filtreler · ' + active.length + ' seçim';
  });

  const decorateTable = table => {
    if (table.dataset.mobileReady || !table.tHead || table.tHead.rows.length !== 1) return;
    const headings = [...table.tHead.rows[0].cells];
    if (headings.length < 2 || headings.some(th => th.colSpan !== 1 || th.rowSpan !== 1 || th.querySelector('input,select,button'))) return;
    table.dataset.mobileReady = '1';
    table.classList.add('mobile-card-table');
    table.parentElement.classList.add('mobile-card-wrap');
    const labels = headings.map(th => th.textContent.trim().replace(/[↑↓↕]/g,'').trim());
    // Preserve sorting links in the card layout too.
    const sortLinks = headings.flatMap(th => [...th.querySelectorAll('a[href]')]);
    if (sortLinks.length) {
      const tools = document.createElement('details');tools.className = 'mobile-sort-tools';
      const summary = document.createElement('summary');summary.textContent = 'Sıralama';tools.append(summary);
      const links = document.createElement('div');tools.append(links);
      sortLinks.forEach(link => {const copy = link.cloneNode(true);copy.classList.add('button');links.append(copy);});
      table.parentElement.before(tools);
      const originalTabs = sortLinks.map(link => link.getAttribute('tabindex'));
      const updateSortFocus = () => sortLinks.forEach((link,index) => {
        if (phone.matches) link.tabIndex = -1;
        else if (originalTabs[index] === null) link.removeAttribute('tabindex');
        else link.setAttribute('tabindex',originalTabs[index]);
      });
      phone.addEventListener('change',updateSortFocus);updateSortFocus();
    }
    const labelRows = () => [...table.tBodies].forEach(body => [...body.rows].forEach(row => {
      [...row.cells].forEach((cell,index) => {
        if (cell.colSpan > 1) {cell.classList.add('mobile-wide-cell');return;}
        const label = labels[index] || '';
        cell.dataset.mobileLabel = label;
        if (/^(Müşteri|Cari|Ürün|Açıklama|Not|Bağlı Sipariş|Fatura No|Teklif No)/i.test(label) || !label) cell.classList.add('mobile-wide-cell');
      });
    }));
    labelRows();
    new MutationObserver(labelRows).observe(table,{childList:true,subtree:true});
  };
  document.querySelectorAll('.table-wrap > table').forEach(decorateTable);
})();
