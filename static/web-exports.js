/* Browser file actions; desktop pywebview keeps its existing native handlers. */
(() => {
  const selector = 'a[data-native-download],a[data-native-share],a[data-native-whatsapp]';
  const methods = {download: 'save_export', share: 'share_export', whatsapp: 'whatsapp_export'};
  function download(file) {
    const url = URL.createObjectURL(file);
    const anchor = document.createElement('a');
    anchor.href = url; anchor.download = file.name;
    document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  function showFile(file, kind) {
    const dialog = document.createElement('dialog');
    dialog.className = 'web-export-dialog';
    const title = document.createElement('h2'); title.textContent = 'PDF hazır';
    const message = document.createElement('p'); message.setAttribute('role', 'status');
    const actions = document.createElement('div'); actions.className = 'web-export-actions';
    const save = document.createElement('button'); save.type = 'button'; save.textContent = 'PDF İndir';
    save.addEventListener('click', () => {download(file); message.textContent = 'İndirme isteği gönderildi. Dosyayı tarayıcınızın indirilenler bölümünden bulabilirsiniz.';});
    const close = document.createElement('button'); close.type = 'button'; close.textContent = 'Kapat';
    close.addEventListener('click', () => dialog.close());
    let canShare = false;
    try { canShare = !!(navigator.share && navigator.canShare && navigator.canShare({files: [file]})); } catch (_) {}
    if (canShare) {
      message.textContent = kind === 'whatsapp' ? 'Paylaşım menüsünden WhatsApp’ı, ardından alıcıyı seçin.' : 'PDF dosyasını göndermek istediğiniz uygulamayı seçin.';
      const share = document.createElement('button'); share.type = 'button'; share.className = 'primary';
      share.textContent = kind === 'whatsapp' ? 'WhatsApp için Paylaş' : 'PDF’yi Paylaş';
      share.addEventListener('click', async () => {
        share.disabled = true;
        try {
          // This click is a fresh user gesture: slow PDF fetches cannot expire it.
          await navigator.share({files: [file], title: file.name});
          dialog.close();
        } catch (error) {
          if (error.name !== 'AbortError') message.textContent = 'Paylaşım açılamadı. Yeniden deneyebilir veya PDF’yi indirip dosya olarak gönderebilirsiniz.';
        } finally { share.disabled = false; }
      });
      actions.append(share);
    } else {
      message.textContent = kind === 'whatsapp' ? 'Bu tarayıcı dosya paylaşımını desteklemiyor. PDF’yi indirin; WhatsApp’ta ataç / + düğmesinden Belge seçerek ekleyin.' : 'Bu tarayıcı dosya paylaşımını desteklemiyor. PDF’yi indirip istediğiniz uygulamaya dosya olarak ekleyebilirsiniz.';
    }
    actions.append(save, close); dialog.append(title, message, actions);
    dialog.addEventListener('close', () => dialog.remove(), {once: true});
    document.body.append(dialog); dialog.showModal();
  }
  document.addEventListener('click', async event => {
    const link = event.target.closest(selector);
    if (!link || event.defaultPrevented) return;
    const kind = link.hasAttribute('data-native-whatsapp') ? 'whatsapp' : link.hasAttribute('data-native-share') ? 'share' : 'download';
    if (window.pywebview?.api?.[methods[kind]]) return;
    event.preventDefault(); event.stopImmediatePropagation();
    if (link.dataset.webExportBusy === 'true') return;
    const original = link.textContent;
    link.dataset.webExportBusy = 'true'; link.textContent = 'Hazırlanıyor…'; link.setAttribute('aria-busy', 'true');
    const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 60000);
    try {
      const url = new URL(link.href, location.href);
      if (url.origin !== location.origin) throw new Error('Dosya bağlantısı geçersiz.');
      const response = await fetch(url.href, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
      if (response.redirected || response.status === 401 || response.status === 403) throw new Error('Oturumunuzu veya erişim yetkinizi kontrol edip tekrar giriş yapın.');
      if (!response.ok) throw new Error('Dosya hazırlanamadı. Lütfen tekrar deneyin.');
      const type = (response.headers.get('content-type') || '').split(';')[0].trim();
      if (!type || type === 'text/html' || type === 'application/json') throw new Error('Dosya yerine bir hata sayfası döndü. Sayfayı yenileyip tekrar deneyin.');
      const blob = await response.blob();
      if (!blob.size) throw new Error('İndirilecek dosya boş.');
      const file = new File([blob], link.dataset.exportName || 'Business-OS-dosya', {type});
      if (kind === 'download') download(file); else showFile(file, kind);
    } catch (error) {
      alert(error.name === 'AbortError' ? 'Dosyanın hazırlanması zaman aşımına uğradı. Tekrar deneyin.' : error.message || 'İşlem tamamlanamadı.');
    } finally {
      clearTimeout(timeout); link.dataset.webExportBusy = 'false'; link.textContent = original; link.removeAttribute('aria-busy');
    }
  });
})();
