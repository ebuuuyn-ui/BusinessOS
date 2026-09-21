# Web işlem kayıtları

Kayıtlar PostgreSQL'deki `public.business_audit_event` tablosunda tutulur.
Menü veya işlem geçmişi ekranı eklenmez. Yalnızca yönetici sorgulayabilir.

- Etkinleştirmeden sonraki INSERT / UPDATE / DELETE değişiklikleri kaydedilir.
- Kullanıcı adı + kalıcı kullanıcı kimliği, UTC zaman, işlem, tablo, birincil anahtar,
  önceki/yeni değerler, değişen alanlar, istek kimliği ve transaction kimliği saklanır.
- Aynı isteğin alt kayıtları `request_id` ile birlikte okunabilir.
- İşlem geri alınırsa onun geçmiş kaydı da geri alınır.
- Şifreler, şifre özetleri, oturum anahtarları ve dosya içerikleri kaydedilmez.
  Kullanıcı şifresi değişirse yalnızca `password_changed` işareti tutulur.
- Uygulama dışı SQL değişiklikleri kullanıcı tahmin edilmeden
  `Sistem / uygulama dışı` olarak kaydedilir.
- Sayfa görüntülemeleri, dosya indirmeleri ve başarısız giriş denemeleri bu
  değişiklik geçmişinin kapsamında değildir.
- Eski işlemler geriye dönük oluşturulmaz; kayıtlar otomatik temizlenmez.
- Uygulamada geçmiş değiştirme/silme yolu yoktur; veritabanı UPDATE / DELETE /
  TRUNCATE işlemleri de engellenir. Veritabanı sahibinin DDL yetkisine karşı
  harici bir değiştirilemez arşiv değildir.
- Masaüstü SQLite davranışı değişmez. Yeni model tablolarında kurulum tekrar
  çalıştırılmalı; durum kontrolü eksik tabloları saptar.

## Kurulum ve doğrulama

Yönetici oturumuyla `/yonetim/islem-kaydi/kurulum` adresinde bir defa
etkinleştirilir. İş tablolarının mevcut satırları değiştirilmez. Kurulum aynı
transaction içinde trigger'ları kurar; geçici tabloda ekleme/düzenleme/silme ve
kullanıcı doğrulaması yapıp test verilerini savepoint ile geri alır. Başarısız
kontrolde kurulum bütünüyle geri alınır. Başlangıçta otomatik DDL yapılmaz.

## Sonradan sorgulama

Yönetici oturumuyla GET `/yonetim/islem-kaydi/kayitlar` JSON döndürür.
Parametreler: `user`, `actor_id`, `table`, `operation`, `request_id`,
`record` (JSON birincil anahtar; örnek `{"id":88}`), `from` (dahil),
`to` (hariç), `after` (önceki sayfanın `next_after` değeri), `limit` (1–500).
Tarihler saat dilimli ISO 8601 olmalıdır; Türkiye saati için +03:00 kullanılır.
URL parametreleri standart URL kodlamasıyla hazırlanmalıdır.
Satırlar artan kayıt kimliğine göre sayfalanır. Yeni kullanıcılar bu yola erişemez.

## İzole test

`python -m unittest test_audit_log test_web_users test_web_auth`

PostgreSQL trigger'larını da çalıştırmak için ayrı bir geçici klasöre
`@electric-sql/pglite` yükleyip:
`PGLITE_MODULE=/absolute/path/node_modules/@electric-sql/pglite/dist/index.js python -m unittest test_audit_log`

Bu paket yalnızca test aracıdır; üretim bağımlılığı değildir.
