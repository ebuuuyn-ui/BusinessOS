// Column filters operate on every rendered stock card, without changing stock data.
(() => {
  const table = document.querySelector('.stock-table');
  if (!table) return;
  const code = document.getElementById('stock-code-filter');
  const name = document.getElementById('stock-name-filter');
  const clear = document.getElementById('stock-filter-clear');
  const count = document.getElementById('stock-filter-count');
  const empty = document.getElementById('stock-filter-empty');
  const normalize = value => String(value || '').trim().normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '').replace(/[Iİı]/g, 'i').toLowerCase();
  const rows = Array.from(table.tBodies[0].rows, row => ({
    row,
    code: normalize(row.cells[0].textContent === '—' ? '' : row.cells[0].textContent),
    name: normalize(row.querySelector('.stock-card-link b').textContent),
  }));
  const params = new URLSearchParams(location.search);
  code.value = params.get('code_prefix') || '';
  name.value = params.get('product_name') || '';
  const fields = ['code_prefix', 'product_name'].map(name => ({name, value: ''}));
  function apply() {
    const prefix = normalize(code.value), fragment = normalize(name.value);
    let visible = 0;
    rows.forEach(item => {
      const matches = item.code.startsWith(prefix) && item.name.includes(fragment);
      item.row.hidden = !matches;
      if (matches) visible++;
    });
    const filtered = Boolean(prefix || fragment);
    count.textContent = filtered ? `${visible} / ${rows.length} ürün` : `${rows.length} ürün`;
    empty.hidden = visible > 0;
    clear.hidden = !filtered;
    const values = [code.value.trim(), name.value.trim()];
    const url = new URL(location.href);
    fields.forEach((field, index) => {
      field.value = values[index];
      if (values[index]) url.searchParams.set(field.name, values[index]);
      else url.searchParams.delete(field.name);
    });
    history.replaceState(history.state, '', url);
    // Keep the column filters when sorting quantities or inspecting a stock card.
    document.querySelectorAll('.stock-sort, .stock-card-link, #stock-history .panel-head a').forEach(link => {
      const target = new URL(link.href);
      fields.forEach(field => {
        if (field.value) target.searchParams.set(field.name, field.value);
        else target.searchParams.delete(field.name);
      });
      link.href = target.href;
    });
  }
  code.addEventListener('input', apply);
  name.addEventListener('input', apply);
  clear.addEventListener('click', () => { code.value = ''; name.value = ''; apply(); code.focus(); });
  apply();
})();
