"""Uyumsoft SOAP integration: draft creation only, never fiscal submission."""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import xml.etree.ElementTree as ET
import requests

T = 'http://tempuri.org/'
S = 'http://schemas.xmlsoap.org/soap/envelope/'
W = 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd'
U = 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd'
B = 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'
A = 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2'
I = 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2'
ENDPOINTS = {'test':'https://efaturaws-test.uyum.com.tr/Services/Integration',
             'live':'https://edonusumapi.uyum.com.tr/Services/Integration'}
for prefix, ns in [('s',S),('t',T),('o',W),('u',U),('cbc',B),('cac',A),('inv',I)]: ET.register_namespace(prefix,ns)

class UyumError(Exception): pass

def add(parent, ns, name, value=None, **attrs):
    e=ET.SubElement(parent, '{'+ns+'}'+name, attrs)
    if value is not None: e.text=str(value)
    return e

def money(value): return Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
def amount(parent, name, value): return add(parent,B,name,format(money(value),'.2f'),currencyID='TRY')

def party(parent, kind, data):
    p=add(add(parent,A,kind),A,'Party')
    add(add(p,A,'PartyIdentification'),B,'ID',data['tax_number'],schemeID='VKN' if len(data['tax_number'])==10 else 'TCKN')
    add(add(p,A,'PartyName'),B,'Name',data['name'])
    address=add(p,A,'PostalAddress')
    add(address,B,'StreetName',data['address']);add(address,B,'CitySubdivisionName',data['district'])
    add(address,B,'CityName',data['city']);add(add(address,A,'Country'),B,'Name','Türkiye')
    add(add(add(p,A,'PartyTaxScheme'),A,'TaxScheme'),B,'Name',data['tax_office'])
    if len(data['tax_number'])==11:
        names=data['name'].split()
        if len(names)<2: raise UyumError('Şahıs için ad ve soyad gereklidir.')
        person=add(p,A,'Person');add(person,B,'FirstName',' '.join(names[:-1]));add(person,B,'FamilyName',names[-1])


def invoice_xml(data, uuid, einvoice):
    root=ET.Element('{'+I+'}Invoice')
    add(root,B,'UBLVersionID','2.1');add(root,B,'CustomizationID','TR1.2')
    add(root,B,'ProfileID','TEMELFATURA' if einvoice else 'EARSIVFATURA')
    add(root,B,'ID','');add(root,B,'CopyIndicator','false');add(root,B,'UUID',uuid)
    add(root,B,'IssueDate',data['date']);add(root,B,'InvoiceTypeCode','SATIS')
    if data.get('notes'): add(root,B,'Note',data['notes'])
    add(root,B,'DocumentCurrencyCode','TRY');add(root,B,'LineCountNumeric',len(data['items']))
    party(root,'AccountingSupplierParty',data['seller']);party(root,'AccountingCustomerParty',data['buyer'])
    if data.get('due_date'): add(add(root,A,'PaymentTerms'),B,'PaymentDueDate',data['due_date'])
    totals={}; rows=[]
    for item in data['items']:
        qty=Decimal(str(item['quantity'])); price=Decimal(item['price']);rate=Decimal(item['vat']);discount=Decimal(item['discount'])
        base=price/(1+rate/100) if item['included'] else price
        net=money(base*qty*(1-discount/100));tax=money(net*rate/100)
        rows.append((item,base,net,tax,rate))
        previous=totals.get(rate,(Decimal(0),Decimal(0)));totals[rate]=(previous[0]+net,previous[1]+tax)
    net=sum((r[2] for r in rows),Decimal(0));vat=sum((r[3] for r in rows),Decimal(0))
    if abs(net-money(data['net']))>Decimal('.01') or abs(vat-money(data['vat']))>Decimal('.01'):
        raise UyumError('Kalem yuvarlamaları BOS toplamıyla uyuşmuyor. Faturayı kontrol edin.')
    def tax_total(parent, groups):
        total=add(parent,A,'TaxTotal');amount(total,'TaxAmount',sum((v[1] for v in groups.values()),Decimal(0)))
        for rate,(taxable,tax) in groups.items():
            sub=add(total,A,'TaxSubtotal');amount(sub,'TaxableAmount',taxable);amount(sub,'TaxAmount',tax);add(sub,B,'Percent',rate)
            scheme=add(add(sub,A,'TaxCategory'),A,'TaxScheme');add(scheme,B,'Name','KDV');add(scheme,B,'TaxTypeCode','0015')
    tax_total(root,totals)
    legal=add(root,A,'LegalMonetaryTotal')
    for name,value in [('LineExtensionAmount',net),('TaxExclusiveAmount',net),('TaxInclusiveAmount',net+vat),('PayableAmount',net+vat)]:amount(legal,name,value)
    for idx,(item,base,net,tax,rate) in enumerate(rows,1):
        line=add(root,A,'InvoiceLine');add(line,B,'ID',idx)
        add(line,B,'InvoicedQuantity',item['quantity'],unitCode='C62');amount(line,'LineExtensionAmount',net)
        if Decimal(item['discount']):
            allowance=add(line,A,'AllowanceCharge');add(allowance,B,'ChargeIndicator','false')
            add(allowance,B,'MultiplierFactorNumeric',Decimal(item['discount'])/100)
            amount(allowance,'Amount',base*Decimal(str(item['quantity']))-net);amount(allowance,'BaseAmount',base*Decimal(str(item['quantity'])))
        tax_total(line,{rate:(net,tax)});add(add(line,A,'Item'),B,'Name',item['name'])
        price=add(line,A,'Price');add(price,B,'PriceAmount',format(base.quantize(Decimal('.000001')), 'f'),currencyID='TRY')
    return root


