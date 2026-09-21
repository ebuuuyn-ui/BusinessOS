# ABİKA tedarikçi tablosu bağlantısı

Hedef: 1FNYd6lkPpKQeps-MyRbwKxd-9Qkl4eg9OdRBLSUxs-Q, gid=0.
Yalnızca customer_id=1339 olan Satın Alma siparişleri. Kullanıcı önce fiyatsız
önizlemeyi inceler, sonra aktarır. Chat PDF gönderimi otomatik izlenmez.

Her sipariş kalemi sabit bir anahtar taşır. Tekrar aktarım mevcut satırları
günceller. Başka siparişlere dokunulmaz. Silinen kalemin A:X değerleri temizlenir.
Ürün, miktar, tarihler, müşteri ve sevkiyat bilgileri, notlar aktarılır.
Fiyat, maliyet, iskonto, KDV ve tutar alanları gönderilmez. Serbest metne elle
yazılan bilgiler önizlemede kontrol edilmelidir.

## Kimlik doğrulama
JSON anahtarı kullanılmaz. Vercel isteğinin x-vercel-oidc-token başlığı Google
Workload Identity Federation ile hizmet hesabına dönüştürülür.

Google projesi: august-edge-509315-r9 (164533587659).
Hizmet hesabı: businessos-supplier-sheets@august-edge-509315-r9.iam.gserviceaccount.com
Bu hesap yalnızca hedef dosyanın düzenleyicisidir; proje IAM rolü verilmez.

Vercel production ortam değişkeni (gizli değildir):
BUSINESSOS_GOOGLE_WIF_AUDIENCE=//iam.googleapis.com/projects/164533587659/locations/global/workloadIdentityPools/businessos-production/providers/vercel

Sağlayıcı issuer: https://oidc.vercel.com/ebuu
Allowed audience: https://vercel.com/ebuu
Mapping: google.subject=assertion.sub
Koşul:
assertion.sub == 'owner:ebuu:project:business-os:environment:production' && assertion.project_id == 'prj_9z9EeCQpReG3eSYHM2DFdtclq1FK' && assertion.owner_id == 'team_cS4xeF7gg7UlYTt3rsPAKsWL'

Hizmet hesabında yalnızca aşağıdaki kimliğe roles/iam.workloadIdentityUser verilir:
principal://iam.googleapis.com/projects/164533587659/locations/global/workloadIdentityPools/businessos-production/subject/owner:ebuu:project:business-os:environment:production

## Doğrulama
Birim testleri sentetik veriler ve mock API kullanır. Rota testleri geçici
in-memory SQLite kullanır; canlı DB'ye bağlanmaz.
Gerçek bağlantı ayrı doğrulanmalıdır. Canlı aktarım sonrası API yazılan satırları
geri okuyarak doğrular. Başarı OrderHistory ve mevcut audit kaydına yazılır.

Owner bağlantı kontrolü: GET /yonetim/tedarikci-tablosu/kontrol. Bu işlem yalnızca
hedef sekme ve başlıkları okur; sipariş veya tablo verisi değiştirmez.

2026-09-21 canlı doğrulama: SA-2026-00046 (order 89), 2 kalem / 6 adet,
kullanıcının açık onayıyla aktarıldı. API geri okuma ve Sheets görünümünde doğrulandı.
Google anahtar oluşturma politikası değiştirilmedi. WIF canlı proje koşulu ve
servis hesabı subject yetkisi etkinleştirildi; Vercel production config kaydedildi.

Sipariş Durumu (E) ilk aktarımda uygulamadan gelir; tekrar aktarımda A:D ve F:X güncellenir, E yazılmaz. Tedarikçi satır bazında düzenler; uygulamaya geri senkronizasyon yoktur.
