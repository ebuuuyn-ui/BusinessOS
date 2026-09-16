# Business OS v1 — Order Center

Microsoft, Google veya Power BI bağlantısı gerektirmeyen; müşteri, ürün ve sipariş yönetimi için bağımsız çalışan Türkçe web uygulaması.

## Özellikler

- Müşteri kartları ve müşteriye göre sipariş geçmişi
- DİA cari kart listesinden `.xlsx` müşteri ekleme/güncelleme ve mükerrer kayıt koruması
- Ürün kartları, varyant/renk, birim ve fiyat bilgileri
- DİA `.xlsx` ürün listesinden ürün ekleme/güncelleme ve mükerrer kayıt koruması
- Tek siparişte sınırsız sipariş kalemi
- Serbest ürün girişi veya kayıtlı ürün kartından otomatik doldurma
- Sipariş durumu ve zaman çizelgesi
- Sipariş numarası, müşteri ve durumla arama/filtreleme
- Cari hesap hareketleri, borç/alacak bakiyesi ve hesap ekstresi
- Fatura, yemek ve diğer giderler için aranabilir masraf modülü
- Ana sayfada günlük ve aylık masraf özeti
- Seçilen ürünlerden otomatik, yazdırılabilir ürün kataloğu ve fiyat listesi
- Fiyat listesini Excel uyumlu CSV olarak indirme
- Nakit tahsilat/ödeme, manuel kasa hareketleri ve anlık kasa bakiyesi
- Alınan/verilen çeklerde çek no, banka, vade ve durum takibi
- Ana sayfadan hızlı nakit, çek veya banka tahsilatı girişi
- Telefon, tablet ve bilgisayara uyumlu arayüz
- Yerel SQLite veritabanı; harici servis bağımlılığı yok

## Kurulum

Bilgisayarda Python 3.10 veya daha yeni bir sürüm bulunmalıdır.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

Tarayıcıdan `http://127.0.0.1:5000` adresini açın. İlk açılışta veritabanı otomatik olarak `instance/business_os.db` dosyasında oluşturulur.

Windows'ta etkinleştirme komutu:

```powershell
.venv\Scripts\activate
```

Windows'ta ilk kurulum için PowerShell'de:

```powershell
powershell -ExecutionPolicy Bypass -File .\launchers\setup_windows.ps1
```

Daha sonraki açılışlarda `launchers\start_windows.bat` dosyasına çift
tıklamak yeterlidir. Başlatıcı Git işlemi yapmaz; veritabanını pull/push
etmeye çalışmaz.

## Veriler ve yedekleme

Kaynak koddan varsayılan başlatmada veritabanı `instance/business_os.db` dosyasıdır. macOS masaüstü uygulaması ise `~/Library/Application Support/Business OS/business_os.db` dosyasını kullanır. Bu iki veritabanı aynı olmayabilir. `BUSINESSOS_DATA_DIR` veri klasörünü, `DATABASE_URL` bağlantıyı değiştirebilir.

Veri aktarımından önce aktif veritabanı doğrulanmalı ve tutarlı SQLite yedeği alınmalıdır. Belge ekleri ve yerel ayarlar da ilgili veri klasöründe ayrıca korunmalıdır. Veritabanları ve yerel ayarlar GitHub üzerinden taşınmaz.

Uygulama ayrıca her başlatıldığında ve müşteri aktarımı/silme gibi önemli işlemlerden önce `instance/backups/` klasörüne tarih-saatli otomatik SQLite yedeği oluşturur. Program kodu güncellenirken bu veritabanı ve yedek klasörü silinmez.

Günlük dış yedek klasörü varsayılan olarak kullanıcının Belgeler klasöründeki
`Business OS Yedekleri` dizinidir. Windows'ta farklı bir disk kullanmak için
`BUSINESSOS_BACKUP_DIR` ortam değişkeni ayarlanabilir. Örneğin:

```powershell
$env:BUSINESSOS_BACKUP_DIR = "D:\Business OS Yedekleri"
```

`*.db`, `*.sqlite` ve `*.sqlite3` dosyaları Git dışında tutulur. Bu dosyalar
Mac ile Windows arasında commit/push/pull yoluyla taşınmamalıdır. İlk Windows
aktarımı, uygulama kapatıldıktan sonra doğrulanmış bir SQLite yedeğiyle ayrı ve
tek yönlü yapılmalıdır.

## PostgreSQL'e geçiş

Veri katmanı SQLAlchemy ile oluşturulmuştur. İleride PostgreSQL sürücüsü eklenip bağlantı adresi ortam değişkeniyle değiştirilebilir:

```bash
export DATABASE_URL="postgresql+psycopg://kullanici:sifre@sunucu/veritabani"
python app.py
```

Üretim ortamında güçlü bir `SECRET_KEY` tanımlanmalı, hata ayıklama modu kapatılmalı ve Waitress/Gunicorn gibi bir uygulama sunucusu kullanılmalıdır.

