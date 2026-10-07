"""Controlled document labels; user-authored text is never silently translated."""
LABELS = {
 'Cari Kartından Seç':'Select Customer', 'Cari Adı veya Kodu':'Customer Name or Code', 'Hazırlayan':'Prepared By',
 '← Fiyat Tekliflerine Dön':'← Back to Quotations','← Teklife Dön':'← Back to Quotation','Excel İndir':'Download Excel',
 'FİYAT TEKLİFİ':'PRICE QUOTATION', 'TEKLİF KOŞULLARI':'QUOTATION TERMS',
 'Müşteri':'Customer','Müşteri Kodu':'Customer Code','Adres':'Address','Yetkili':'Contact Person',
 'Teklif Tarihi':'Quotation Date','Firma':'Company','Hazırlayan':'Prepared By','E-Posta':'Email','Telefon':'Phone',
 'Ürün Adı / Kodu':'Product Name / Code','Ürün Adı':'Product Name','Ürün Görseli':'Product Image',
 'Birim Fiyatı':'Unit Price','İskonto':'Discount','İskontolu Birim Fiyatı':'Discounted Unit Price','Adet':'Quantity','Toplam':'Total',
 'Liste Toplamı':'List Total','Toplam İskonto':'Total Discount','Ara Toplam (KDV Hariç)':'Subtotal (Excl. VAT)',
 'KDV Tutarı':'VAT Amount','Genel Toplam':'Grand Total','Görsel Yok':'No Image',
 'Birim fiyatlar ve satır toplamları KDV hariçtir. Teslimat ve diğer teklif koşulları sonraki sayfadadır.':'Unit prices and line totals exclude VAT. Delivery and other quotation terms are on the following page.',
 'Teslimat':'Delivery','Ödeme':'Payment','Süre':'Lead Time','Garanti':'Warranty','İade':'Returns','Teklif Geçerlilik Süresi':'Quotation Validity',
 'Teklif Bilgileri':'Quotation Details','Müşteri / Firma Adı':'Customer / Company Name','Teklif Para Birimi':'Quotation Currency',
 'USD — Amerikan Doları':'USD — US Dollar','USD Kuru (1 USD = TL)':'USD Exchange Rate (1 USD = TRY)',
 'Teklifte kullanılacak kuru girin. Kur bu teklif için saklanır.':'Enter the exchange rate. It is stored for this quotation.',
 'Ürün Kalemleri':'Product Items','Teklif Koşulları':'Quotation Terms','PDF Önizleme':'PDF Preview','Teklifi Kaydet':'Save Quotation',
 '+ Ürün Ekle':'+ Add Product','Ürün Kalemi':'Product Item','Kalemi Kaldır':'Remove Item','Stok Kartı':'Stock Item',
 'Ürün Detayı':'Product Description','Fiyat Giriş Yöntemi':'Price Entry Method','İskonto Gir':'Enter Discount',
 'TL Satış Fiyatı Gir':'Enter TRY Selling Price','USD Satış Fiyatı Gir':'Enter USD Selling Price',
 'İskonto (%)':'Discount (%)','KDV (%)':'VAT (%)','Birim Fiyatı (₺)':'Unit Price (TRY)',
 'İskontolu Birim Fiyatı (₺)':'Discounted Unit Price (TRY)','İskontolu Birim Fiyatı (USD)':'Discounted Unit Price (USD)',
 'Ürün Adı veya Kodu Yazın':'Enter Product Name or Code','Görsel Eklenmemiş':'No Image Added','Görsel Yükle':'Upload Image',
 'Görsel stok kartına kaydedilir.':'The image is saved to the stock item.',
 'Belge Bilgileri':'Document Details','Gönderici Firma':'Exporter Company','Gönderici Adresi':'Exporter Address','Gönderici Telefonu':'Exporter Phone',
 'Alıcı Firma':'Consignee Company','Teslimat Adresi':'Delivery Address','Alıcı Telefonu':'Consignee Phone',
 'Fatura / Teklif No':'Invoice / Quotation No','Belge Tarihi':'Document Date','Konteyner No (İsteğe Bağlı)':'Container No (Optional)',
 'Mühür No (İsteğe Bağlı)':'Seal No (Optional)','Paket Kalemleri':'Package Items','Teklif Ürünü / Model No':'Quotation Product / Model No',
 'Ürün Adedi':'Quantity','Koli Sayısı':'Cartons','Brüt kg':'Gross kg','Net kg':'Net kg','Paket Ölçüsü cm':'Package Dimensions cm',
 'Boy × En × Yükseklik':'Length × Width × Height','Toplam m³':'Total m³','Boy':'Length','En':'Width','Yükseklik':'Height',
 '+ Paket Satırı Ekle':'+ Add Package Row','Belge Notu':'Document Note','Packing Listi Kaydet':'Save Packing List',
 'Ürün adı ve açıklamayı İngilizce yazın. Net / brüt ağırlık ve CBM her satırın toplamıdır. Paket ölçüsü ambalajın dış ölçüsüdür; aynı satırdaki koliler aynı ölçüde olmalıdır.':'Enter product names and descriptions in English. Net/gross weights and CBM are totals per row. Dimensions are external package dimensions; cartons on the same row must have the same dimensions.',
 'Kaydettikten sonra İngilizce PDF ve Excel indirebilirsiniz. Bu belge stok veya cari hareketi oluşturmaz.':'After saving, download the English PDF and Excel. This document does not create stock or accounting entries.',
 'Fiyatlar KDV hariçtir. TL birim fiyatına iskonto uygulayıp USD’ye çevirebilir veya doğrudan USD satış fiyatı girebilirsiniz. Stoktan gelen ad ve birim fiyatı bu teklif için değiştirebilirsiniz.':'Prices exclude VAT. Apply a discount to the TRY price and convert to USD, or enter a USD selling price directly. Product names and unit prices can be edited for this quotation.',
}
def language(value):
    if value not in ('tr','en'):raise ValueError('Belge dili Türkçe veya İngilizce olmalıdır.')
    return value

def label(text,lang='tr'):return LABELS.get(text,text) if lang=='en' else text

for _term in ("Teslimat","Ödeme","Süre","Garanti","İade","Teklif Geçerlilik Süresi"):
    LABELS[_term+" koşullarını yazın"]="Enter "+LABELS[_term].lower()+" terms"

def document_type(data):
    return data.get("document_type", "proforma" if data.get("language")=="en" and data.get("currency")=="USD" else "quotation")

LABELS.update({"Teklif Türü":"Document Type","Yurt İçi Fiyat Teklifi":"Domestic Quotation","Yurtdışı / Proforma Invoice":"Export / Proforma Invoice"})