class Client:
    def __init__(self, config):
        self.config=config
        if config.get('environment') not in ENDPOINTS: raise UyumError('Geçersiz servis ortamı.')
    def call(self, method, params=None):
        if method not in {'IsEInvoiceUser','SaveAsDraft','QueryOutboxInvoiceStatus','GetUserAliasses','GetOutboxInvoice','GetOutboxInvoicePdf','GetInboxInvoiceList','GetInboxInvoice','GetInboxInvoicePdf','QueryInboxInvoiceStatus'}: raise UyumError('Bu işlem desteklenmiyor.')
        env=ET.Element('{'+S+'}Envelope');header=add(env,S,'Header');security=add(header,W,'Security',**{'{'+S+'}mustUnderstand':'1'})
        now=datetime.now(timezone.utc)
        timestamp=add(security,U,'Timestamp')
        add(timestamp,U,'Created',now.isoformat().replace('+00:00','Z'));add(timestamp,U,'Expires',(now+timedelta(minutes=5)).isoformat().replace('+00:00','Z'))
        token=add(security,W,'UsernameToken');add(token,W,'Username',self.config['username']);add(token,W,'Password',self.config['password'])
        op=add(add(env,S,'Body'),T,method)
        for child in params or []:op.append(child)
        try:
            response=requests.post(ENDPOINTS[self.config['environment']],data=ET.tostring(env,encoding='utf-8',xml_declaration=True),
                headers={'Content-Type':'text/xml; charset=utf-8','SOAPAction':'"http://tempuri.org/IIntegration/'+method+'"'},timeout=(8,25),allow_redirects=False)
            if len(response.content)>20_000_000:raise UyumError('Servis yanıtı beklenen boyutu aştı.')
            tree=ET.fromstring(response.content)
            result=tree.find('.//{'+T+'}'+method+'Result')
            if response.status_code!=200 or result is None:
                raise UyumError('Uyumsoft bağlantıyı kabul etmedi. Servis kullanıcı bilgilerini ve erişim yetkisini kontrol edin.')
            if result.get('IsSucceded')!='true' and result.findtext('{'+T+'}IsSucceded')!='true':
                message=result.get('Message') or result.findtext('{'+T+'}Message') or 'İşlem kabul edilmedi.'
                for secret in (self.config.get('password'),self.config.get('username')):
                    if secret:message=message.replace(secret,'***')
                raise UyumError('Uyumsoft: '+message[:500])
            return result
        except (requests.RequestException, ET.ParseError):raise UyumError('Uyumsoft yanıtı alınamadı. Aktarım yapıldıysa tekrar göndermeden durumunu sorgulayın.') from None
    def is_einvoice(self, tax_number):
        p=ET.Element('{'+T+'}vknTckn');p.text=tax_number
        result=self.call('IsEInvoiceUser',[p])
        value=result.get('Value') or result.findtext('{'+T+'}Value')
        if value not in ('true','false'):raise UyumError('Mükellef sorgusu doğrulanamadı.')
        return value=='true'
    def resolve_alias(self, tax_number, chosen=''):
        p=ET.Element('{'+T+'}vknTckn');p.text=tax_number
        result=self.call('GetUserAliasses',[p])
        aliases=[x.get('Alias') for x in result.findall('.//{'+T+'}ReceiverboxAliases') if x.get('Enabled')=='true' and x.get('Alias')]
        if chosen and chosen in aliases:return chosen
        if not chosen and len(aliases)==1:return aliases[0]
        raise UyumError('Alıcının e-Fatura posta kutusunu belirtin. Kullanılabilir adresler: '+', '.join(aliases))
    def save_draft(self, data, uuid, einvoice):
        invoices=ET.Element('{'+T+'}invoices');info=add(invoices,T,'InvoiceInfo',LocalDocumentId='BOS-'+uuid)
        inv=invoice_xml(data,uuid,einvoice);inv.tag='{'+T+'}Invoice';info.append(inv)
        add(info,T,'TargetCustomer',VknTckn=data['buyer']['tax_number'],Title=data['buyer']['name'],Alias=data.get('alias',''))
        if not einvoice:add(info,T,'EArchiveInvoiceInfo',DeliveryType='Paper')
        add(info,T,'Scenario','eInvoice' if einvoice else 'eArchive')
        add(info,T,'CreateDateUtc',datetime.now(timezone.utc).isoformat())
        result=self.call('SaveAsDraft',[invoices]);identity=result.find('{'+T+'}Value')
        if identity is None or identity.get('Id','').lower()!=uuid.lower():raise UyumError('Taslak kimliği doğrulanamadı; yeniden göndermeden durumu sorgulayın.')
        return {'number':identity.get('Number',''),'scenario':identity.get('InvoiceScenario','')}
    def status(self, uuid):
        ids=ET.Element('{'+T+'}invoiceIds');add(ids,T,'string',uuid)
        values=self.call('QueryOutboxInvoiceStatus',[ids]).findall('{'+T+'}Value')
        match=next((x for x in values if x.get('InvoiceId','').lower()==uuid.lower()),None)
        if match is None:raise UyumError('Bu ETTN için durum bulunamadı. Portalı veya Uyumsoft desteğini kontrol edin; yeniden aktarım yapılmadı.')
        return match.get('Status','Bilinmiyor')

    def outbox_invoice(self, uuid):
        p=ET.Element('{'+T+'}invoiceId');p.text=uuid
        result=self.call('GetOutboxInvoice',[p])
        root=result.find('{'+T+'}Value/{'+T+'}Invoice')
        if root is None:raise UyumError('Uyumsoft fatura içeriği alınamadı.')
        return root
    def outbox_pdf(self, uuid):
        import base64, binascii
        p=ET.Element('{'+T+'}invoiceId');p.text=uuid
        result=self.call('GetOutboxInvoicePdf',[p]);value=result.find('{'+T+'}Value')
        if value is None or value.get('InvoiceId','').lower()!=uuid.lower():raise UyumError('PDF ETTN eşleşmedi.')
        try:data=base64.b64decode(''.join((value.findtext('{'+T+'}Data') or '').split()),validate=True)
        except (ValueError,binascii.Error):raise UyumError('PDF içeriği okunamadı.') from None
        if not data.startswith(b'%PDF-') or len(data)>12_000_000:raise UyumError('Geçerli PDF alınamadı; fatura kaydedilmedi.')
        return data

    def inbox_list(self, start, end, page=0):
        q=ET.Element('{'+T+'}query',PageIndex=str(page),PageSize='25',OnlyNewestInvoices='false')
        xsi='http://www.w3.org/2001/XMLSchema-instance'
        for name in ('ExecutionStartDate','ExecutionEndDate','CreateStartDate','CreateEndDate','Status'):
            if name=='ExecutionStartDate':add(q,T,name,start+'T00:00:00')
            elif name=='ExecutionEndDate':add(q,T,name,end+'T23:59:59')
            else:add(q,T,name,**{'{'+xsi+'}nil':'true'})
        add(q,T,'SortColumn','ExecutionDate');add(q,T,'SortMode','Descending')
        add(q,T,'IsArchived',**{'{'+xsi+'}nil':'true'})
        add(q,T,'IncludeTagList','false')
        value=self.call('GetInboxInvoiceList',[q]).find('{'+T+'}Value')
        if value is None:raise UyumError('Gelen fatura listesi okunamadı.')
        rows=[{x.tag.split('}')[-1]:x.text or '' for x in row} for row in value.findall('{'+T+'}Items')]
        return rows,int(value.get('TotalPages','1')),int(value.get('TotalCount',str(len(rows))))
    def inbox_invoice(self, uuid):
        p=ET.Element('{'+T+'}invoiceId');p.text=uuid
        root=self.call('GetInboxInvoice',[p]).find('{'+T+'}Value/{'+T+'}Invoice')
        if root is None:raise UyumError('Gelen fatura içeriği alınamadı.')
        return root
    def inbox_status(self, uuid):
        ids=ET.Element('{'+T+'}invoiceIds');add(ids,T,'string',uuid)
        rows=self.call('QueryInboxInvoiceStatus',[ids]).findall('{'+T+'}Value')
        match=next((x for x in rows if x.get('InvoiceId','').lower()==uuid.lower()),None)
        if match is None:raise UyumError('Gelen faturanın durumu bulunamadı.')
        return match.get('Status','Bilinmiyor')
    def inbox_pdf(self, uuid):
        import base64,binascii
        p=ET.Element('{'+T+'}invoiceId');p.text=uuid
        value=self.call('GetInboxInvoicePdf',[p]).find('{'+T+'}Value')
        if value is None or value.get('InvoiceId','').lower()!=uuid.lower():raise UyumError('PDF ETTN eşleşmedi.')
        try:data=base64.b64decode(''.join((value.findtext('{'+T+'}Data') or '').split()),validate=True)
        except (ValueError,binascii.Error):raise UyumError('PDF okunamadı.') from None
        if not data.startswith(b'%PDF-') or len(data)>12_000_000:raise UyumError('Geçerli PDF alınamadı.')
        return data
