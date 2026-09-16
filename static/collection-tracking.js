(() => {
  document.querySelectorAll('.collection-detail-toggle[data-detail-url]').forEach(button => {
    const row = document.getElementById(button.getAttribute('aria-controls'));
    const content = row.querySelector('.collection-detail-content');
    async function load() {
      button.disabled = true;
      row.setAttribute('aria-busy', 'true');
      content.textContent = 'Kayıtlar yükleniyor…';
      try {
        const response = await fetch(button.dataset.detailUrl, {credentials: 'same-origin', cache: 'no-store'});
        if (!response.ok || response.redirected || response.headers.get('X-BusinessOS-Fragment') !== 'maturity-detail') {
          throw new Error('Unable to load details');
        }
        content.innerHTML = await response.text();
      } catch (_) {
        content.textContent = 'Kayıtlar yüklenemedi. Oturumunuz kapandıysa sayfayı yenileyip giriş yapın. ';
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'button small';
        retry.textContent = 'Tekrar dene';
        retry.addEventListener('click', load);
        content.append(retry);
      } finally {
        button.disabled = false;
        row.removeAttribute('aria-busy');
      }
    }
    button.addEventListener('click', () => {
      row.hidden = !row.hidden;
      button.setAttribute('aria-expanded', String(!row.hidden));
      if (!row.hidden) load();
    });
  });
})();
