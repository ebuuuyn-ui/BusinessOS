(() => {
  const picker = document.currentScript.previousElementSibling;
  const form = picker.closest('form');
  const search = picker.querySelector('.shipment-source-search');
  const results = picker.querySelector('.shipment-source-results');
  const options = [...results.querySelectorAll('button')];
  const copy = picker.querySelector('.shipment-copy-button');
  const status = picker.querySelector('[role="status"]');
  let selectedId = copy.dataset.sourceId;
  const normalize = value => String(value).toLocaleLowerCase('tr-TR');
  const filter = () => {
    const terms = normalize(search.value).split(/\s+/).filter(Boolean);
    options.forEach(option => option.hidden = !terms.every(term => normalize(option.dataset.search).includes(term)));
    results.querySelector('.no-customer').style.display = options.some(option => !option.hidden) ? 'none' : 'block';
    results.classList.add('open');
  };
  search.addEventListener('focus', filter);
  search.addEventListener('input', () => { selectedId = ''; filter(); });
  search.addEventListener('keydown', event => {
    if (event.key === 'Escape') results.classList.remove('open');
    if (event.key === 'Enter') { event.preventDefault(); options.find(option => !option.hidden)?.focus(); }
    if (event.key === 'ArrowDown') { event.preventDefault(); options.find(option => !option.hidden)?.focus(); }
  });
  options.forEach(option => option.addEventListener('click', () => {
    selectedId = option.dataset.id;
    search.value = option.querySelector('b').textContent;
    results.classList.remove('open');
    status.textContent = 'Bilgileri aktarmak için Sevkiyat Bilgilerini Ekle düğmesine basın.';
  }));
  document.addEventListener('click', event => { if (!picker.contains(event.target)) results.classList.remove('open'); });
  copy.addEventListener('click', () => {
    const option = options.find(option => option.dataset.id === selectedId);
    if (!option) { status.textContent = 'Önce listeden bir cari seçin.'; search.focus(); return; }
    const mapping = { shipment_contact: 'contact', shipment_phone: 'phone', delivery_city: 'city', shipment_address: 'address', shipment_note: 'note' };
    if (!Object.values(mapping).some(key => option.dataset[key].trim())) {
      status.textContent = 'Bu caride kayıtlı sevkiyat bilgisi yok. Alanları elle doldurabilirsiniz.';
      return;
    }
    const fields = Object.entries(mapping).map(([name, key]) => [form.elements.namedItem(name), option.dataset[key]]);
    if (fields.some(([field, value]) => field.value.trim() && field.value !== value) && !confirm('Siparişteki mevcut sevkiyat bilgileri seçilen carinin bilgileriyle değiştirilsin mi?')) return;
    fields.forEach(([field, value]) => { field.value = value; field.dispatchEvent(new Event('input', { bubbles: true })); });
    status.textContent = 'Sevkiyat bilgileri eklendi. Siparişi kaydetmeden önce kontrol edebilirsiniz.';
  });
})();
