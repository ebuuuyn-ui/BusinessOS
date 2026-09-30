(() => {
  const form = document.getElementById('trello-export-all');
  if (!form) return;
  const box = document.getElementById('trello-export-progress');
  const progress = box.querySelector('progress');
  const status = box.querySelector('p');
  const button = form.querySelector('button');
  let running = false;
  const warn = event => { event.preventDefault(); event.returnValue = ''; };
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (running) return;
    running = true;
    button.disabled = true;
    box.hidden = false;
    let completed = Number(form.dataset.completed);
    const total = Number(form.dataset.total);
    window.addEventListener('beforeunload', warn);
    try {
      while (completed < total) {
        status.textContent = `${completed} / ${total} kalem aktarıldı. Aktarım sürüyor; bu sekmeyi açık tutun.`;
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 90000);
        let response;
        try {
          response = await fetch(form.action || location.href, {
            method: 'POST', body: new FormData(form), credentials: 'same-origin',
            headers: { Accept: 'application/json' }, signal: controller.signal
          });
        } finally { clearTimeout(timer); }
        if (!response.headers.get('content-type')?.includes('application/json')) {
          throw new Error('Oturum veya bağlantı kesildi. Sayfayı yenileyerek aktarım durumunu kontrol edin.');
        }
        const result = await response.json();
        progress.value = result.completed;
        if (!response.ok || result.error) throw new Error(result.error || 'Aktarım durduruldu. Sayfayı yenileyin.');
        if (result.total !== total || result.completed <= completed) {
          throw new Error('Aktarım ilerlemedi. Sayfayı yenileyerek kalemleri kontrol edin.');
        }
        completed = result.completed;
        form.dataset.completed = String(completed);
      }
      status.textContent = `${total} / ${total} kalem aktarıldı. Tüm kartlar hazır.`;
      button.textContent = 'Aktarım tamamlandı';
      window.removeEventListener('beforeunload', warn);
      location.reload();
    } catch (error) {
      status.textContent = error.name === 'AbortError' || error instanceof TypeError
        ? 'Bağlantı kesildi. Otomatik tekrar gönderim durduruldu. Sayfayı yenileyip kartların durumunu kontrol edin.'
        : error.message;
      button.textContent = 'Kalanları Aktar';
      button.disabled = false;
    } finally {
      running = false;
      window.removeEventListener('beforeunload', warn);
    }
  });
})();
