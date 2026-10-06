# BOS ortak arayüz düzeni

Ortak görünüm `static/ui.css`, sıkı çalışma düzeni `static/density.css`, açılır işlem menüleri `static/ui.js` üzerinden yönetilir.

- Masaüstünde normal düğmeler, tek satırlı girişler ve seçim alanları 28 px; liste içindeki kontroller 24–26 px yüksekliğindedir. Telefonda giriş ve dokunma alanları 44 px kalır. Masaüstü düğme metni 11 px, cari ekstre tarih alanı 124 px genişliğindedir. Ölçüyü ortak `--control-height` değişkeninden alın.
- Masaüstü liste metni 12 px, sütun başlıkları 11 px kullanır. Standart satır yaklaşık 30–34 px yüksekliğindedir; çok satırlı gerçek içerik için satır büyüyebilir. Satır ve sütun çizgileri belirgindir, uygun listelerde dönüşümlü zemin vardır.
- Başlık, filtre, form ve panel aralıklarını küçük tutun; geniş ekranı tam kullanın. Bu yoğunluk tercihi sipariş, fatura, cari, stok, teklif ve finans alanlarının tamamı için geçerlidir. PDF/Excel belge tasarımları bu ekran kurallarından bağımsızdır.
- Sayfanın ana işlemi üst başlıktaki `header-actions` içinde görünür. Aynı işlem içerikte tekrar edilmez.
- `header-actions`, `detail-navigation` ve `actions` içindeki düğmeler masaüstünde 160 px eşit genişlik kullanır. Uzun metin sarılır ve aynı satırdaki düğmeler birlikte uzar. Telefonda eşit iki sütun kullanılır; son düğme tek başına kalsa da genişlemez. Filtre düğmeleri masaüstünde 72 px genişliğindedir. Simge düğmeleri ve açılır menünün içindeki seçenekler bu genişlik kuralına girmez.
- İndirme ve ikincil işlemler `details.action-menu` içinde gruplanır. `summary` açık bir ad taşır; menü Escape veya dışarı tıklama ile kapanır.
- Filtre alanlarının üzerinde görünür etiket bulunur. `filters` satırları dar ekranda kırılır; `filter-actions` alanı düğmeleri hizalar.
- Uzun açıklama ve özetler `section-disclosure` veya `ui-help` ile gerektiğinde açılır. İşlemin sonucu veya gerekli uyarı gizlenmez.
- Cari hesap ekstresi tam genişlik kullanır; yeni hareket formu tablonun üstünde kapalı bir disclosure içinde saklanır. Açıldığında geniş masaüstünde temel alanlar ve küçük kaydet düğmesi tek yatay satır kullanır; ödeme, çek ve kart alanları gerektiğinde ince çizgiyle ayrılmış alt satırda açılır. Bu form da ortak 28 px kontrol ölçüsünü kullanır; sabit 44 px zorlayan eski kurallar kullanılmaz. Masaüstünde hareketler 28 px tek satırdır; tarih, referans ve tutarlar bölünmez. Uzun açıklama ve ödeme ayrıntıları doğal `details/summary` ile açılır. Yazdırmada ayrıntılar açılıp sonrasında önceki ekran durumu geri yüklenir; PDF/Excel dışa aktarma verileri değişmez.
- Temel metin, boşluk, kenarlık ve renk değişikliği için sayfa bazlı ek kurallar yerine ortak dosyayı düzenleyin. Yeni `!important` yükseklik kuralları eklemeyin.
- Değişiklikleri geniş ve dar ekranda, ayrıca sekmeli çalışma alanındaki iframe içinde kontrol edin. Açılır menülerin ve arama sonuçlarının kesilmediğini doğrulayın.

Bu düzenleme iş kayıtları, hesaplama ve entegrasyon davranışlarını değiştirmez.

## Telefon düzeni

`static/mobile.css` ve `static/mobile.js`, aynı formları ve kayıtları telefon için yeniden yerleştirir.

- 700 px ve altında basit başlıklı tablolar etiketli kartlara dönüşür. Müşteri/ürün adı gizlenmez; tutar ve durum ayrı alanlarda gösterilir. Karmaşık başlıklı tablolar kendi alanında yatay kayar.
- Kart başlıkları `thead` içeriğinden alınır. Sıralama bağlantıları telefon görünümünde ayrı bir açılır alana taşınır. Filtrelenmiş veya kapalı ayrıntı satırlarının `hidden` niteliği korunur.
- GET filtreleri telefonda açılır; masaüstünde görünür kalır. Alan ve olay dinleyicilerini kopyalamayın.
- Formlar tek sütuna, sipariş/fatura kalemlerinin sayısal alanları iki sütuna yerleşir. Giriş metni en az 16 px, dokunma düğmeleri en az 44 px kullanır.
- Alt gezinme Ana Sayfa, Siparişler, Cariler ve Menü bağlantılarını sunar. Açılır yan menü Escape, kapatma düğmesi veya arka plana dokunmayla kapanır; odağı içinde tutar ve kapanınca geri verir.
- Güvenli ekran kenarlarını (`safe-area-inset-bottom`) ve ekran klavyesini hesaba katın. Mobil doğrudan gezinmede masaüstünün boş sekme çubuğunu göstermeyin.
- 320, 360 ve 390 px telefon genişliklerinde belge taşmasını; filtreleme, satır açma, arama ve menü davranışlarını doğrulayın. Masaüstü iframe görünümünü ayrıca kontrol edin. Tarayıcı dar ekran kontrolü, gerçek iOS/Android cihaz testi yerine geçmez.
