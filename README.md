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

## Veriler ve yedekleme

Uygulama kapalıyken `instance/business_os.db` dosyasını başka bir diske kopyalamak tam yedek almak için yeterlidir. Bu dosya `.gitignore` içinde tutulur; yanlışlıkla kaynak kodla paylaşılmaz.

Uygulama ayrıca her başlatıldığında ve müşteri aktarımı/silme gibi önemli işlemlerden önce `instance/backups/` klasörüne tarih-saatli otomatik SQLite yedeği oluşturur. Program kodu güncellenirken bu veritabanı ve yedek klasörü silinmez.

## PostgreSQL'e geçiş

Veri katmanı SQLAlchemy ile oluşturulmuştur. İleride PostgreSQL sürücüsü eklenip bağlantı adresi ortam değişkeniyle değiştirilebilir:

```bash
export DATABASE_URL="postgresql+psycopg://kullanici:sifre@sunucu/veritabani"
python app.py
```

Üretim ortamında güçlü bir `SECRET_KEY` tanımlanmalı, hata ayıklama modu kapatılmalı ve Waitress/Gunicorn gibi bir uygulama sunucusu kullanılmalıdır.
