# BOS ortak arayüz düzeni

Ortak görünüm `static/ui.css`, açılır işlem menüleri `static/ui.js` üzerinden yönetilir.

- Normal düğmeler, tek satırlı girişler ve seçim alanları 44 px yüksekliğindedir. Satır silme/açma simgeleri ve onay kutuları ayrı ölçü kullanır.
- Sayfanın ana işlemi üst başlıktaki `header-actions` içinde görünür. Aynı işlem içerikte tekrar edilmez.
- İndirme ve ikincil işlemler `details.action-menu` içinde gruplanır. `summary` açık bir ad taşır; menü Escape veya dışarı tıklama ile kapanır.
- Filtre alanlarının üzerinde görünür etiket bulunur. `filters` satırları dar ekranda kırılır; `filter-actions` alanı düğmeleri hizalar.
- Uzun açıklama ve özetler `section-disclosure` veya `ui-help` ile gerektiğinde açılır. İşlemin sonucu veya gerekli uyarı gizlenmez.
- Temel metin, boşluk, kenarlık ve renk değişikliği için sayfa bazlı ek kurallar yerine ortak dosyayı düzenleyin. Yeni `!important` yükseklik kuralları eklemeyin.
- Değişiklikleri geniş ve dar ekranda, ayrıca sekmeli çalışma alanındaki iframe içinde kontrol edin. Açılır menülerin ve arama sonuçlarının kesilmediğini doğrulayın.

Bu düzenleme iş kayıtları, hesaplama ve entegrasyon davranışlarını değiştirmez.