## Eylül 2026 masaüstü sürümü

- Faturalar, cari hesap etkileri ve faturaya bağlı stok hareketleri.
- Stoklarda Kod/Ürün filtreleri, Türkçe ve doğal sayısal sıralama.
- Müşteri bazlı tahsilat takibi, cari raporları ve PDF/Excel çıktıları.
- Satın alma sevkiyat bilgileri, belge ekleri, Telegram ve e-arşiv belge ekranları.
- Kart tahsilatında aranabilir tedarikçi seçimi ve dar forma uyumlu yerleşim.

macOS paketi için `requirements-build.txt` bağımlılıklarıyla `python -m PyInstaller "Business OS.spec"` çalıştırılır. Çıktı `dist/Business OS.app` altındadır; GitHub yalnız kaynakları içerir, kurulu uygulamayı otomatik güncellemez.

Bilinen eksik: `/ozon-satislari` rotasının beklediği `templates/ozon_sales.html` güncel kaynak klasöründe bulunmuyor. Ozon ekranı doğrulanmış bir özellik olarak kabul edilmemelidir.

## Web giriş koruması (Vercel / PostgreSQL)

Vercel ortamında veya PostgreSQL bağlantısıyla giriş zorunludur. Yerel SQLite
başlatmasında giriş ekranı eklenmez. Aşağıdaki değişkenler Vercel proje ayarlarında
Production ve kullanılan Preview ortamları için tanımlanmalıdır:

- `BUSINESSOS_WEB_USERNAME`: sahibin seçtiği kullanıcı adı.
- `BUSINESSOS_WEB_PASSWORD`: parola yöneticisiyle üretilmiş, en az 16 karakterli benzersiz şifre.
- `SECRET_KEY`: parola yöneticisiyle üretilmiş, şifreden farklı, en az 32 karakterli rastgele değer.

Değerleri kaynak koda, GitHub'a veya sohbet mesajına yazmayın. Vercel'de hassas
ortam değişkeni olarak saklayın. Değişkenleri kaydettikten sonra yeniden deploy edin.
Ayarlar eksik veya çok kısaysa tüm web istekleri 503 kurulum ekranıyla engellenir.
`/giris` sayfasıyla giriş yapılır; sekiz saat sonra oturum sona erer. Şifre veya
SECRET_KEY değişikliği ve yeniden deploy mevcut oturumları geçersiz kılar.
Kenar menüsündeki Çıkış yap düğmesi oturumu kapatır.

HTTPS zorunludur. Oturum çerezi Secure, HttpOnly, SameSite=Strict kullanır.
Değişiklik yapan web istekleri aynı HTTPS origin bilgisini göndermelidir;
origin bilgisi olmayan veya başka siteden gelen POST/PUT/PATCH/DELETE istekleri
reddedilir. Giriş formu ayrıca CSRF belirteci kullanır. Yanıtlar önbelleğe alınmaz.
Bu tek sahipli giriş çözümü kullanıcı yönetimi, MFA veya dağıtık giriş denemesi
sınırlaması sağlamaz. Eski, korumasız Vercel yayınlarının adreslerini ayrıca
kapatın veya Vercel erişim korumasına alın; yeni kod eski yayınları değiştirmez.
Gerçek veri aktarımından önce üretim, preview ve eski yayın erişimlerini doğrulayın.

Doğrulama: `python -m unittest test_web_auth -v` canlı veritabanı kullanmaz.

## Web başlangıcı ve veritabanı hazırlığı

Vercel veya PostgreSQL ile çalışan web sürümü başlangıçta tablo oluşturmaz,
şema kontrolü yapmaz ve eski yerel Ödeme Takibi verilerini aktarmaya çalışmaz.
Yerel SQLite yedek klasörleri web isteklerinde taranmaz. Mevcut web veritabanı
olduğu gibi kullanılır; bu değişiklik için canlı veritabanında komut çalıştırmak gerekmez.

Yalnız **yeni, boş bir web veritabanı kurulurken**, bağlantı ortam değişkenleri
hazırlandıktan sonra şu komut bir kez çalıştırılabilir:

```bash
python -m flask --app app init-db
```

Bu komut eksik tabloları oluşturur; mevcut kayıtları silmez veya sıfırlamaz.
Mevcut tablo sütunlarını değiştiren bir migration aracı değildir. Gelecekteki
şema değişiklikleri ayrı, incelenmiş bir migration adımıyla uygulanmalıdır.
Masaüstü SQLite uygulamasının başlangıç yedeği, uyumluluk kontrolleri ve eski
veri aktarım davranışı korunur. Web PostgreSQL yedekleri barındırma/veritabanı
sağlayıcısı üzerinden ayrıca yönetilmelidir; yerel SQLite yedeği değildir.
