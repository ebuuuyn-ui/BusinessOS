import os
import sqlite3
import unicodedata
import csv
import calendar
import math
import hashlib
import json
import mimetypes
import secrets
import re
import shutil
import subprocess
import tempfile
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape as xml_escape
from io import BytesIO, StringIO
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import Flask, abort, flash, make_response, redirect, render_template, request, send_file, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, func, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import joinedload, selectinload
from werkzeug.utils import secure_filename
from customer_movement_report import register_report
from stock_sorting import stock_text_sort_key
from web_auth import install_web_auth
from database_migration import MigrationError, migrate_sqlite_database
from account_exports import statement_period, export_xlsx as account_export_xlsx, export_pdf as account_export_pdf

try:
    # macOS Anahtarlık'taki kurumsal/ağ sertifikalarını da kullanır.
    # Sertifika doğrulamasını kapatmaz; sistemin güvenilir sertifika deposuna bağlar.
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass


db = SQLAlchemy()

ORDER_STATUSES = [
    "Bekliyor",
    "Onaylandı",
    "Üretimde",
    "Paketleniyor",
    "Sevkiyat Bekliyor",
    "Sevk Edildi",
    "Teslim Edildi",
    "İptal Edildi",
]
ORDER_TYPES = ["Satış", "Satın Alma"]
ORDER_PAYMENT_METHODS = ["Banka Havalesi", "Kredi Kartı Tek Çekim", "Kredi Kartı 3 Taksit", "Çek"]
FINANCIAL_ORDER_STATUSES = ["Sevk Edildi", "Teslim Edildi"]
EXPENSE_CATEGORIES = ["Fatura Ödemeleri", "Yemek", "Ulaşım", "Kira", "Personel", "Vergi / Harç", "Bakım / Onarım", "Ofis Giderleri", "Kargo / Nakliye", "Pazarlama", "Diğer"]
PAYMENT_METHODS = ["Nakit", "Kredi Kartı"]
COLLECTION_PAYMENT_METHODS = ["Nakit", "Çek", "Banka", "Kredi Kartı"]
ACCOUNT_PAYMENT_METHODS = COLLECTION_PAYMENT_METHODS
LIQUID_PAYMENT_METHODS = ["Nakit", "Banka"]
CARD_OWNER_TYPES = ["Kendi Kartımız", "Müşteri Kartı"]
CHECK_STATUSES = ["Bekliyor", "Tahsil Edildi", "Ödendi", "İade Edildi", "Karşılıksız"]
ORDER_DOCUMENT_TYPES = ["Giden Fatura", "Gelen Fatura", "İrsaliye", "Teklif", "Diğer Belge"]
ORDER_DOCUMENT_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "webp", "heic"}
ORDER_DOCUMENT_MAX_BYTES = 25 * 1024 * 1024
CUSTOMER_TAX_DOCUMENT_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "webp", "heic"}
STOCK_MOVEMENT_TYPES = ["Açılış Stoğu", "Stok Girişi", "Stok Çıkışı", "Sayım Artışı", "Sayım Azalışı"]
STOCK_IN_TYPES = {"Açılış Stoğu", "Stok Girişi", "Sayım Artışı"}
TELEGRAM_KEYCHAIN_SERVICE = "BusinessOS.Telegram"
TELEGRAM_KEYCHAIN_ACCOUNT = "bot_token"


def normalize_search_text(value):
    if value is None:
        return ""
    text_value = str(value).translate(str.maketrans({"ı": "i", "İ": "I"})).casefold()
    return "".join(character for character in unicodedata.normalize("NFD", text_value) if unicodedata.category(character) != "Mn")


def selected_order_statuses(args):
    """Geçerli, tekrarsız sipariş durumlarını çoklu filtre olarak döndürür."""
    return list(dict.fromkeys(
        status for status in args.getlist("status")
        if status in ORDER_STATUSES
    ))


def selected_order_invoice_status(args):
    """Siparişin en az bir faturaya bağlı olup olmadığına göre filtreyi döndürür."""
    value = args.get("invoice_status", "")
    return value if value in {"Faturalandı", "Fatura Bekliyor"} else ""


def apply_order_invoice_status_filter(records, invoice_status):
    if not invoice_status:
        return records
    invoiced_order_ids = db.session.query(Invoice.order_id).filter(Invoice.order_id.isnot(None))
    return records.filter(Order.id.in_(invoiced_order_ids) if invoice_status == "Faturalandı" else ~Order.id.in_(invoiced_order_ids))


class InvoicePrefillPlaceholder:
    """Şablonda boş fatura için güvenli, yanlışsız bir sipariş yer tutucusu."""
    class Customer:
        name = ""
        code = ""

    customer = Customer()

    def __bool__(self):
        return False


@event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        dbapi_connection.create_function("normalize_tr", 1, normalize_search_text, deterministic=True)


class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    code = db.Column(db.String(80), unique=True, index=True)
    contact_name = db.Column(db.String(120))
    phone = db.Column(db.String(40))
    mobile = db.Column(db.String(40))
    email = db.Column(db.String(160))
    city = db.Column(db.String(100))
    address = db.Column(db.Text)
    notes = db.Column(db.Text)
    tax_office = db.Column(db.String(120))
    tax_number = db.Column(db.String(20), index=True)
    shipment_contact = db.Column(db.String(120))
    shipment_phone = db.Column(db.String(40))
    shipment_city = db.Column(db.String(100))
    shipment_address = db.Column(db.Text)
    shipment_note = db.Column(db.Text)
    tax_document_name = db.Column(db.String(255))
    tax_document_stored_name = db.Column(db.String(255))
    tax_document_mime_type = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    orders = db.relationship("Order", back_populates="customer", lazy="dynamic")
    invoices = db.relationship("Invoice", back_populates="customer", lazy="dynamic")
    account_transactions = db.relationship("AccountTransaction", back_populates="customer", cascade="all, delete-orphan", order_by="AccountTransaction.transaction_date")

    @property
    def balance(self):
        """Cari bakiye yalnız faturalar ve tahsilat/ödemelerden oluşur.

        Siparişin sevk veya teslim edilmesi fiziksel/operasyonel bir durumdur;
        cari borç-alacak etkisi fatura kaydedildiğinde oluşur.
        """
        invoices = self.invoices.all()
        invoice_balance = sum((invoice.total_amount if invoice.invoice_type == "Satış" else -invoice.total_amount) for invoice in invoices)
        manual_balance = sum((transaction.debit or 0) - (transaction.credit or 0) for transaction in self.account_transactions)
        return invoice_balance + manual_balance


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    code = db.Column(db.String(80), unique=True)
    description = db.Column(db.Text)
    special_code = db.Column(db.String(160))
    group_name = db.Column(db.String(160), index=True)
    default_variant = db.Column(db.String(120))
    unit = db.Column(db.String(30), default="Adet", nullable=False)
    unit_price = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    purchase_price = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    include_in_catalog = db.Column(db.Boolean, default=False, nullable=False, index=True)
    include_in_price_list = db.Column(db.Boolean, default=False, nullable=False, index=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class StockMovement(db.Model):
    """Fiziksel stok için elle doğrulanmış giriş/çıkış hareketi."""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    movement_type = db.Column(db.String(30), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False)
    movement_date = db.Column(db.Date, default=date.today, nullable=False, index=True)
    note = db.Column(db.String(240))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    product = db.relationship("Product")

    @property
    def signed_quantity(self):
        return self.quantity if self.movement_type in STOCK_IN_TYPES else -self.quantity


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(30), unique=True, nullable=False, index=True)
    order_type = db.Column(db.String(30), default="Satış", nullable=False, index=True)
    source_order_id = db.Column(db.Integer, nullable=True, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    order_date = db.Column(db.Date, default=date.today, nullable=False)
    delivery_date = db.Column(db.Date)
    # Satın alma siparişinin ürünlerinin gönderileceği il.
    # Satış siparişlerinde boş kalır.
    delivery_city = db.Column(db.String(100))
    shipment_contact = db.Column(db.String(120))
    shipment_phone = db.Column(db.String(40))
    shipment_address = db.Column(db.Text)
    shipment_note = db.Column(db.Text)
    # Satın alma siparişinin hangi müşteri/firma için açıldığını tutar.
    # Bu yalnızca şirket içi takip bilgisidir; tedarikçiye giden formlara eklenmez.
    customer_company = db.Column(db.String(180))
    payment_method = db.Column(db.String(40))
    status = db.Column(db.String(40), default="Bekliyor", nullable=False, index=True)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    customer = db.relationship("Customer", back_populates="orders")
    items = db.relationship("OrderItem", back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.id")
    history = db.relationship("OrderHistory", back_populates="order", cascade="all, delete-orphan", order_by="OrderHistory.created_at.desc()")
    documents = db.relationship("OrderDocument", back_populates="order", cascade="all, delete-orphan", order_by="OrderDocument.created_at.desc()")
    invoices = db.relationship("Invoice", back_populates="order")

    @property
    def total_quantity(self):
        return sum(item.quantity for item in self.items)

    @property
    def net_amount(self):
        return sum((item.net_amount for item in self.items), Decimal("0"))

    @property
    def vat_amount(self):
        return sum(item.vat_amount for item in self.items)

    @property
    def total_amount(self):
        return self.net_amount + self.vat_amount


class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id", ondelete="CASCADE"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=True)
    source_order_item_id = db.Column(db.Integer, nullable=True, index=True)
    product_name = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text)
    variant = db.Column(db.String(120))
    detail_2 = db.Column(db.String(160))
    detail_3 = db.Column(db.String(160))
    quantity = db.Column(db.Integer, nullable=False)
    unit = db.Column(db.String(30), default="Adet", nullable=False)
    unit_price = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    discount_rate = db.Column(db.Numeric(5, 2), default=0, nullable=False)
    # Satış anındaki birim maliyet. Ürün kartındaki alış fiyatı sonradan
    # değişse bile geçmiş dönem kârlılığı bu değer sayesinde değişmez.
    cost_unit_price = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    vat_rate = db.Column(db.Numeric(5, 2), default=10, nullable=False)
    # False: girilen birim fiyat KDV hariçtir. True: birim fiyat KDV dahildir.
    # Eski siparişler hesapları değişmesin diye varsayılanı hariçtir.
    vat_included = db.Column(db.Boolean, default=False, nullable=False)
    note = db.Column(db.Text)
    order = db.relationship("Order", back_populates="items")
    product = db.relationship("Product")

    @property
    def net_amount(self):
        gross = (self.unit_price or Decimal("0")) * self.quantity
        discount = max(Decimal("0"), min(self.discount_rate or Decimal("0"), Decimal("100")))
        after_discount = gross * (Decimal("100") - discount) / Decimal("100")
        vat_rate = self.vat_rate or Decimal("0")
        if self.vat_included and vat_rate:
            return after_discount * Decimal("100") / (Decimal("100") + vat_rate)
        return after_discount

    @property
    def vat_amount(self):
        vat_rate = self.vat_rate or Decimal("0")
        if self.vat_included:
            gross = (self.unit_price or Decimal("0")) * self.quantity
            discount = max(Decimal("0"), min(self.discount_rate or Decimal("0"), Decimal("100")))
            return gross * (Decimal("100") - discount) / Decimal("100") - self.net_amount
        return self.net_amount * vat_rate / Decimal("100")

    @property
    def total_amount(self):
        return self.net_amount + self.vat_amount

    @property
    def cost_amount(self):
        return (self.cost_unit_price or Decimal("0")) * self.quantity


class OrderHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id", ondelete="CASCADE"), nullable=False)
    status = db.Column(db.String(40), nullable=False)
    note = db.Column(db.String(240))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    order = db.relationship("Order", back_populates="history")


class OrderDocument(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id", ondelete="CASCADE"), nullable=False, index=True)
    document_type = db.Column(db.String(40), nullable=False, default="Diğer Belge")
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False, unique=True)
    mime_type = db.Column(db.String(120))
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    source = db.Column(db.String(30), nullable=False, default="Manuel")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    order = db.relationship("Order", back_populates="documents")


class Invoice(db.Model):
    """Cari ve fiziksel stok etkisi olan alış/satış faturası."""
    id = db.Column(db.Integer, primary_key=True)
    invoice_no = db.Column(db.String(80), unique=True, nullable=False, index=True)
    invoice_type = db.Column(db.String(20), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False, index=True)
    # Bir sipariş parça parça veya farklı irsaliyelerle birden fazla faturaya bağlanabilir.
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=True, index=True)
    invoice_date = db.Column(db.Date, default=date.today, nullable=False, index=True)
    due_date = db.Column(db.Date, nullable=True, index=True)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    customer = db.relationship("Customer", back_populates="invoices")
    order = db.relationship("Order", back_populates="invoices")
    items = db.relationship("InvoiceItem", back_populates="invoice", cascade="all, delete-orphan", order_by="InvoiceItem.id")

    @property
    def net_amount(self):
        return sum((item.net_amount for item in self.items), Decimal("0"))

    @property
    def vat_amount(self):
        return sum((item.vat_amount for item in self.items), Decimal("0"))

    @property
    def total_amount(self):
        return self.net_amount + self.vat_amount


class InvoiceItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    product_name = db.Column(db.String(160), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit = db.Column(db.String(30), nullable=False, default="Adet")
    unit_price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    discount_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=10)
    vat_included = db.Column(db.Boolean, default=False, nullable=False)
    stock_movement_id = db.Column(db.Integer, unique=True, nullable=True, index=True)
    invoice = db.relationship("Invoice", back_populates="items")
    product = db.relationship("Product")

    @property
    def net_amount(self):
        gross = (self.unit_price or Decimal("0")) * self.quantity
        discount = max(Decimal("0"), min(self.discount_rate or Decimal("0"), Decimal("100")))
        discounted = gross * (Decimal("100") - discount) / Decimal("100")
        vat_rate = self.vat_rate or Decimal("0")
        return discounted * Decimal("100") / (Decimal("100") + vat_rate) if self.vat_included and vat_rate else discounted

    @property
    def vat_amount(self):
        gross = (self.unit_price or Decimal("0")) * self.quantity
        discount = max(Decimal("0"), min(self.discount_rate or Decimal("0"), Decimal("100")))
        discounted = gross * (Decimal("100") - discount) / Decimal("100")
        return discounted - self.net_amount if self.vat_included else self.net_amount * (self.vat_rate or Decimal("0")) / Decimal("100")

    @property
    def total_amount(self):
        return self.net_amount + self.vat_amount


class TelegramIncomingDocument(db.Model):
    """Telegram'dan gelen, henüz bir siparişe bağlanmamış belge."""
    id = db.Column(db.Integer, primary_key=True)
    telegram_update_id = db.Column(db.String(40), unique=True, nullable=False, index=True)
    chat_id = db.Column(db.String(40), nullable=False, index=True)
    telegram_message_id = db.Column(db.String(40), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False, unique=True)
    mime_type = db.Column(db.String(120))
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    caption = db.Column(db.String(500))
    suggested_order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=True, index=True)
    received_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    suggested_order = db.relationship("Order", foreign_keys=[suggested_order_id])


class EArchiveIncomingDocument(db.Model):
    """E-Arşiv Portal'dan İndirilenler klasörüne gelen, henüz bağlanmamış PDF."""
    id = db.Column(db.Integer, primary_key=True)
    file_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False, unique=True)
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    suggested_order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=True, index=True)
    received_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    suggested_order = db.relationship("Order", foreign_keys=[suggested_order_id])


class StoredFile(db.Model):
    """Vercel'de kalıcı olması gereken belge içeriği."""
    id = db.Column(db.Integer, primary_key=True)
    storage_key = db.Column(db.String(500), unique=True, nullable=False, index=True)
    content = db.Column(db.LargeBinary, nullable=False)
    mime_type = db.Column(db.String(120))
    size_bytes = db.Column(db.Integer, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class MigrationLegacyArchive(db.Model):
    """Güncel modellerde bulunmayan eski SQLite verilerini kayıpsız saklar."""
    id = db.Column(db.Integer, primary_key=True)
    source_table = db.Column(db.String(160), nullable=False, index=True)
    source_key = db.Column(db.String(240))
    payload = db.Column(db.JSON, nullable=False)
    imported_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class AccountTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id", ondelete="CASCADE"), nullable=False, index=True)
    transaction_date = db.Column(db.Date, default=date.today, nullable=False, index=True)
    transaction_type = db.Column(db.String(40), nullable=False)
    reference_no = db.Column(db.String(80))
    description = db.Column(db.String(240), nullable=False)
    debit = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    credit = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    payment_method = db.Column(db.String(30))
    check_no = db.Column(db.String(80))
    check_bank = db.Column(db.String(120))
    check_due_date = db.Column(db.Date, index=True)
    check_status = db.Column(db.String(40))
    card_installments = db.Column(db.Integer)
    card_owner_type = db.Column(db.String(40))
    card_customer_id = db.Column(db.Integer)
    card_customer_name = db.Column(db.String(160))
    linked_transaction_id = db.Column(db.Integer, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    customer = db.relationship("Customer", back_populates="account_transactions")


class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    expense_date = db.Column(db.Date, default=date.today, nullable=False, index=True)
    category = db.Column(db.String(80), nullable=False, index=True)
    document_no = db.Column(db.String(80))
    payee = db.Column(db.String(160))
    description = db.Column(db.String(240), nullable=False)
    payment_method = db.Column(db.String(40), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class RecurringExpense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(240), nullable=False)
    category = db.Column(db.String(80), nullable=False, index=True)
    payee = db.Column(db.String(160))
    payment_method = db.Column(db.String(40), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    payment_day = db.Column(db.Integer, nullable=False)
    last_recorded_month = db.Column(db.String(7))
    active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def due_date_for(self, target_date):
        last_day = calendar.monthrange(target_date.year, target_date.month)[1]
        return date(target_date.year, target_date.month, min(self.payment_day, last_day))

    def is_recorded_for(self, target_date):
        return self.last_recorded_month == target_date.strftime("%Y-%m")


class CashMovement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    movement_date = db.Column(db.Date, default=date.today, nullable=False, index=True)
    movement_type = db.Column(db.String(20), nullable=False)
    description = db.Column(db.String(240), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class PersonalMonth(db.Model):
    month = db.Column(db.String(80), primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    entries = db.relationship("PersonalPayment", back_populates="month_record", cascade="all, delete-orphan", order_by="PersonalPayment.sort_order")


class PersonalPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    month = db.Column(db.String(80), db.ForeignKey("personal_month.month", ondelete="CASCADE"), nullable=False, index=True)
    kind = db.Column(db.String(20), default="other", nullable=False)
    name = db.Column(db.String(160), nullable=False)
    credit_limit = db.Column(db.Numeric(14, 2))
    debt = db.Column(db.Numeric(14, 2))
    minimum_payment = db.Column(db.Numeric(14, 2))
    payment = db.Column(db.Numeric(14, 2))
    available_limit = db.Column(db.Numeric(14, 2))
    remaining_debt = db.Column(db.Numeric(14, 2))
    due_day = db.Column(db.Integer)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    month_record = db.relationship("PersonalMonth", back_populates="entries")

    @property
    def calculated_remaining_minimum(self):
        return max((self.minimum_payment or Decimal("0")) - (self.payment or Decimal("0")), Decimal("0"))

    @property
    def calculated_remaining(self):
        if self.kind == "card" and self.credit_limit is not None and self.available_limit is not None:
            return max(self.credit_limit - self.available_limit, Decimal("0"))
        return self.remaining_debt or Decimal("0")


class PersonalPerson(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    transactions = db.relationship("PersonalLedgerTransaction", back_populates="person", cascade="all, delete-orphan", order_by="PersonalLedgerTransaction.transaction_date")


class PersonalLedgerTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    person_id = db.Column(db.Integer, db.ForeignKey("personal_person.id", ondelete="CASCADE"), nullable=False, index=True)
    transaction_date = db.Column(db.Date)
    description = db.Column(db.String(240), default="", nullable=False)
    sent_amount = db.Column(db.Numeric(14, 2))
    received_amount = db.Column(db.Numeric(14, 2))
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    person = db.relationship("PersonalPerson", back_populates="transactions")


class OzonSale(db.Model):
    """Ozon satış ve kesinti kayıtları; API geldiğinde aynı alanlar otomatik dolar."""
    id = db.Column(db.Integer, primary_key=True)
    store_key = db.Column(db.String(60), default="magaza-1", nullable=False, index=True)
    external_operation_id = db.Column(db.String(80), index=True)
    sale_date = db.Column(db.Date, default=date.today, nullable=False, index=True)
    posting_number = db.Column(db.String(100), index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=True, index=True)
    ozon_sku = db.Column(db.String(100), index=True)
    product_name = db.Column(db.String(180), nullable=False)
    quantity = db.Column(db.Integer, default=1, nullable=False)
    sales_amount = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    cost_amount = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    commission_amount = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    logistics_amount = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    advertising_amount = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    other_expense_amount = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    product = db.relationship("Product")

    @property
    def platform_expenses(self):
        return sum((self.commission_amount or 0, self.logistics_amount or 0, self.advertising_amount or 0, self.other_expense_amount or 0), Decimal("0"))

    @property
    def net_revenue(self):
        return (self.sales_amount or Decimal("0")) - self.platform_expenses

    @property
    def profit(self):
        return self.net_revenue - (self.cost_amount or Decimal("0"))


class OzonProduct(db.Model):
    """Her Ozon mağazasından yalnızca okunarak eşitlenen ürün kartı."""
    id = db.Column(db.Integer, primary_key=True)
    store_key = db.Column(db.String(60), nullable=False, index=True)
    ozon_product_id = db.Column(db.String(80), nullable=False, index=True)
    offer_id = db.Column(db.String(160), index=True)
    sku = db.Column(db.String(100), index=True)
    name = db.Column(db.String(240), nullable=False)
    current_price = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    old_price = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    marketing_price = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    cost_unit_price = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    is_archived = db.Column(db.Boolean, default=False, nullable=False)
    synced_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (db.UniqueConstraint("store_key", "ozon_product_id", name="uq_ozon_product_store_product"),)


class OzonFinanceSnapshot(db.Model):
    """Ozon Seller API'den alınan aylık resmî finans özeti."""
    # Eski tek mağaza kayıtlarıyla uyumluluk için bu alan birincil anahtar kalır.
    # Yeni kayıtlarda değer: "magaza-1:2026-08" biçimindedir.
    report_month = db.Column(db.String(80), primary_key=True)
    store_key = db.Column(db.String(60), default="magaza-1", nullable=False, index=True)
    period_month = db.Column(db.String(7), index=True)
    sales_accrual = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    sale_commission = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    processing_delivery = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    refunds_cancellations = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    services_amount = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    other_amount = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    money_transfer = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    synced_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


def ozon_settings_path(app):
    return os.path.join(app.instance_path, "ozon_api_settings.json")


def load_ozon_settings(app):
    try:
        with open(ozon_settings_path(app), "r", encoding="utf-8") as settings_file:
            values = json.load(settings_file)
        raw_stores = values.get("stores")
        if isinstance(raw_stores, list):
            stores = []
            for index, raw_store in enumerate(raw_stores, start=1):
                if not isinstance(raw_store, dict):
                    continue
                client_id = str(raw_store.get("client_id", "")).strip()
                api_key = str(raw_store.get("api_key", "")).strip()
                if client_id or api_key:
                    stores.append({
                        "key": str(raw_store.get("key") or f"magaza-{index}").strip(),
                        "name": str(raw_store.get("name") or f"Mağaza {index}").strip(),
                        "client_id": client_id,
                        "api_key": api_key,
                    })
            return {"stores": stores}
        # İlk sürümde kaydedilmiş tek mağaza bilgisini kaybetmeden yeni yapıya taşır.
        client_id = str(values.get("client_id", "")).strip()
        api_key = str(values.get("api_key", "")).strip()
        return {"stores": ([{"key": "magaza-1", "name": "Mağaza 1", "client_id": client_id, "api_key": api_key}] if client_id or api_key else [])}
    except (OSError, ValueError, TypeError):
        return {"stores": []}


def save_ozon_settings(app, stores):
    """Keep the API key only on this computer, outside SQLite and Git backups."""
    settings_path = ozon_settings_path(app)
    temporary_path = f"{settings_path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as settings_file:
        json.dump({"stores": stores}, settings_file)
    try:
        os.chmod(temporary_path, 0o600)
    except OSError:
        pass
    os.replace(temporary_path, settings_path)


def get_ozon_store(settings, store_key):
    return next((store for store in settings["stores"] if store["key"] == store_key), None)


def ozon_api_post(store, path, payload):
    """Ozon Seller API isteği; yalnızca okuma amaçlı uç noktalar için kullanılır."""
    api_request = urllib.request.Request(
        f"https://api-seller.ozon.ru{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Client-Id": store["client_id"], "Api-Key": store["api_key"], "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(api_request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def ozon_money_from_item(item, *keys):
    for key in keys:
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("price", value.get("value", "0"))
        if value not in (None, ""):
            return parse_money(value)
    return Decimal("0")


def parse_ozon_operation_date(value):
    text_value = str(value or "").strip().replace("Z", "+00:00")
    for parser in (lambda: datetime.fromisoformat(text_value).date(), lambda: datetime.strptime(text_value[:10], "%Y-%m-%d").date()):
        try:
            return parser()
        except ValueError:
            continue
    return date.today()


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date() if value else None


def parse_money(value):
    try:
        normalized = str(value or "0").strip().replace("₺", "").replace(" ", "")
        if "," in normalized:
            normalized = normalized.replace(".", "").replace(",", ".")
        elif "." in normalized:
            parts = normalized.split(".")
            if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
                normalized = "".join(parts)
        return Decimal(normalized)
    except InvalidOperation:
        return Decimal("0")


def card_payment_details(payment_method, transaction_type):
    """Validate and normalize the optional credit-card payment fields."""
    if payment_method != "Kredi Kartı":
        return {}, None
    if transaction_type not in {"Tahsilat", "Ödeme"}:
        return {}, "Kredi kartı yalnızca tahsilat ve ödeme hareketlerinde kullanılabilir."
    owner_type = "Müşteri Kartı" if transaction_type == "Tahsilat" else request.form.get("card_owner_type", "")
    try:
        installments = int(request.form.get("card_installments", "0"))
    except (TypeError, ValueError):
        installments = 0
    if owner_type not in CARD_OWNER_TYPES:
        return {}, "Kartın kime ait olduğunu seçin."
    if not 1 <= installments <= 36:
        return {}, "Taksit sayısı 1 ile 36 arasında olmalıdır."
    details = {
        "card_installments": installments,
        "card_owner_type": owner_type,
        "card_customer_id": None,
        "card_customer_name": None,
    }
    if owner_type == "Müşteri Kartı":
        card_customer_id = request.form.get("customer_id", type=int) if transaction_type == "Tahsilat" else request.form.get("card_customer_id", type=int)
        card_customer = db.session.get(Customer, card_customer_id) if card_customer_id else None
        if not card_customer:
            return {}, "Kart sahibi müşteriyi seçin."
        details["card_customer_id"] = card_customer.id
        details["card_customer_name"] = card_customer.name
    return details, None


def direct_supplier_for_card_collection():
    """Return an optional supplier selected for a direct customer-card transfer."""
    if request.form.get("direct_to_supplier") != "on":
        return None, None
    supplier_id = request.form.get("direct_supplier_id", type=int)
    supplier = db.session.get(Customer, supplier_id) if supplier_id else None
    if not supplier:
        return None, "Kartın çekildiği tedarikçiyi seçin."
    return supplier, None


def add_direct_card_collection_pair(customer, supplier, amount, transaction_date, reference_no, description, card_details):
    """Create linked collection/payment movements without affecting the cash account."""
    collection = AccountTransaction(customer=customer, transaction_date=transaction_date,
        transaction_type="Tahsilat", reference_no=reference_no,
        description=description or f"Kredi kartı tahsilatı · {supplier.name} firmasına doğrudan aktarıldı",
        debit=0, credit=amount, payment_method="Kredi Kartı", **card_details)
    payment_details = dict(card_details)
    payment_details.update(card_owner_type="Müşteri Kartı", card_customer_id=customer.id, card_customer_name=customer.name)
    payment = AccountTransaction(customer=supplier, transaction_date=transaction_date,
        transaction_type="Ödeme", reference_no=reference_no,
        description=f"{customer.name} kartıyla doğrudan ödeme" + (f" · {description}" if description else ""),
        debit=amount, credit=0, payment_method="Kredi Kartı", **payment_details)
    db.session.add_all([collection, payment])
    db.session.flush()
    collection.linked_transaction_id = payment.id
    payment.linked_transaction_id = collection.id
    return collection, payment


def import_personal_finance_data(app):
    """Eski bağımsız Ödeme Takibi verilerini Business OS'a bir kez aktarır."""
    if app.config.get("TESTING") or PersonalMonth.query.first() or PersonalPerson.query.first():
        return
    source_path = os.path.expanduser("~/Library/Application Support/OdemeTakibi/odemeler.db")
    if not os.path.isfile(source_path):
        return
    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    try:
        table_names = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {"months", "entries"}.issubset(table_names):
            for row in source.execute("SELECT * FROM months ORDER BY month"):
                db.session.add(PersonalMonth(month=row["month"]))
            db.session.flush()
            for row in source.execute("SELECT * FROM entries ORDER BY month,sort_order,id"):
                db.session.add(PersonalPayment(
                    month=row["month"], kind=row["kind"], name=row["name"], credit_limit=row["credit_limit"],
                    debt=row["debt"], minimum_payment=row["minimum_payment"], payment=row["payment"],
                    available_limit=row["available_limit"], remaining_debt=row["remaining_debt"],
                    due_day=row["due_day"], sort_order=row["sort_order"],
                ))
        person_map = {}
        if "people" in table_names:
            for row in source.execute("SELECT * FROM people ORDER BY id"):
                person = PersonalPerson(name=row["name"])
                db.session.add(person)
                db.session.flush()
                person_map[row["id"]] = person.id
        if "ledger_transactions" in table_names:
            for row in source.execute("SELECT * FROM ledger_transactions ORDER BY person_id,sort_order,id"):
                if row["person_id"] in person_map:
                    db.session.add(PersonalLedgerTransaction(
                        person_id=person_map[row["person_id"]], transaction_date=parse_date(row["transaction_date"]),
                        description=row["description"], sent_amount=row["sent_amount"], received_amount=row["received_amount"],
                        sort_order=row["sort_order"],
                    ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    finally:
        source.close()


def calculate_treasury(today=None):
    today = today or date.today()
    # Anlık nakit, fiziksel kasa ile banka havalelerinin toplam kullanılabilir bakiyesidir.
    cash_collections = sum((item.credit or 0 for item in AccountTransaction.query.filter(
        AccountTransaction.transaction_type == "Tahsilat",
        AccountTransaction.payment_method.in_(LIQUID_PAYMENT_METHODS),
    ).all()), Decimal("0"))
    cash_payments = sum((item.debit or 0 for item in AccountTransaction.query.filter(
        AccountTransaction.transaction_type == "Ödeme",
        AccountTransaction.payment_method.in_(LIQUID_PAYMENT_METHODS),
    ).all()), Decimal("0"))
    cash_expenses = sum((item.amount or 0 for item in Expense.query.filter_by(payment_method="Nakit").all()), Decimal("0"))
    manual_in = sum((item.amount or 0 for item in CashMovement.query.filter_by(movement_type="Giriş").all()), Decimal("0"))
    manual_out = sum((item.amount or 0 for item in CashMovement.query.filter_by(movement_type="Çıkış").all()), Decimal("0"))
    open_checks = AccountTransaction.query.filter(AccountTransaction.payment_method == "Çek", AccountTransaction.check_status == "Bekliyor").all()
    incoming_checks = sum((item.credit or 0 for item in open_checks if item.transaction_type == "Tahsilat"), Decimal("0"))
    outgoing_checks = sum((item.debit or 0 for item in open_checks if item.transaction_type == "Ödeme"), Decimal("0"))
    due_soon = [item for item in open_checks if item.check_due_date and today <= item.check_due_date <= today + timedelta(days=30)]
    overdue_checks = [item for item in open_checks if item.check_due_date and item.check_due_date < today]
    return {
        "cash_balance": cash_collections + manual_in - cash_payments - cash_expenses - manual_out,
        "cash_collections": cash_collections,
        "cash_payments": cash_payments,
        "cash_expenses": cash_expenses,
        "manual_in": manual_in,
        "manual_out": manual_out,
        "incoming_checks": incoming_checks,
        "outgoing_checks": outgoing_checks,
        "due_soon": due_soon,
        "overdue_checks": overdue_checks,
    }


def build_account_statement(customer):
    entries = []
    invoices = Invoice.query.filter_by(customer_id=customer.id).all()
    for invoice in invoices:
        is_sale = invoice.invoice_type == "Satış"
        entries.append({
            "date": invoice.invoice_date,
            "sort_time": invoice.created_at,
            "reference": invoice.invoice_no,
            "description": f"{invoice.invoice_type} Faturası",
            "debit": invoice.total_amount if is_sale else Decimal("0"),
            "credit": Decimal("0") if is_sale else invoice.total_amount,
            "order_id": None,
            "order": None,
            "invoice_id": invoice.id,
            "invoice": invoice,
            "transaction_id": None,
        })
    for transaction in customer.account_transactions:
        entries.append({
            "date": transaction.transaction_date,
            "sort_time": transaction.created_at,
            "reference": transaction.reference_no or "—",
            "description": transaction.description,
            "debit": transaction.debit or Decimal("0"),
            "credit": transaction.credit or Decimal("0"),
            "order_id": None,
            "order": None,
            "transaction_id": transaction.id,
            "payment_method": transaction.payment_method,
            "check_no": transaction.check_no,
            "check_bank": transaction.check_bank,
            "check_due_date": transaction.check_due_date,
            "check_status": transaction.check_status,
            "card_installments": transaction.card_installments,
            "card_owner_type": transaction.card_owner_type,
            "card_customer_id": transaction.card_customer_id,
            "card_customer_name": transaction.card_customer_name,
        })
    entries.sort(key=lambda entry: (entry["date"], entry["sort_time"], entry["reference"]))
    balance = Decimal("0")
    for entry in entries:
        balance += entry["debit"] - entry["credit"]
        entry["balance"] = balance
    return entries


def calculate_customer_balances(customer_ids=None):
    customer_query = Customer.query
    if customer_ids is not None:
        customer_query = customer_query.filter(Customer.id.in_(customer_ids))
    ids = [customer.id for customer in customer_query.all()]
    balances = {customer_id: Decimal("0") for customer_id in ids}
    if not ids:
        return balances
    invoices = Invoice.query.options(selectinload(Invoice.items)).filter(Invoice.customer_id.in_(ids)).all()
    for invoice in invoices:
        balances[invoice.customer_id] += invoice.total_amount if invoice.invoice_type == "Satış" else -invoice.total_amount
    for transaction in AccountTransaction.query.filter(AccountTransaction.customer_id.in_(ids)).all():
        balances[transaction.customer_id] += (transaction.debit or 0) - (transaction.credit or 0)
    return balances


def customer_balance_view(query="", balance_filter="", balance_sort="amount_desc"):
    """Return the same filtered customer balances used by the balances screen."""
    records = Customer.query
    if query:
        if db.engine.dialect.name == "sqlite":
            pattern = f"%{normalize_search_text(query)}%"
            records = records.filter(db.or_(
                db.func.normalize_tr(Customer.name).like(pattern),
                db.func.normalize_tr(Customer.code).like(pattern),
                db.func.normalize_tr(Customer.contact_name).like(pattern),
                db.func.normalize_tr(Customer.phone).like(pattern),
                db.func.normalize_tr(Customer.mobile).like(pattern),
                db.func.normalize_tr(Customer.email).like(pattern),
                db.func.normalize_tr(Customer.city).like(pattern),
            ))
        else:
            pattern = f"%{query}%"
            records = records.filter(db.or_(
                Customer.name.ilike(pattern), Customer.code.ilike(pattern), Customer.contact_name.ilike(pattern),
                Customer.phone.ilike(pattern), Customer.mobile.ilike(pattern), Customer.email.ilike(pattern),
                Customer.city.ilike(pattern),
            ))
    customers = records.order_by(Customer.name).all()
    balances = calculate_customer_balances([customer.id for customer in customers])
    customers = [customer for customer in customers if balances.get(customer.id, Decimal("0")) != 0]
    if balance_filter == "debit":
        customers = [customer for customer in customers if balances.get(customer.id, Decimal("0")) > 0]
    elif balance_filter == "credit":
        customers = [customer for customer in customers if balances.get(customer.id, Decimal("0")) < 0]
    else:
        balance_filter = ""
    if balance_sort == "amount_asc":
        customers.sort(key=lambda customer: abs(balances.get(customer.id, Decimal("0"))))
    elif balance_sort == "name":
        customers.sort(key=lambda customer: normalize_search_text(customer.name))
    else:
        balance_sort = "amount_desc"
        customers.sort(key=lambda customer: abs(balances.get(customer.id, Decimal("0"))), reverse=True)
    return customers, balances, balance_filter, balance_sort


def build_customer_balances_xlsx(customers, balances):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Cari Bakiyeler"
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "A6"
    navy, pale, line = "14213D", "E8EEF9", "DCE3EC"
    thin = Side(style="thin", color=line)
    debit_total = sum((balances[customer.id] for customer in customers if balances[customer.id] > 0), Decimal("0"))
    credit_total = sum((-balances[customer.id] for customer in customers if balances[customer.id] < 0), Decimal("0"))
    sheet.merge_cells("A1:G1")
    sheet["A1"] = "Cari Bakiyeler"
    sheet["A1"].font = Font(name="Arial", size=18, bold=True, color=navy)
    sheet.row_dimensions[1].height = 30
    sheet.merge_cells("A2:G2")
    sheet["A2"] = f"Oluşturulma: {datetime.now().strftime('%d.%m.%Y %H:%M')} | Business OS"
    sheet["A2"].font = Font(name="Arial", size=9, color="6B7280")
    for column, (label, value) in enumerate((("BORÇLU CARİLER", debit_total), ("ALACAKLI CARİLER", credit_total), ("NET BAKİYE", debit_total-credit_total)), 1):
        cell = sheet.cell(4, column * 2 - 1, label)
        cell.fill = PatternFill("solid", fgColor=pale); cell.font = Font(name="Arial", size=9, bold=True, color="526074")
        cell.alignment = Alignment(horizontal="center")
        value_cell = sheet.cell(4, column * 2, float(value))
        value_cell.fill = PatternFill("solid", fgColor=pale); value_cell.font = Font(name="Arial", size=11, bold=True, color=navy)
        value_cell.alignment = Alignment(horizontal="center"); value_cell.number_format = '₺#,##0.00;[Red]-₺#,##0.00;₺-'
    headers = ["Cari Kodu", "Cari Adı", "Şehir", "Telefon", "Bakiye Türü", "Bakiye", "Net Bakiye"]
    for column, header in enumerate(headers, 1):
        cell = sheet.cell(5, column, header)
        cell.fill = PatternFill("solid", fgColor=navy); cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.alignment = Alignment(horizontal="center")
    for row, customer in enumerate(customers, 6):
        balance = balances[customer.id]
        values = [customer.code or "-", customer.name, customer.city or "-", customer.mobile or customer.phone or "-", "Borçlu" if balance > 0 else "Alacaklı", float(abs(balance)), float(balance)]
        for column, value in enumerate(values, 1):
            cell = sheet.cell(row, column, value)
            cell.border = Border(bottom=thin); cell.font = Font(name="Arial", size=10)
            if column in (6, 7):
                cell.number_format = '₺#,##0.00;[Red]-₺#,##0.00;₺-'
                cell.alignment = Alignment(horizontal="right")
    sheet.column_dimensions["A"].width = 16; sheet.column_dimensions["B"].width = 42; sheet.column_dimensions["C"].width = 18
    sheet.column_dimensions["D"].width = 20; sheet.column_dimensions["E"].width = 16; sheet.column_dimensions["F"].width = 18; sheet.column_dimensions["G"].width = 18
    sheet.auto_filter.ref = f"A5:G{max(5, 5 + len(customers))}"
    sheet.page_setup.orientation = "landscape"; sheet.page_setup.fitToWidth = 1; sheet.page_setup.fitToHeight = 0
    sheet.sheet_properties.pageSetUpPr.fitToPage = True; sheet.print_title_rows = "1:5"; sheet.print_area = f"A1:G{max(5, 5 + len(customers))}"
    output = BytesIO(); workbook.save(output); output.seek(0)
    return output


def build_customer_balances_pdf(customers, balances):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    regular_font, bold_font = "/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
    font_name, bold_name = "Helvetica", "Helvetica-Bold"
    if os.path.isfile(regular_font) and os.path.isfile(bold_font):
        pdfmetrics.registerFont(TTFont("BalanceArial", regular_font)); pdfmetrics.registerFont(TTFont("BalanceArialBold", bold_font))
        font_name, bold_name = "BalanceArial", "BalanceArialBold"
    amount = lambda value: f"TL {Decimal(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    debit_total = sum((balances[customer.id] for customer in customers if balances[customer.id] > 0), Decimal("0"))
    credit_total = sum((-balances[customer.id] for customer in customers if balances[customer.id] < 0), Decimal("0"))
    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=landscape(A4), leftMargin=12*mm, rightMargin=12*mm, topMargin=12*mm, bottomMargin=12*mm, title="Cari Bakiyeler")
    styles = getSampleStyleSheet()
    title = ParagraphStyle("BalanceTitle", parent=styles["Title"], fontName=bold_name, fontSize=17, leading=20, textColor=colors.HexColor("#14213D"), alignment=TA_LEFT)
    meta = ParagraphStyle("BalanceMeta", parent=styles["BodyText"], fontName=font_name, fontSize=8, textColor=colors.HexColor("#6B7280"))
    cell = ParagraphStyle("BalanceCell", parent=styles["BodyText"], fontName=font_name, fontSize=8, leading=10)
    right = ParagraphStyle("BalanceRight", parent=cell, alignment=TA_RIGHT)
    center = ParagraphStyle("BalanceCenter", parent=cell, alignment=TA_CENTER)
    header = ParagraphStyle("BalanceHeader", parent=cell, fontName=bold_name, textColor=colors.white, alignment=TA_CENTER)
    story = [Paragraph("Cari Bakiyeler", title), Paragraph(f"Oluşturulma: {datetime.now().strftime('%d.%m.%Y %H:%M')} | Business OS", meta), Spacer(1, 4*mm)]
    summary = Table([["BORÇLU CARİLER", "ALACAKLI CARİLER", "NET BAKİYE"], [amount(debit_total), amount(credit_total), amount(debit_total-credit_total)]], colWidths=[88*mm, 88*mm, 88*mm])
    summary.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#E8EEF9")), ("FONTNAME", (0,0), (-1,0), bold_name), ("TEXTCOLOR", (0,0), (-1,0), colors.HexColor("#526074")), ("FONTNAME", (0,1), (-1,1), bold_name), ("FONTSIZE", (0,1), (-1,1), 13), ("ALIGN", (0,0), (-1,-1), "CENTER"), ("GRID", (0,0), (-1,-1), .35, colors.HexColor("#DCE3EC")), ("TOPPADDING", (0,0), (-1,-1), 6), ("BOTTOMPADDING", (0,0), (-1,-1), 6)]))
    story.extend([summary, Spacer(1, 5*mm)])
    rows = [[Paragraph(value, header) for value in ["CARİ KODU", "CARİ ADI", "ŞEHİR", "TELEFON", "DURUM", "BAKİYE"]]]
    for customer in customers:
        balance = balances[customer.id]
        rows.append([Paragraph(xml_escape(customer.code or "-"), cell), Paragraph(xml_escape(customer.name), cell), Paragraph(xml_escape(customer.city or "-"), cell), Paragraph(xml_escape(customer.mobile or customer.phone or "-"), cell), Paragraph("Borçlu" if balance > 0 else "Alacaklı", center), Paragraph(amount(abs(balance)), right)])
    if not customers:
        rows.append(["", Paragraph("Gösterilecek bakiyeli cari bulunamadı.", cell), "", "", "", ""])
    table = Table(rows, repeatRows=1, colWidths=[29*mm, 89*mm, 33*mm, 40*mm, 31*mm, 43*mm])
    table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#14213D")), ("VALIGN", (0,0), (-1,-1), "MIDDLE"), ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#F6F8FB")]), ("LINEBELOW", (0,0), (-1,-1), .25, colors.HexColor("#DCE3EC")), ("TOPPADDING", (0,0), (-1,-1), 5), ("BOTTOMPADDING", (0,0), (-1,-1), 5)]))
    story.append(table)
    def footer(canvas, doc):
        canvas.saveState(); canvas.setFont(font_name, 7); canvas.setFillColor(colors.HexColor("#6B7280")); canvas.drawString(12*mm, 7*mm, "Business OS | Cari Bakiyeler"); canvas.drawRightString(landscape(A4)[0]-12*mm, 7*mm, f"Sayfa {doc.page}"); canvas.restoreState()
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    output.seek(0)
    return output


def delivered_sales_collection_tracking(today=None, delivered_sales=None):
    """Teslim edilmiş satışları, cari tahsilatları düşerek sipariş bazında izler.

    Tahsilatlar aynı carinin en eski teslim edilmiş satışından başlanarak mahsup
    edilir. Bu sayede kısmi veya önceden alınmış tahsilatlar, 30 günlük vade
    listesindeki açık tutarı doğrudan azaltır.
    """
    today = today or date.today()
    if delivered_sales is None:
        delivered_sales = Order.query.options(
            selectinload(Order.items),
            selectinload(Order.history),
            joinedload(Order.customer),
        ).filter_by(order_type="Satış", status="Teslim Edildi").all()
    grouped_orders = {}
    for order in delivered_sales:
        delivered_events = [event for event in order.history if event.status == "Teslim Edildi"]
        delivered_at = min((event.created_at.date() for event in delivered_events), default=None)
        # Eski siparişlerde geçmiş kaydı olmayabilir. Bu durumda girilmiş teslim
        # tarihi, o da yoksa siparişin son güncellenme tarihi güvenli varsayımdır.
        delivered_at = delivered_at or order.delivery_date or (order.updated_at or order.created_at).date()
        grouped_orders.setdefault(order.customer_id, []).append((delivered_at, order))

    credit_types = {"Tahsilat", "Alacak Dekontu", "Alacak Devir"}
    customer_ids = list(grouped_orders)
    collections_by_customer = {customer_id: Decimal("0") for customer_id in customer_ids}
    if customer_ids:
        transactions = AccountTransaction.query.filter(
            AccountTransaction.customer_id.in_(customer_ids),
            AccountTransaction.transaction_type.in_(credit_types),
            AccountTransaction.transaction_date <= today,
        ).all()
        for transaction in transactions:
            collections_by_customer[transaction.customer_id] += transaction.credit or Decimal("0")

    items = []
    for customer_id, dated_orders in grouped_orders.items():
        dated_orders.sort(key=lambda entry: (entry[0], entry[1].id))
        available_collection = collections_by_customer[customer_id]
        for delivered_at, order in dated_orders:
            amount = order.total_amount
            collected = min(max(available_collection, Decimal("0")), amount)
            remaining = amount - collected
            available_collection -= collected
            due_date = delivered_at + timedelta(days=30)
            days_to_due = (due_date - today).days
            if remaining <= 0:
                state, state_label = "paid", "Tahsil edildi"
            elif days_to_due < 0:
                state, state_label = "overdue", f"{abs(days_to_due)} gün gecikti"
            elif days_to_due == 0:
                state, state_label = "due_today", "Bugün vadesi doluyor"
            elif days_to_due <= 7:
                state, state_label = "due_soon", f"{days_to_due} gün kaldı"
            else:
                state, state_label = "open", f"{days_to_due} gün kaldı"
            items.append({
                "order": order,
                "customer": order.customer,
                "delivered_at": delivered_at,
                "due_date": due_date,
                "amount": amount,
                "collected": collected,
                "remaining": remaining,
                "days_to_due": days_to_due,
                "state": state,
                "state_label": state_label,
            })
    return sorted(items, key=lambda item: (item["due_date"], item["order"].id))


def procurement_summary(sales_order, converted_orders=None):
    """Return quantity-based purchasing coverage for a sales order."""
    if sales_order.order_type != "Satış":
        return None
    linked_orders = converted_orders
    if linked_orders is None:
        linked_orders = Order.query.filter_by(source_order_id=sales_order.id).order_by(Order.id).all()
    active_orders = [order for order in linked_orders if order.status != "İptal Edildi"]
    required = {item.id: item.quantity for item in sales_order.items}
    covered = {item.id: 0 for item in sales_order.items}
    legacy_items = []
    for purchase in active_orders:
        for purchase_item in purchase.items:
            if purchase_item.source_order_item_id in covered:
                covered[purchase_item.source_order_item_id] += purchase_item.quantity
            else:
                legacy_items.append(purchase_item)

    # Eski dönüştürülmüş satın almalarda satır bağlantısı bulunmadığından ürün
    # kartı/adı ve ayrıntıları üzerinden geriye dönük eşleştirme yapılır.
    def item_key(item):
        return (
            item.product_id or 0,
            normalize_search_text(item.product_name),
            normalize_search_text(item.variant),
            normalize_search_text(item.detail_2),
            normalize_search_text(item.detail_3),
            normalize_search_text(item.unit),
        )

    sale_groups = {}
    for sale_item in sales_order.items:
        sale_groups.setdefault(item_key(sale_item), []).append(sale_item)
    for purchase_item in legacy_items:
        remaining = purchase_item.quantity
        for sale_item in sale_groups.get(item_key(purchase_item), []):
            available = max(required[sale_item.id] - covered[sale_item.id], 0)
            allocated = min(remaining, available)
            covered[sale_item.id] += allocated
            remaining -= allocated
            if remaining <= 0:
                break

    required_quantity = sum(required.values())
    covered_quantity = sum(min(covered[item_id], quantity) for item_id, quantity in required.items())
    if covered_quantity <= 0:
        # Bir satın alma siparişi sonradan satışa bağlanmış olabilir. Bu durumda
        # kalemler aynı olmadığı için adet eşleşmesi yapılamasa da bağlantı vardır.
        code, label = ("linked", "Satın Alma Bağlandı") if active_orders else ("none", "Satın Alma Verilmedi")
    elif covered_quantity < required_quantity:
        code, label = "partial", "Kısmi Satın Alma"
    else:
        code, label = "complete", "Satın Alma Tamam"
    return {
        "code": code,
        "label": label,
        "required_quantity": required_quantity,
        "covered_quantity": covered_quantity,
        "linked_orders": linked_orders,
        "active_orders": active_orders,
        "items": {
            item_id: {
                "required": quantity,
                "covered": min(covered[item_id], quantity),
                "remaining": max(quantity - covered[item_id], 0),
            }
            for item_id, quantity in required.items()
        },
    }


def calculate_pending_delivery_amounts(orders=None):
    """Gelecek siparişler ve açık cari bakiyeler için sipariş bazlı nakit beklentisi."""
    if orders is None:
        orders = Order.query.filter(Order.status != "İptal Edildi").all()
    balances = calculate_customer_balances({order.customer_id for order in orders})
    grouped_orders = {}
    for order in orders:
        grouped_orders.setdefault((order.customer_id, order.order_type), []).append(order)
    expected_by_order = {}
    for (customer_id, order_type), customer_orders in grouped_orders.items():
        balance = balances.get(customer_id, Decimal("0"))
        future_total = sum((order.total_amount for order in customer_orders if order.status not in FINANCIAL_ORDER_STATUSES), Decimal("0"))
        projected_total = max(future_total + balance, Decimal("0")) if order_type == "Satış" else max(future_total - balance, Decimal("0"))
        gross_total = sum((order.total_amount for order in customer_orders), Decimal("0"))
        projected_total = min(projected_total, gross_total)
        deduction = gross_total - projected_total
        customer_orders.sort(key=lambda order: (0 if order.status in FINANCIAL_ORDER_STATUSES else 1, order.order_date, order.id))
        for order in customer_orders:
            order_deduction = min(deduction, order.total_amount)
            expected_by_order[order.id] = order.total_amount - order_deduction
            deduction -= order_deduction
    return expected_by_order


def next_order_no(order_type="Satış"):
    year = date.today().year
    prefix = f"{'SS' if order_type == 'Satış' else 'SA'}-{year}-"
    latest = Order.query.filter(Order.order_no.like(f"{prefix}%")).order_by(Order.id.desc()).first()
    sequence = 1
    if latest:
        try:
            sequence = int(latest.order_no.split("-")[-1]) + 1
        except ValueError:
            sequence = Order.query.filter(Order.order_no.like(f"{prefix}%")).count() + 1
    return f"{prefix}{sequence:05d}"


def create_database_backup(app, label="automatic", target_dir=None, keep=250):
    """Create a consistent SQLite snapshot without depending on external services."""
    if db.engine.dialect.name != "sqlite":
        return None
    database_path = db.engine.url.database
    if not database_path or database_path == ":memory:" or not os.path.exists(database_path):
        return None
    backup_dir = target_dir or os.path.join(app.instance_path, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    backup_path = os.path.join(backup_dir, f"business_os_{label}_{timestamp}.db")
    source = sqlite3.connect(database_path)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    backups = sorted(
        (os.path.join(backup_dir, name) for name in os.listdir(backup_dir) if name.endswith(".db")),
        key=os.path.getmtime,
        reverse=True,
    )
    for old_backup in backups[keep:]:
        try:
            os.remove(old_backup)
        except OSError:
            pass
    return backup_path


def ensure_scheduled_backups(app):
    """Create one hourly local snapshot and one daily copy outside the project."""
    now = datetime.now()
    backup_dir = os.path.join(app.instance_path, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    hourly_prefix = f"business_os_hourly_{now.strftime('%Y-%m-%d_%H-')}"
    if not any(name.startswith(hourly_prefix) for name in os.listdir(backup_dir)):
        create_database_backup(app, "hourly", keep=250)
    archive_dir = os.path.abspath(
        os.path.expanduser(
            os.getenv("BUSINESSOS_BACKUP_DIR", "~/Documents/Business OS Yedekleri")
        )
    )
    daily_prefix = f"business_os_daily_{now.strftime('%Y-%m-%d_')}"
    try:
        os.makedirs(archive_dir, exist_ok=True)
        if not any(name.startswith(daily_prefix) for name in os.listdir(archive_dir)):
            create_database_backup(app, "daily", target_dir=archive_dir, keep=90)
    except OSError:
        # Harici arşiv klasörü kullanılamasa bile yerel saatlik yedek devam eder.
        pass


def personal_ledger_totals(person):
    sent = sum((item.sent_amount or Decimal("0") for item in person.transactions), Decimal("0"))
    received = sum((item.received_amount or Decimal("0") for item in person.transactions), Decimal("0"))
    return sent, received, received - sent


def safe_export_name(value):
    cleaned = "".join(character if character.isalnum() or character in "-_" else "-" for character in value.strip())
    return cleaned.strip("-") or "Kisi"


def order_documents_directory(app, order_id):
    directory = os.path.realpath(os.path.join(app.instance_path, "order_documents", str(order_id)))
    root = os.path.realpath(os.path.join(app.instance_path, "order_documents"))
    if not directory.startswith(root + os.sep):
        raise ValueError("Geçersiz belge klasörü")
    os.makedirs(directory, exist_ok=True)
    return directory


def order_document_path(app, document):
    directory = order_documents_directory(app, document.order_id)
    path = os.path.realpath(os.path.join(directory, document.stored_name))
    if not path.startswith(directory + os.sep):
        raise ValueError("Geçersiz belge yolu")
    return path


def customer_tax_document_directory(app, customer_id):
    directory = os.path.realpath(os.path.join(app.instance_path, "customer_tax_documents", str(customer_id)))
    root = os.path.realpath(os.path.join(app.instance_path, "customer_tax_documents"))
    if not directory.startswith(root + os.sep):
        raise ValueError("Geçersiz vergi levhası klasörü")
    os.makedirs(directory, exist_ok=True)
    return directory


def customer_tax_document_path(app, customer):
    if not customer.tax_document_stored_name:
        raise ValueError("Vergi levhası bulunamadı")
    directory = customer_tax_document_directory(app, customer.id)
    path = os.path.realpath(os.path.join(directory, customer.tax_document_stored_name))
    if not path.startswith(directory + os.sep):
        raise ValueError("Geçersiz vergi levhası yolu")
    return path


def persistent_storage_key(app, path):
    root = os.path.realpath(app.instance_path)
    resolved = os.path.realpath(path)
    if not resolved.startswith(root + os.sep):
        raise ValueError("Geçersiz kalıcı dosya yolu")
    return os.path.relpath(resolved, root).replace(os.sep, "/")


def persist_local_file(app, path, mime_type=None):
    """PostgreSQL web kurulumunda dosyanın kalıcı kopyasını veritabanına yazar."""
    if db.engine.dialect.name != "postgresql":
        return
    with open(path, "rb") as handle:
        content = handle.read()
    key = persistent_storage_key(app, path)
    stored = StoredFile.query.filter_by(storage_key=key).first()
    if stored is None:
        stored = StoredFile(storage_key=key)
        db.session.add(stored)
    stored.content = content
    stored.mime_type = mime_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
    stored.size_bytes = len(content)
    stored.sha256 = hashlib.sha256(content).hexdigest()


def stored_file_response(app, path, download_name, mime_type, as_attachment):
    if db.engine.dialect.name == "postgresql":
        stored = StoredFile.query.filter_by(storage_key=persistent_storage_key(app, path)).first()
        if stored is not None:
            return send_file(
                BytesIO(stored.content), as_attachment=as_attachment,
                download_name=download_name, mimetype=mime_type or stored.mime_type,
            )
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=as_attachment, download_name=download_name, mimetype=mime_type)


def delete_persistent_file(app, path):
    if db.engine.dialect.name == "postgresql":
        stored = StoredFile.query.filter_by(storage_key=persistent_storage_key(app, path)).first()
        if stored is not None:
            db.session.delete(stored)
    if os.path.isfile(path):
        os.remove(path)


def recognized_tax_document_text(path, extension):
    """Vergi levhasındaki dört sabit alanı macOS Vision ile yerel olarak okur."""
    source_path = path
    preview_path = None
    try:
        if extension == "pdf":
            preview_path = os.path.join(os.path.dirname(path), f".ocr-{secrets.token_urlsafe(10)}.png")
            converted = subprocess.run(["/usr/bin/sips", "-s", "format", "png", path, "--out", preview_path], capture_output=True, text=True, timeout=30)
            if converted.returncode != 0 or not os.path.isfile(preview_path):
                raise ValueError("PDF'nin ilk sayfası okunamadı")
            source_path = preview_path
        vision_script = '''
import Foundation
import Vision
let url = URL(fileURLWithPath: CommandLine.arguments[1])
// Vergi levhasındaki alanlar sabit konumdadır. Görüntünün başka kısmı
// taranmaz; koordinatlar Vision'ın sol-alt kökenli normalleştirilmiş düzlemindedir.
let fields: [(String, CGRect)] = [
    ("name", CGRect(x: 0.18, y: 0.66, width: 0.42, height: 0.14)),
    ("address", CGRect(x: 0.18, y: 0.53, width: 0.42, height: 0.15)),
    ("tax_office", CGRect(x: 0.72, y: 0.74, width: 0.27, height: 0.14)),
    ("tax_number", CGRect(x: 0.72, y: 0.65, width: 0.27, height: 0.14))
]
for (field, region) in fields {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["tr-TR", "en-US"]
    request.regionOfInterest = region
    let handler = VNImageRequestHandler(url: url, options: [:])
    try handler.perform([request])
    let value = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: " ")
    print("\\(field)\\t\\(value)")
}
'''
        result = subprocess.run(["/usr/bin/swift", "-e", vision_script, source_path], capture_output=True, text=True, timeout=45)
        if result.returncode != 0:
            raise ValueError("Belgenin metni okunamadı")
        return result.stdout.strip()
    finally:
        if preview_path and os.path.exists(preview_path):
            os.remove(preview_path)


def tax_certificate_suggestion(text_value):
    """Sadece belirtilen dört vergi levhası bölgesinden gelen OCR sonucunu önerir."""
    fields = {}
    for line in (text_value or "").splitlines():
        key, separator, value = line.partition("\t")
        if separator and key in {"name", "address", "tax_office", "tax_number"}:
            fields[key] = re.sub(r"\s+", " ", value).strip(" :-")

    result = {
        "name": fields.get("name", "")[:160],
        "address": fields.get("address", "")[:1000],
        "tax_office": fields.get("tax_office", "").replace("i", "İ").upper()[:120],
        "tax_number": "".join(re.findall(r"\d", fields.get("tax_number", "")))[:10],
    }
    return {key: value for key, value in result.items() if value}


def order_document_type_for(order):
    return "Giden Fatura" if order.order_type == "Satış" else "Gelen Fatura"


def store_order_document(app, order, upload, document_type=None, source="Manuel"):
    if not upload or not upload.filename:
        raise ValueError("Eklenecek dosya bulunamadı.")
    original_name = secure_filename(upload.filename) or "belge"
    extension = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
    if extension not in ORDER_DOCUMENT_EXTENSIONS:
        raise ValueError("Yalnızca PDF, JPG, PNG, WEBP veya HEIC dosyası ekleyebilirsiniz.")
    upload.stream.seek(0, os.SEEK_END)
    size_bytes = upload.stream.tell()
    upload.stream.seek(0)
    if size_bytes <= 0:
        raise ValueError("Boş dosya eklenemez.")
    if size_bytes > ORDER_DOCUMENT_MAX_BYTES:
        raise ValueError("Bir belge en fazla 25 MB olabilir.")
    stored_name = f"{secrets.token_urlsafe(18)}.{extension}"
    destination = os.path.join(order_documents_directory(app, order.id), stored_name)
    upload.save(destination)
    persist_local_file(app, destination, upload.mimetype or mimetypes.guess_type(original_name)[0])
    document = OrderDocument(
        order_id=order.id,
        document_type=document_type if document_type in ORDER_DOCUMENT_TYPES else order_document_type_for(order),
        original_name=original_name,
        stored_name=stored_name,
        mime_type=upload.mimetype or mimetypes.guess_type(original_name)[0] or "application/octet-stream",
        size_bytes=size_bytes,
        source=source,
    )
    db.session.add(document)
    return document


def earchive_inbox_directory(app):
    root = os.path.realpath(os.path.join(app.instance_path, "earchive_inbox"))
    os.makedirs(root, exist_ok=True)
    return root


def earchive_incoming_path(app, incoming):
    root = earchive_inbox_directory(app)
    path = os.path.realpath(os.path.join(root, incoming.stored_name))
    if not path.startswith(root + os.sep):
        raise ValueError("Geçersiz E-Arşiv belge yolu")
    return path


def suggested_order_from_document_name(name):
    match = re.search(r"\b(?:SS|SA)-\d{4}-\d{5}\b", name or "", re.IGNORECASE)
    if not match:
        return None
    order = Order.query.filter(func.upper(Order.order_no) == match.group(0).upper()).first()
    return order.id if order else None


def sync_earchive_downloads(app):
    """E-Arşiv Portal'dan indirilen, fatura adı taşıyan PDF'leri güvenli yerel kuyruğa alır."""
    downloads = os.path.expanduser("~/Downloads")
    if not os.path.isdir(downloads):
        raise ValueError("İndirilenler klasörü bulunamadı.")
    accepted_prefixes = ("gib", "earsiv", "e-arsiv", "fatura")
    added = 0
    for name in sorted(os.listdir(downloads)):
        lowered = name.casefold()
        if not lowered.endswith(".pdf") or not lowered.startswith(accepted_prefixes):
            continue
        source = os.path.join(downloads, name)
        if not os.path.isfile(source):
            continue
        size_bytes = os.path.getsize(source)
        if size_bytes <= 0 or size_bytes > ORDER_DOCUMENT_MAX_BYTES:
            continue
        digest = hashlib.sha256()
        with open(source, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        file_hash = digest.hexdigest()
        if EArchiveIncomingDocument.query.filter_by(file_hash=file_hash).first():
            continue
        stored_name = f"{secrets.token_urlsafe(18)}.pdf"
        shutil.copy2(source, os.path.join(earchive_inbox_directory(app), stored_name))
        db.session.add(EArchiveIncomingDocument(
            file_hash=file_hash, original_name=secure_filename(name) or "e-arsiv-fatura.pdf",
            stored_name=stored_name, size_bytes=size_bytes,
            suggested_order_id=suggested_order_from_document_name(name),
        ))
        added += 1
    return added


def telegram_settings_path(app):
    return os.path.join(app.instance_path, "telegram_settings.json")


def load_telegram_settings(app):
    defaults = {"allowed_chat_id": "", "pairing_code": "", "last_update_id": 0}
    try:
        with open(telegram_settings_path(app), "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        if isinstance(saved, dict):
            defaults.update({key: saved.get(key, value) for key, value in defaults.items()})
    except (OSError, json.JSONDecodeError):
        pass
    return defaults


def save_telegram_settings(app, settings):
    path = telegram_settings_path(app)
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, ensure_ascii=False)
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, path)


def telegram_bot_token():
    supplied = os.getenv("BUSINESSOS_TELEGRAM_BOT_TOKEN", "").strip()
    if supplied:
        return supplied
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", TELEGRAM_KEYCHAIN_SERVICE, "-a", TELEGRAM_KEYCHAIN_ACCOUNT, "-w"],
            text=True, capture_output=True, timeout=5, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def telegram_inbox_directory(app):
    root = os.path.realpath(os.path.join(app.instance_path, "telegram_inbox"))
    os.makedirs(root, exist_ok=True)
    return root


def telegram_incoming_path(app, incoming):
    root = telegram_inbox_directory(app)
    path = os.path.realpath(os.path.join(root, incoming.stored_name))
    if not path.startswith(root + os.sep):
        raise ValueError("Geçersiz Telegram belge yolu")
    return path


def telegram_api_get(token, method, query=None, timeout=25):
    query_string = urllib.parse.urlencode(query or {})
    endpoint = f"https://api.telegram.org/bot{token}/{method}"
    if query_string:
        endpoint = f"{endpoint}?{query_string}"
    with urllib.request.urlopen(endpoint, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise ValueError(payload.get("description") or "Telegram isteği başarısız oldu.")
    return payload.get("result")


def telegram_suggested_order_id(caption):
    match = re.search(r"\b(?:SS|SA)-\d{4}-\d{5}\b", caption or "", re.IGNORECASE)
    if not match:
        return None
    order = Order.query.filter(func.upper(Order.order_no) == match.group(0).upper()).first()
    return order.id if order else None


def sync_telegram_documents(app):
    """Read permitted Telegram messages only when the user presses refresh."""
    token = telegram_bot_token()
    if not token:
        raise ValueError("Telegram bot anahtarı bu bilgisayarda bulunamadı.")
    settings = load_telegram_settings(app)
    if not settings.get("pairing_code"):
        settings["pairing_code"] = secrets.token_hex(3).upper()
        save_telegram_settings(app, settings)
    updates = telegram_api_get(token, "getUpdates", {"offset": int(settings.get("last_update_id") or 0) + 1, "timeout": 0})
    added = paired = 0
    for update in updates or []:
        update_id = int(update.get("update_id") or 0)
        settings["last_update_id"] = max(int(settings.get("last_update_id") or 0), update_id)
        message = update.get("message") or update.get("channel_post") or {}
        chat_id = str((message.get("chat") or {}).get("id") or "")
        content = str(message.get("text") or message.get("caption") or "").strip()
        if chat_id and settings["pairing_code"].casefold() in content.casefold():
            settings["allowed_chat_id"] = chat_id
            paired += 1
            # Eşleştirme, aynı güncellemedeki belge indirmesi başarısız olsa
            # bile kaybolmamalıdır. Anahtar yalnızca bu yerel ayar dosyasında
            # tutulur; yedeklere veya Git'e yazılmaz.
            save_telegram_settings(app, settings)
        if not chat_id or chat_id != str(settings.get("allowed_chat_id") or ""):
            continue
        attachment = message.get("document") or (message.get("photo") or [None])[-1]
        if not attachment or TelegramIncomingDocument.query.filter_by(telegram_update_id=str(update_id)).first():
            continue
        filename = str(attachment.get("file_name") or f"telegram-belge-{update_id}.jpg")
        safe_name = secure_filename(filename) or f"telegram-belge-{update_id}.jpg"
        extension = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
        if extension not in ORDER_DOCUMENT_EXTENSIONS:
            continue
        size_bytes = int(attachment.get("file_size") or 0)
        if size_bytes <= 0 or size_bytes > ORDER_DOCUMENT_MAX_BYTES:
            continue
        stored_name = f"{secrets.token_urlsafe(18)}.{extension}"
        destination = os.path.join(telegram_inbox_directory(app), stored_name)
        try:
            file_info = telegram_api_get(token, "getFile", {"file_id": attachment.get("file_id")})
            file_path = str((file_info or {}).get("file_path") or "")
            if not file_path:
                raise ValueError("Telegram belge dosya yolu alınamadı.")
            download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
            with urllib.request.urlopen(download_url, timeout=35) as response, open(destination, "wb") as output:
                shutil.copyfileobj(response, output)
            actual_size = os.path.getsize(destination)
            if actual_size <= 0 or actual_size > ORDER_DOCUMENT_MAX_BYTES:
                raise ValueError("Belge boyutu desteklenen sınırın dışında.")
            db.session.add(TelegramIncomingDocument(
                telegram_update_id=str(update_id), chat_id=chat_id,
                telegram_message_id=str(message.get("message_id") or ""),
                original_name=safe_name, stored_name=stored_name,
                mime_type=str(attachment.get("mime_type") or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"),
                size_bytes=actual_size, caption=content[:500], suggested_order_id=telegram_suggested_order_id(content),
            ))
            added += 1
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError):
            if os.path.isfile(destination):
                os.remove(destination)
    save_telegram_settings(app, settings)
    return added, paired


def build_personal_ledger_pdf(person):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    regular_font = "/System/Library/Fonts/Supplemental/Arial.ttf"
    bold_font = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
    font_name, bold_name = "Helvetica", "Helvetica-Bold"
    if os.path.isfile(regular_font) and os.path.isfile(bold_font):
        pdfmetrics.registerFont(TTFont("BusinessArial", regular_font))
        pdfmetrics.registerFont(TTFont("BusinessArial-Bold", bold_font))
        font_name, bold_name = "BusinessArial", "BusinessArial-Bold"

    stream = BytesIO()
    document = SimpleDocTemplate(stream, pagesize=landscape(A4), leftMargin=15*mm, rightMargin=15*mm,
                                 topMargin=15*mm, bottomMargin=15*mm, title=f"{person.name} Borç-Alacak Ekstresi")
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleTR", parent=styles["Title"], fontName=bold_name, fontSize=18, leading=22, textColor=colors.HexColor("#14213d"), alignment=TA_LEFT)
    meta_style = ParagraphStyle("MetaTR", parent=styles["BodyText"], fontName=font_name, fontSize=8, textColor=colors.HexColor("#6b7280"))
    cell_style = ParagraphStyle("CellTR", parent=styles["BodyText"], fontName=font_name, fontSize=8, leading=10)
    cell_right = ParagraphStyle("CellRightTR", parent=cell_style, alignment=TA_RIGHT)
    cell_center = ParagraphStyle("CellCenterTR", parent=cell_style, alignment=TA_CENTER)
    header_left = ParagraphStyle("HeaderLeftTR", parent=cell_style, fontName=bold_name, textColor=colors.white)
    header_right = ParagraphStyle("HeaderRightTR", parent=header_left, alignment=TA_RIGHT)
    header_center = ParagraphStyle("HeaderCenterTR", parent=header_left, alignment=TA_CENTER)
    sent, received, balance = personal_ledger_totals(person)

    def amount(value):
        return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " TL"

    if balance > 0:
        balance_text = f"Siz {person.name} kişisine {amount(balance)} borçlusunuz."
    elif balance < 0:
        balance_text = f"{person.name} size {amount(-balance)} borçlu."
    else:
        balance_text = f"{person.name} ile hesabınız dengede."

    story = [Paragraph(f"{person.name} - Borç / Alacak Ekstresi", title_style),
             Paragraph(f"Oluşturulma: {datetime.now().strftime('%d.%m.%Y %H:%M')} | Business OS", meta_style), Spacer(1, 5*mm)]
    summary = Table([
        ["TOPLAM GÖNDERİLEN", "TOPLAM GELEN", "NET FARK"],
        [amount(sent), amount(received), amount(abs(balance))],
        ["", "", balance_text],
    ], colWidths=[85*mm, 85*mm, 85*mm])
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#e8eef9")), ("TEXTCOLOR", (0,0), (-1,0), colors.HexColor("#526074")),
        ("FONTNAME", (0,0), (-1,0), bold_name), ("FONTSIZE", (0,0), (-1,0), 8),
        ("FONTNAME", (0,1), (-1,1), bold_name), ("FONTSIZE", (0,1), (-1,1), 14),
        ("FONTNAME", (0,2), (-1,2), font_name), ("FONTSIZE", (0,2), (-1,2), 8),
        ("ALIGN", (0,0), (-1,-1), "CENTER"), ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("BOX", (0,0), (-1,-1), .5, colors.HexColor("#c9d3e3")), ("INNERGRID", (0,0), (-1,-1), .25, colors.HexColor("#dce3ec")),
        ("TOPPADDING", (0,0), (-1,-1), 6), ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.extend([KeepTogether(summary), Spacer(1, 6*mm)])
    rows = [[Paragraph("TARİH", header_center), Paragraph("AÇIKLAMA", header_left), Paragraph("GÖNDERİLEN PARA", header_right), Paragraph("GELEN PARA", header_right)]]
    for item in person.transactions:
        rows.append([Paragraph(item.transaction_date.strftime("%d.%m.%Y") if item.transaction_date else "-", cell_center),
                     Paragraph(item.description or "-", cell_style),
                     Paragraph(amount(item.sent_amount or Decimal("0")), cell_right),
                     Paragraph(amount(item.received_amount or Decimal("0")), cell_right)])
    rows.append(["", Paragraph("TOPLAM", ParagraphStyle("TotalLabel", parent=cell_style, fontName=bold_name)),
                 Paragraph(amount(sent), ParagraphStyle("TotalSent", parent=cell_right, fontName=bold_name)),
                 Paragraph(amount(received), ParagraphStyle("TotalReceived", parent=cell_right, fontName=bold_name))])
    ledger_table = Table(rows, colWidths=[31*mm, 129*mm, 48*mm, 48*mm], repeatRows=1)
    ledger_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#14213d")), ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), bold_name), ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0,1), (-1,-2), [colors.white, colors.HexColor("#f6f8fb")]),
        ("LINEBELOW", (0,0), (-1,-2), .25, colors.HexColor("#dce3ec")),
        ("BACKGROUND", (0,-1), (-1,-1), colors.HexColor("#e8eef9")), ("LINEABOVE", (0,-1), (-1,-1), 1, colors.HexColor("#14213d")),
        ("TOPPADDING", (0,0), (-1,-1), 5), ("BOTTOMPADDING", (0,0), (-1,-1), 5),
    ]))
    story.append(ledger_table)

    def footer(canvas, doc):
        canvas.saveState(); canvas.setFont(font_name, 7); canvas.setFillColor(colors.HexColor("#6b7280"))
        canvas.drawString(15*mm, 8*mm, f"Business OS | {person.name} Borç-Alacak Ekstresi")
        canvas.drawRightString(landscape(A4)[0]-15*mm, 8*mm, f"Sayfa {doc.page}"); canvas.restoreState()
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    stream.seek(0)
    return stream


def build_personal_ledger_xlsx(person):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Borç-Alacak Ekstresi"
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "A8"
    sent, received, balance = personal_ledger_totals(person)
    navy, blue, pale, green, line = "14213D", "2563EB", "E8EEF9", "14735A", "DCE3EC"
    thin = Side(style="thin", color=line)
    sheet.merge_cells("A1:D1"); sheet["A1"] = f"{person.name} - Borç / Alacak Ekstresi"
    sheet["A1"].font = Font(name="Arial", size=18, bold=True, color=navy); sheet["A1"].alignment = Alignment(horizontal="left")
    sheet.row_dimensions[1].height = 30
    sheet.merge_cells("A2:D2"); sheet["A2"] = f"Oluşturulma: {datetime.now().strftime('%d.%m.%Y %H:%M')} | Business OS"
    sheet["A2"].font = Font(name="Arial", size=9, color="6B7280")
    sheet["A4"], sheet["B4"], sheet["C4"], sheet["D4"] = "TOPLAM GÖNDERİLEN", "TOPLAM GELEN", "NET FARK", "GÜNCEL DURUM"
    for cell in sheet[4]:
        cell.fill = PatternFill("solid", fgColor=pale); cell.font = Font(name="Arial", size=9, bold=True, color="526074"); cell.alignment = Alignment(horizontal="center")
    start_row = 8
    end_data_row = start_row + len(person.transactions) - 1
    total_row = max(end_data_row + 1, start_row)
    sheet["A5"] = f"=C{total_row}"; sheet["B5"] = f"=D{total_row}"; sheet["C5"] = "=ABS(B5-A5)"
    if balance > 0: status = f"Siz {person.name} kişisine borçlusunuz"
    elif balance < 0: status = f"{person.name} size borçlu"
    else: status = "Hesap dengede"
    sheet["D5"] = status
    for cell in sheet[5]:
        cell.font = Font(name="Arial", size=12, bold=True, color=green if cell.column != 4 else navy); cell.alignment = Alignment(horizontal="center")
    sheet.row_dimensions[5].height = 25
    headers = ["Tarih", "Açıklama", "Gönderilen Para", "Gelen Para"]
    for column, header in enumerate(headers, 1):
        cell = sheet.cell(start_row-1, column, header); cell.fill = PatternFill("solid", fgColor=navy); cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF"); cell.alignment = Alignment(horizontal="center")
    sheet.row_dimensions[start_row-1].height = 24
    for row_index, item in enumerate(person.transactions, start_row):
        sheet.cell(row_index, 1, item.transaction_date)
        sheet.cell(row_index, 2, item.description)
        sheet.cell(row_index, 3, float(item.sent_amount or 0))
        sheet.cell(row_index, 4, float(item.received_amount or 0))
        for cell in sheet[row_index]: cell.border = Border(bottom=thin); cell.font = Font(name="Arial", size=10)
    sheet.cell(total_row, 2, "TOPLAM")
    sheet.cell(total_row, 3, f"=SUM(C{start_row}:C{end_data_row})" if person.transactions else "=0")
    sheet.cell(total_row, 4, f"=SUM(D{start_row}:D{end_data_row})" if person.transactions else "=0")
    for cell in sheet[total_row]: cell.fill = PatternFill("solid", fgColor=pale); cell.font = Font(name="Arial", size=10, bold=True); cell.border = Border(top=Side(style="medium", color=navy))
    currency_format = '₺#,##0.00;[Red]-₺#,##0.00;₺-'
    for row in range(5, total_row+1):
        for column in (3,4): sheet.cell(row,column).number_format = currency_format
    for cell in (sheet["A5"], sheet["B5"], sheet["C5"]): cell.number_format = currency_format
    for row in range(start_row, end_data_row+1): sheet.cell(row,1).number_format = "dd.mm.yyyy"
    sheet.column_dimensions["A"].width = 16; sheet.column_dimensions["B"].width = 55; sheet.column_dimensions["C"].width = 23; sheet.column_dimensions["D"].width = 36
    sheet.auto_filter.ref = f"A{start_row-1}:D{max(end_data_row,start_row-1)}"
    sheet.sheet_properties.pageSetUpPr.fitToPage = True; sheet.page_setup.orientation = "landscape"; sheet.page_setup.fitToWidth = 1; sheet.page_setup.fitToHeight = 0
    sheet.print_title_rows = f"1:{start_row-1}"; sheet.print_area = f"A1:D{total_row}"
    workbook.calculation.fullCalcOnLoad = True; workbook.calculation.forceFullCalc = True; workbook.calculation.calcMode = "auto"
    stream = BytesIO(); workbook.save(stream); stream.seek(0)
    return stream


def product_cost(product_id):
    product = db.session.get(Product, product_id) if product_id else None
    return product.purchase_price if product else Decimal("0")


def order_realization_date(order):
    events = [event.created_at.date() for event in order.history if event.status in FINANCIAL_ORDER_STATUSES]
    return min(events) if events else (order.updated_at or order.created_at).date()


def latest_delivered_purchase_cost(item, cutoff_date=None):
    """Return the latest realized purchase price for the same product."""
    query = OrderItem.query.join(Order).filter(
        Order.order_type == "Satın Alma",
        Order.status.in_(FINANCIAL_ORDER_STATUSES),
    )
    if item.product_id:
        query = query.filter(OrderItem.product_id == item.product_id)
    else:
        query = query.filter(db.func.normalize_tr(OrderItem.product_name) == normalize_search_text(item.product_name))
    candidates = query.all()
    if cutoff_date:
        candidates = [candidate for candidate in candidates if order_realization_date(candidate.order) <= cutoff_date]
    if not candidates:
        return Decimal("0")
    latest = max(candidates, key=lambda candidate: (order_realization_date(candidate.order), candidate.order.id, candidate.id))
    # KDV dahil alış girildiyse ve/veya iskonto uygulanmışsa maliyet hesabı
    # için vergi hariç, iskontolu net birim bedeli kullanılır.
    return latest.net_amount / latest.quantity if latest.quantity else Decimal("0")


def effective_sales_item_cost(item, sale_date=None):
    # Kârlılıkta ürün kartındaki sabit alış fiyatı kullanılmaz. Satış tarihine
    # kadar gerçekleşmiş en son satın alma siparişinin net birim fiyatı alınır.
    return latest_delivered_purchase_cost(item, sale_date)


def create_app(test_config=None):
    data_directory = os.getenv("BUSINESSOS_DATA_DIR", "").strip()
    flask_options = {"instance_relative_config": True}
    if data_directory:
        flask_options["instance_path"] = os.path.abspath(os.path.expanduser(data_directory))
    app = Flask(__name__, **flask_options)
    os.makedirs(app.instance_path, exist_ok=True)
    database_url = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(app.instance_path, 'business_os.db')}")
    if database_url.startswith(("postgresql://", "postgres://")):
        database_url = "postgresql+psycopg://" + database_url.split("://", 1)[1]
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", "development-key-change-in-production"),
        SQLALCHEMY_DATABASE_URI=database_url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=ORDER_DOCUMENT_MAX_BYTES,
    )
    if test_config:
        app.config.update(test_config)
    db.init_app(app)
    install_web_auth(app)

    @app.before_request
    def scheduled_database_backup():
        ensure_scheduled_backups(app)

    @app.template_filter("money")
    def money(value):
        return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    @app.route("/yonetim/neon-aktarimi", methods=["GET", "POST"])
    def neon_database_migration():
        if not app.config.get("WEB_AUTH_ENABLED") or db.engine.dialect.name != "postgresql":
            abort(404)
        error = None
        result = None
        if request.method == "POST":
            upload = request.files.get("database")
            if request.form.get("confirmation", "").strip() != "AKTAR":
                error = "Devam etmek için onay alanına AKTAR yazın."
            elif not upload or not upload.filename:
                error = "Aktarılacak SQLite yedeğini seçin."
            else:
                temporary_path = None
                try:
                    temporary_directory = tempfile.mkdtemp(prefix="businessos-neon-")
                    temporary_path = os.path.join(temporary_directory, "business_os.db")
                    if upload.filename.lower().endswith(".zip"):
                        archive_path = os.path.join(temporary_directory, "migration.zip")
                        upload.save(archive_path)
                        with zipfile.ZipFile(archive_path) as archive:
                            allowed_roots = {"order_documents", "customer_tax_documents", "telegram_inbox", "earchive_inbox"}
                            for member in archive.infolist():
                                parts = member.filename.replace("\\", "/").strip("/").split("/")
                                if member.is_dir():
                                    continue
                                if parts == ["business_os.db"]:
                                    destination = temporary_path
                                elif parts and parts[0] in allowed_roots and all(part not in {"", ".", ".."} for part in parts):
                                    destination = os.path.join(temporary_directory, *parts)
                                else:
                                    continue
                                os.makedirs(os.path.dirname(destination), exist_ok=True)
                                with archive.open(member) as source, open(destination, "wb") as target:
                                    shutil.copyfileobj(source, target)
                    else:
                        upload.save(temporary_path)
                    if not os.path.isfile(temporary_path):
                        raise MigrationError("Aktarım paketinde business_os.db bulunamadı.")
                    result = migrate_sqlite_database(temporary_path, db.engine, db.metadata, file_root=temporary_directory)
                except (MigrationError, sqlite3.Error, ValueError, zipfile.BadZipFile) as exc:
                    error = str(exc)
                finally:
                    if temporary_path:
                        shutil.rmtree(os.path.dirname(temporary_path), ignore_errors=True)
        return render_template("database_migration.html", error=error, result=result), 400 if error else 200

    @app.get("/")
    def dashboard():
        today = date.today()
        all_orders = Order.query.options(
            selectinload(Order.items),
            selectinload(Order.history),
            joinedload(Order.customer),
        ).all()
        recent_orders = sorted(all_orders, key=lambda order: order.created_at, reverse=True)[:7]
        active_count = sum(order.status not in {"Teslim Edildi", "İptal Edildi"} for order in all_orders)
        today_orders = [order for order in all_orders if order.order_date == today and order.status != "İptal Edildi"]
        today_sales = [order for order in today_orders if order.order_type == "Satış"]
        today_purchases = [order for order in today_orders if order.order_type == "Satın Alma"]
        status_groups = {}
        for order_type in ORDER_TYPES:
            counts = []
            for status in ORDER_STATUSES:
                count = sum(order.order_type == order_type and order.status == status for order in all_orders)
                if count:
                    counts.append({"name": status, "count": count})
            status_groups[order_type] = {"items": counts, "max": max((item["count"] for item in counts), default=1), "total": sum(item["count"] for item in counts)}
        exit_orders_by_date = {}
        completed_orders = [order for order in all_orders if order.status in {"Sevk Edildi", "Teslim Edildi"}]
        for order in completed_orders:
            exit_events = [event for event in order.history if event.status in ["Sevk Edildi", "Teslim Edildi"]]
            exit_date = min((event.created_at.date() for event in exit_events), default=(order.updated_at or order.created_at).date())
            exit_orders_by_date.setdefault(exit_date, []).append(order)
        week_activity = []
        for days_ago in range(6, -1, -1):
            activity_date = today - timedelta(days=days_ago)
            day_orders = exit_orders_by_date.get(activity_date, [])
            sales_count = sum(1 for order in day_orders if order.order_type == "Satış")
            purchase_count = sum(1 for order in day_orders if order.order_type == "Satın Alma")
            sales_amount = sum((order.total_amount for order in day_orders if order.order_type == "Satış"), Decimal("0"))
            purchase_amount = sum((order.total_amount for order in day_orders if order.order_type == "Satın Alma"), Decimal("0"))
            week_activity.append({"date": activity_date, "label": activity_date.strftime("%d.%m"), "sales": sales_count, "purchases": purchase_count, "total": len(day_orders), "sales_amount": sales_amount, "purchase_amount": purchase_amount})
        week_max = max((max(item["sales_amount"], item["purchase_amount"]) for item in week_activity), default=Decimal("1")) or Decimal("1")
        delivered_today = db.session.query(OrderHistory.order_id).filter(OrderHistory.status == "Teslim Edildi", db.func.date(OrderHistory.created_at) == today).distinct().count()
        overdue_count = Order.query.filter(Order.delivery_date < today, ~Order.status.in_(["Teslim Edildi", "İptal Edildi"])).count()
        due_today_count = Order.query.filter(Order.delivery_date == today, ~Order.status.in_(["Teslim Edildi", "İptal Edildi"])).count()
        customer_balances = calculate_customer_balances()
        collection_tracking = delivered_sales_collection_tracking(
            today,
            [order for order in all_orders if order.order_type == "Satış" and order.status == "Teslim Edildi"],
        )
        open_collection_tracking = [item for item in collection_tracking if item["remaining"] > 0]
        overdue_collections = [item for item in open_collection_tracking if item["state"] == "overdue"]
        due_soon_collections = [item for item in open_collection_tracking if item["state"] in {"due_today", "due_soon"}]
        total_debit_balance = sum((balance for balance in customer_balances.values() if balance > 0), Decimal("0"))
        total_credit_balance = sum((-balance for balance in customer_balances.values() if balance < 0), Decimal("0"))
        month_start = today.replace(day=1)
        today_expense = db.session.query(func.coalesce(func.sum(Expense.amount), 0)).filter(Expense.expense_date == today).scalar()
        month_expense = db.session.query(func.coalesce(func.sum(Expense.amount), 0)).filter(Expense.expense_date >= month_start, Expense.expense_date <= today).scalar()
        recurring_expenses = RecurringExpense.query.filter_by(active=True).all()
        recurring_due = sorted(
            ({"expense": item, "due_date": item.due_date_for(today), "recorded": item.is_recorded_for(today)} for item in recurring_expenses),
            key=lambda item: item["due_date"],
        )
        cashflow_orders = [order for order in all_orders if order.status != "İptal Edildi"]
        pending_expected = calculate_pending_delivery_amounts(cashflow_orders)
        pending_delivery_sales = [order for order in cashflow_orders if order.order_type == "Satış" and pending_expected.get(order.id, 0) > 0]
        pending_delivery_purchases = [order for order in cashflow_orders if order.order_type == "Satın Alma" and pending_expected.get(order.id, 0) > 0]
        treasury = calculate_treasury(today)
        month_end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
        forecast_sales_orders = [
            order for order in all_orders
            if order.order_type == "Satış"
            and order.status not in FINANCIAL_ORDER_STATUSES + ["İptal Edildi"]
            and (order.delivery_date is None or order.delivery_date <= month_end)
        ]
        forecast_sales_by_customer = {}
        for order in forecast_sales_orders:
            forecast_sales_by_customer.setdefault(order.customer_id, Decimal("0"))
            forecast_sales_by_customer[order.customer_id] += order.total_amount
        forecast_sales_amount = sum(
            (max(total + min(customer_balances.get(customer_id, Decimal("0")), Decimal("0")), Decimal("0")) for customer_id, total in forecast_sales_by_customer.items()),
            Decimal("0"),
        )
        month_checks = AccountTransaction.query.filter(
            AccountTransaction.payment_method == "Çek",
            AccountTransaction.check_status == "Bekliyor",
            AccountTransaction.check_due_date >= today,
            AccountTransaction.check_due_date <= month_end,
        ).all()
        forecast_incoming_checks = sum((item.credit or Decimal("0") for item in month_checks if item.transaction_type == "Tahsilat"), Decimal("0"))
        forecast_outgoing_checks = sum((item.debit or Decimal("0") for item in month_checks if item.transaction_type == "Ödeme"), Decimal("0"))
        forecast_purchase_orders = [
            order for order in all_orders
            if order.order_type == "Satın Alma"
            and order.status not in FINANCIAL_ORDER_STATUSES + ["İptal Edildi"]
            and (order.delivery_date is None or order.delivery_date <= month_end)
        ]
        forecast_purchases_by_supplier = {}
        for order in forecast_purchase_orders:
            forecast_purchases_by_supplier.setdefault(order.customer_id, Decimal("0"))
            forecast_purchases_by_supplier[order.customer_id] += order.total_amount
        supplier_ids = {customer_id for (customer_id,) in db.session.query(Order.customer_id).filter(Order.order_type == "Satın Alma").distinct().all()}
        forecast_supplier_debt = sum(
            (max(forecast_purchases_by_supplier.get(customer_id, Decimal("0")) - customer_balances.get(customer_id, Decimal("0")), Decimal("0")) for customer_id in supplier_ids),
            Decimal("0"),
        )
        forecast_recurring_expenses = sum((item["expense"].amount for item in recurring_due if not item["recorded"]), Decimal("0"))
        forecast_net = treasury["cash_balance"] + forecast_sales_amount + forecast_incoming_checks - forecast_supplier_debt - forecast_recurring_expenses - forecast_outgoing_checks
        return render_template(
            "dashboard.html",
            today=today,
            recent_orders=recent_orders,
            customer_count=Customer.query.count(),
            product_count=Product.query.filter_by(active=True).count(),
            active_count=active_count,
            completed_count=sum(order.status == "Teslim Edildi" for order in all_orders),
            today_sales_count=len(today_sales),
            today_sales_amount=sum((order.total_amount for order in today_sales), Decimal("0")),
            today_purchase_count=len(today_purchases),
            today_purchase_amount=sum((order.total_amount for order in today_purchases), Decimal("0")),
            today_quantity=sum(order.total_quantity for order in today_orders),
            delivered_today=delivered_today,
            due_today_count=due_today_count,
            overdue_count=overdue_count,
            total_debit_balance=total_debit_balance,
            total_credit_balance=total_credit_balance,
            net_account_balance=total_debit_balance - total_credit_balance,
            overdue_collection_count=len(overdue_collections),
            overdue_collection_amount=sum((item["remaining"] for item in overdue_collections), Decimal("0")),
            due_soon_collection_count=len(due_soon_collections),
            due_soon_collection_amount=sum((item["remaining"] for item in due_soon_collections), Decimal("0")),
            debit_customer_count=sum(1 for balance in customer_balances.values() if balance > 0),
            credit_customer_count=sum(1 for balance in customer_balances.values() if balance < 0),
            today_expense=today_expense,
            month_expense=month_expense,
            recurring_due=recurring_due,
            recurring_pending_count=sum(1 for item in recurring_due if not item["recorded"]),
            recurring_pending_amount=sum((item["expense"].amount for item in recurring_due if not item["recorded"]), Decimal("0")),
            cash_forecast={
                "month_end": month_end,
                "cash": treasury["cash_balance"],
                "sales": forecast_sales_amount,
                "incoming_checks": forecast_incoming_checks,
                "supplier_debt": forecast_supplier_debt,
                "recurring_expenses": forecast_recurring_expenses,
                "outgoing_checks": forecast_outgoing_checks,
                "net": forecast_net,
            },
            pending_delivery_sales_count=len(pending_delivery_sales),
            pending_delivery_sales_amount=sum((pending_expected[order.id] for order in pending_delivery_sales), Decimal("0")),
            pending_delivery_purchases_count=len(pending_delivery_purchases),
            pending_delivery_purchases_amount=sum((pending_expected[order.id] for order in pending_delivery_purchases), Decimal("0")),
            treasury=treasury,
            status_groups=status_groups,
            week_activity=week_activity,
            week_max=week_max,
        )

    def collection_tracking_context():
        from collection_tracking import customer_summaries, filter_customers
        today = date.today()
        selected_state = request.args.get("state", "open").strip()
        query = request.args.get("q", "").strip()
        all_items = delivered_sales_collection_tracking(today)
        if selected_state not in {"open", "overdue", "due_soon", "paid", "all"}:
            selected_state = "open"
        all_customers = customer_summaries(all_items, today)
        customers = filter_customers(all_customers, selected_state, query, normalize_search_text)
        items = [item for group in customers for item in group['orders']]
        summary = {
            "open_amount": sum((item["remaining"] for item in all_items if item["remaining"] > 0), Decimal("0")),
            "overdue_amount": sum((item["remaining"] for item in all_items if item["remaining"] > 0 and item["state"] == "overdue"), Decimal("0")),
            "overdue_count": sum(1 for item in all_items if item["remaining"] > 0 and item["state"] == "overdue"),
            "due_soon_amount": sum((item["remaining"] for item in all_items if item["remaining"] > 0 and item["state"] in {"due_today", "due_soon"}), Decimal("0")),
            "due_soon_count": sum(1 for item in all_items if item["remaining"] > 0 and item["state"] in {"due_today", "due_soon"}),
        }
        summary['overdue_customers'] = sum(g['overdue_amount'] > 0 for g in all_customers)
        summary['due_soon_customers'] = sum(g['due_soon_amount'] > 0 for g in all_customers)
        return dict(items=items, customers=customers, summary=summary, selected_state=selected_state, query=query, today=today)

    @app.get("/tahsilat-takibi")
    def collection_tracking():
        return render_template("collection_tracking.html", **collection_tracking_context())

    @app.get("/tahsilat-takibi/<file_format>")
    def collection_tracking_export(file_format):
        if file_format not in {"excel", "pdf"}:
            abort(404)
        view = request.args.get('view', 'summary')
        if view not in {'summary', 'details'}:
            abort(400)
        if view == 'summary':
            from collection_summary_exports import export_excel, export_pdf
        else:
            from collection_exports import export_excel, export_pdf
        context = collection_tracking_context()
        output = (export_excel if file_format == "excel" else export_pdf)(context)
        extension = "xlsx" if file_format == "excel" else "pdf"
        mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if file_format == "excel" else "application/pdf"
        label = 'Cari' if view == 'summary' else 'Siparis-Ayrinti'
        return send_file(output, as_attachment=True, download_name=f"Tahsilat-Takibi-{label}-{context['today'].isoformat()}.{extension}", mimetype=mimetype)

    @app.get("/yedekler")
    def backups():
        backup_dir = os.path.join(app.instance_path, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        files = []
        backup_names = [item for item in os.listdir(backup_dir) if item.endswith(".db")]
        backup_names.sort(key=lambda item: os.path.getmtime(os.path.join(backup_dir, item)), reverse=True)
        for name in backup_names[:80]:
            path = os.path.join(backup_dir, name)
            files.append({"name": name, "size": os.path.getsize(path), "modified": datetime.fromtimestamp(os.path.getmtime(path))})
        archive_dir = os.path.expanduser("~/Documents/Business OS Yedekleri")
        return render_template("backups.html", files=files, archive_dir=archive_dir)

    @app.route("/faturalar", methods=["GET", "POST"])
    def invoices():
        products = Product.query.filter_by(active=True).order_by(Product.name).all()
        customers = Customer.query.order_by(Customer.name).all()
        orders = Order.query.order_by(Order.order_date.desc(), Order.id.desc()).limit(500).all()
        prefill_order = InvoicePrefillPlaceholder()
        prefill_order_id = request.args.get("order_id", type=int)
        if request.method == "GET" and prefill_order_id:
            selected_order = db.session.get(Order, prefill_order_id)
            if selected_order:
                if any(not item.product or not item.product.active for item in selected_order.items):
                    flash("Siparişte stok kartı eksik veya pasif olan kalem var. Önce bu ürünleri stok kartına bağlayın.", "error")
                    return redirect(url_for("order_detail", order_id=selected_order.id))
                prefill_order = selected_order
        if request.method == "POST":
            invoice_no = request.form.get("invoice_no", "").strip().upper()
            invoice_type = request.form.get("invoice_type", "")
            customer_ref = request.form.get("customer_ref", "").strip()
            customer_by_ref = {}
            for listed_customer in customers:
                customer_by_ref[normalize_search_text(listed_customer.name)] = listed_customer
                if listed_customer.code:
                    customer_by_ref[normalize_search_text(listed_customer.code)] = listed_customer
                    customer_by_ref[normalize_search_text(f"{listed_customer.name} · {listed_customer.code}")] = listed_customer
            customer = customer_by_ref.get(normalize_search_text(customer_ref)) if customer_ref else None
            order_ref = request.form.get("order_ref", "").strip()
            order_by_ref = {}
            for order in orders:
                order_by_ref[normalize_search_text(f"{order.order_no} · {order.order_type} · {order.customer.name}")] = order
                order_by_ref[normalize_search_text(order.order_no)] = order
            linked_order = order_by_ref.get(normalize_search_text(order_ref)) if order_ref else None
            product_by_ref = {}
            for product in products:
                product_by_ref[normalize_search_text(f"{product.code or 'Kodsuz'} · {product.name}")] = product
                product_by_ref[normalize_search_text(product.name)] = product
                if product.code:
                    product_by_ref[normalize_search_text(product.code)] = product
            lines = []
            errors = []
            refs = request.form.getlist("product_ref[]")
            quantities = request.form.getlist("quantity[]")
            prices = request.form.getlist("unit_price[]")
            discount_rates = request.form.getlist("discount_rate[]")
            vat_rates = request.form.getlist("vat_rate[]")
            vat_modes = request.form.getlist("vat_included[]")
            for index, product_ref in enumerate(refs):
                product_ref = product_ref.strip()
                if not product_ref and not (quantities[index].strip() if index < len(quantities) else ""):
                    continue
                product = product_by_ref.get(normalize_search_text(product_ref))
                quantity = int(parse_money(quantities[index])) if index < len(quantities) and quantities[index].strip() else 0
                unit_price = parse_money(prices[index]) if index < len(prices) else Decimal("0")
                discount_rate = parse_money(discount_rates[index]) if index < len(discount_rates) else Decimal("0")
                vat_rate = parse_money(vat_rates[index]) if index < len(vat_rates) else Decimal("0")
                if not product:
                    errors.append(f"{index + 1}. satırdaki ürün stok kartından seçilmelidir.")
                elif quantity < 1:
                    errors.append(f"{product.name} için adet en az 1 olmalıdır.")
                elif unit_price < 0 or discount_rate < 0 or discount_rate > 100 or vat_rate < 0 or vat_rate > 100:
                    errors.append(f"{product.name} satırındaki fiyat, iskonto veya KDV geçersiz.")
                else:
                    lines.append({"product": product, "quantity": quantity, "unit_price": unit_price, "discount_rate": discount_rate, "vat_rate": vat_rate, "vat_included": index < len(vat_modes) and vat_modes[index] == "1"})
            duplicate = Invoice.query.filter_by(invoice_no=invoice_no).first() if invoice_no else None
            existing_order_invoice_count = Invoice.query.filter_by(order_id=linked_order.id).count() if linked_order else 0
            if not invoice_no:
                errors.append("Fatura numarası zorunludur.")
            elif duplicate:
                errors.append("Bu fatura numarası zaten kayıtlı.")
            if invoice_type not in {"Satış", "Satın Alma"}:
                errors.append("Fatura türünü seçin.")
            if not customer:
                errors.append("Geçerli bir cari adı veya kodu yazın.")
            if order_ref and not linked_order:
                errors.append("Bağlı siparişi listeden seçin.")
            if linked_order and customer and (linked_order.order_type != invoice_type or linked_order.customer_id != customer.id):
                errors.append("Bağlı siparişin türü ve carisi faturayla aynı olmalıdır.")
            if not lines:
                errors.append("En az bir stok kalemi girin.")
            if errors:
                for error in errors:
                    flash(error, "error")
            else:
                create_database_backup(app, "before_invoice_create")
                invoice = Invoice(
                    invoice_no=invoice_no, invoice_type=invoice_type, customer=customer, order=linked_order,
                    invoice_date=parse_date(request.form.get("invoice_date")) or date.today(),
                    due_date=parse_date(request.form.get("due_date")), notes=request.form.get("notes", "").strip() or None,
                )
                db.session.add(invoice)
                db.session.flush()
                movement_type = "Stok Girişi" if invoice_type == "Satın Alma" else "Stok Çıkışı"
                for line in lines:
                    item = InvoiceItem(invoice=invoice, product=line["product"], product_name=line["product"].name,
                        quantity=line["quantity"], unit=line["product"].unit, unit_price=line["unit_price"], discount_rate=line["discount_rate"],
                        vat_rate=line["vat_rate"], vat_included=line["vat_included"])
                    db.session.add(item)
                    db.session.flush()
                    movement = StockMovement(product=line["product"], movement_type=movement_type, quantity=line["quantity"],
                        movement_date=invoice.invoice_date, note=f"{invoice_type} faturası · {invoice.invoice_no}")
                    db.session.add(movement)
                    db.session.flush()
                    item.stock_movement_id = movement.id
                db.session.commit()
                flash(f"{invoice.invoice_no} faturası kaydedildi; cari ve stok hareketleri işlendi.", "success")
                if existing_order_invoice_count:
                    flash(
                        f"Uyarı: {linked_order.order_no} siparişine bağlı {existing_order_invoice_count} fatura daha var; yeni fatura da kaydedildi.",
                        "warning",
                    )
                return redirect(url_for("invoices", saved=1))
        invoice_type = request.args.get("type", "").strip()
        selected_customer_id = request.args.get("customer_id", type=int)
        query = request.args.get("q", "").strip()
        start_date = parse_date(request.args.get("start_date"))
        end_date = parse_date(request.args.get("end_date"))
        records = Invoice.query
        if invoice_type in {"Satış", "Satın Alma"}:
            records = records.filter_by(invoice_type=invoice_type)
        else:
            invoice_type = ""
        if selected_customer_id:
            records = records.filter_by(customer_id=selected_customer_id)
        if start_date:
            records = records.filter(Invoice.invoice_date >= start_date)
        if end_date:
            records = records.filter(Invoice.invoice_date <= end_date)
        listed_invoices = records.order_by(Invoice.invoice_date.desc(), Invoice.id.desc()).all()
        if query:
            normalized_query = normalize_search_text(query)
            listed_invoices = [invoice for invoice in listed_invoices if normalized_query in normalize_search_text(" ".join([invoice.invoice_no, invoice.customer.name, invoice.notes or ""]))]
        prefill_discount_rates = [str(item.discount_rate or 0) for item in prefill_order.items] if prefill_order else []
        return render_template("invoices.html", invoices=listed_invoices, customers=customers, products=products, orders=orders, prefill_order=prefill_order, prefill_discount_rates=prefill_discount_rates, entry_mode=request.args.get("entry") == "1", saved=request.args.get("saved") == "1",
            selected_type=invoice_type, selected_customer_id=selected_customer_id, query=query,
            start_date=request.args.get("start_date", ""), end_date=request.args.get("end_date", ""), today=date.today().isoformat())

    @app.get("/siparisler/<int:order_id>/faturaya-aktar")
    def order_to_invoice(order_id):
        order = db.get_or_404(Order, order_id)
        return redirect(url_for("invoices", entry=1, order_id=order.id))

    @app.get("/faturalar/<int:invoice_id>")
    def invoice_detail(invoice_id):
        invoice = db.get_or_404(Invoice, invoice_id)
        return render_template("invoice_detail.html", invoice=invoice)

    @app.route("/faturalar/<int:invoice_id>/duzenle", methods=["GET", "POST"])
    def edit_invoice(invoice_id):
        invoice = db.get_or_404(Invoice, invoice_id)
        products = Product.query.filter_by(active=True).order_by(Product.name).all()
        customers = Customer.query.order_by(Customer.name).all()
        orders = Order.query.order_by(Order.order_date.desc(), Order.id.desc()).limit(500).all()
        if request.method == "POST":
            invoice_no = request.form.get("invoice_no", "").strip().upper()
            invoice_type = request.form.get("invoice_type", "")
            customer_ref = request.form.get("customer_ref", "").strip()
            customer_by_ref = {}
            for listed_customer in customers:
                customer_by_ref[normalize_search_text(listed_customer.name)] = listed_customer
                if listed_customer.code:
                    customer_by_ref[normalize_search_text(listed_customer.code)] = listed_customer
                    customer_by_ref[normalize_search_text(f"{listed_customer.name} · {listed_customer.code}")] = listed_customer
            customer = customer_by_ref.get(normalize_search_text(customer_ref)) if customer_ref else None
            order_ref = request.form.get("order_ref", "").strip()
            order_by_ref = {}
            for order in orders:
                order_by_ref[normalize_search_text(f"{order.order_no} · {order.order_type} · {order.customer.name}")] = order
                order_by_ref[normalize_search_text(order.order_no)] = order
            linked_order = order_by_ref.get(normalize_search_text(order_ref)) if order_ref else None
            product_by_ref = {}
            for product in products:
                product_by_ref[normalize_search_text(f"{product.code or 'Kodsuz'} · {product.name}")] = product
                product_by_ref[normalize_search_text(product.name)] = product
                if product.code:
                    product_by_ref[normalize_search_text(product.code)] = product
            refs = request.form.getlist("product_ref[]")
            quantities = request.form.getlist("quantity[]")
            prices = request.form.getlist("unit_price[]")
            discount_rates = request.form.getlist("discount_rate[]")
            vat_rates = request.form.getlist("vat_rate[]")
            vat_modes = request.form.getlist("vat_included[]")
            lines = []
            errors = []
            for index, product_ref in enumerate(refs):
                product_ref = product_ref.strip()
                if not product_ref and not (quantities[index].strip() if index < len(quantities) else ""):
                    continue
                product = product_by_ref.get(normalize_search_text(product_ref))
                quantity = int(parse_money(quantities[index])) if index < len(quantities) and quantities[index].strip() else 0
                unit_price = parse_money(prices[index]) if index < len(prices) else Decimal("0")
                discount_rate = parse_money(discount_rates[index]) if index < len(discount_rates) else Decimal("0")
                vat_rate = parse_money(vat_rates[index]) if index < len(vat_rates) else Decimal("0")
                if not product:
                    errors.append(f"{index + 1}. satırdaki ürün stok kartından seçilmelidir.")
                elif quantity < 1:
                    errors.append(f"{product.name} için adet en az 1 olmalıdır.")
                elif unit_price < 0 or discount_rate < 0 or discount_rate > 100 or vat_rate < 0 or vat_rate > 100:
                    errors.append(f"{product.name} satırındaki fiyat, iskonto veya KDV geçersiz.")
                else:
                    lines.append({"product": product, "quantity": quantity, "unit_price": unit_price, "discount_rate": discount_rate, "vat_rate": vat_rate, "vat_included": index < len(vat_modes) and vat_modes[index] == "1"})
            duplicate = Invoice.query.filter(Invoice.invoice_no == invoice_no, Invoice.id != invoice.id).first() if invoice_no else None
            if not invoice_no:
                errors.append("Fatura numarası zorunludur.")
            elif duplicate:
                errors.append("Bu fatura numarası başka bir kayıtta kullanılıyor.")
            if invoice_type not in {"Satış", "Satın Alma"}:
                errors.append("Fatura türünü seçin.")
            if not customer:
                errors.append("Geçerli bir cari adı veya kodu yazın.")
            if order_ref and not linked_order:
                errors.append("Bağlı siparişi listeden seçin.")
            if linked_order and customer and (linked_order.order_type != invoice_type or linked_order.customer_id != customer.id):
                errors.append("Bağlı siparişin türü ve carisi faturayla aynı olmalıdır.")
            if not lines:
                errors.append("En az bir stok kalemi girin.")
            if errors:
                for error in errors:
                    flash(error, "error")
            else:
                create_database_backup(app, "before_invoice_edit")
                previous_movement_ids = [item.stock_movement_id for item in invoice.items if item.stock_movement_id]
                for movement_id in previous_movement_ids:
                    movement = db.session.get(StockMovement, movement_id)
                    if movement:
                        db.session.delete(movement)
                invoice.items.clear()
                db.session.flush()
                invoice.invoice_no = invoice_no
                invoice.invoice_type = invoice_type
                invoice.customer = customer
                invoice.order = linked_order
                invoice.invoice_date = parse_date(request.form.get("invoice_date")) or date.today()
                invoice.due_date = parse_date(request.form.get("due_date"))
                invoice.notes = request.form.get("notes", "").strip() or None
                movement_type = "Stok Girişi" if invoice_type == "Satın Alma" else "Stok Çıkışı"
                for line in lines:
                    item = InvoiceItem(invoice=invoice, product=line["product"], product_name=line["product"].name,
                        quantity=line["quantity"], unit=line["product"].unit, unit_price=line["unit_price"], discount_rate=line["discount_rate"],
                        vat_rate=line["vat_rate"], vat_included=line["vat_included"])
                    db.session.add(item)
                    db.session.flush()
                    movement = StockMovement(product=line["product"], movement_type=movement_type, quantity=line["quantity"],
                        movement_date=invoice.invoice_date, note=f"{invoice_type} faturası · {invoice.invoice_no}")
                    db.session.add(movement)
                    db.session.flush()
                    item.stock_movement_id = movement.id
                db.session.commit()
                flash(f"{invoice.invoice_no} faturası güncellendi; stok ve cari etkileri yeniden hesaplandı.", "success")
                return redirect(url_for("invoice_detail", invoice_id=invoice.id))
        return render_template("invoice_edit.html", invoice=invoice, customers=customers, products=products, orders=orders)

    @app.post("/faturalar/<int:invoice_id>/sil")
    def delete_invoice(invoice_id):
        invoice = db.get_or_404(Invoice, invoice_id)
        invoice_no = invoice.invoice_no
        customer_id = invoice.customer_id
        movement_ids = [item.stock_movement_id for item in invoice.items if item.stock_movement_id]
        create_database_backup(app, "before_invoice_delete")
        for movement_id in movement_ids:
            movement = db.session.get(StockMovement, movement_id)
            if movement:
                db.session.delete(movement)
        db.session.delete(invoice)
        db.session.commit()
        flash(f"{invoice_no} faturası, bağlı stok ve cari etkileriyle birlikte silindi.", "success")
        return redirect(url_for("customer_account", customer_id=customer_id))

    @app.get("/telegram-belgeler")
    def telegram_documents():
        settings = load_telegram_settings(app)
        if not settings.get("pairing_code"):
            settings["pairing_code"] = secrets.token_hex(3).upper()
            save_telegram_settings(app, settings)
        documents = TelegramIncomingDocument.query.order_by(TelegramIncomingDocument.received_at.desc()).all()
        orders = Order.query.order_by(Order.order_date.desc(), Order.id.desc()).limit(500).all()
        return render_template(
            "telegram_documents.html", documents=documents, orders=orders,
            pairing_code=settings["pairing_code"], paired=bool(settings.get("allowed_chat_id")),
            configured=bool(telegram_bot_token()), order_document_types=ORDER_DOCUMENT_TYPES,
        )

    @app.post("/telegram-belgeler/kontrol")
    def refresh_telegram_documents():
        try:
            create_database_backup(app, "before_telegram_document_sync")
            added, paired = sync_telegram_documents(app)
            db.session.commit()
            if paired:
                flash("Telegram hesabı bu bilgisayar için eşleştirildi.", "success")
            flash(f"Telegram kontrol edildi: {added} yeni belge alındı.", "success")
        except (ValueError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as error:
            db.session.rollback()
            flash(f"Telegram belgeleri alınamadı: {error}", "error")
        return redirect(url_for("telegram_documents"))

    @app.get("/telegram-belgeler/<int:incoming_id>/indir")
    def download_telegram_document(incoming_id):
        incoming = db.get_or_404(TelegramIncomingDocument, incoming_id)
        try:
            path = telegram_incoming_path(app, incoming)
        except ValueError:
            abort(404)
        if not os.path.isfile(path):
            abort(404)
        return send_file(path, as_attachment=True, download_name=incoming.original_name, mimetype=incoming.mime_type)

    @app.get("/telegram-belgeler/<int:incoming_id>/ac")
    def view_telegram_document(incoming_id):
        incoming = db.get_or_404(TelegramIncomingDocument, incoming_id)
        try:
            path = telegram_incoming_path(app, incoming)
        except ValueError:
            abort(404)
        if not os.path.isfile(path):
            abort(404)
        return send_file(path, as_attachment=False, download_name=incoming.original_name, mimetype=incoming.mime_type)

    @app.post("/telegram-belgeler/<int:incoming_id>/bagla")
    def link_telegram_document(incoming_id):
        incoming = db.get_or_404(TelegramIncomingDocument, incoming_id)
        order_no = request.form.get("order_no", "").strip().upper()
        order = Order.query.filter(func.upper(Order.order_no) == order_no).first()
        if not order:
            flash("Bağlanacak sipariş numarası bulunamadı.", "error")
            return redirect(url_for("telegram_documents"))
        try:
            source_path = telegram_incoming_path(app, incoming)
            if not os.path.isfile(source_path):
                raise ValueError("Telegram belgesi yerel klasörde bulunamadı.")
            create_database_backup(app, "before_telegram_document_link")
            extension = incoming.original_name.rsplit(".", 1)[-1].lower() if "." in incoming.original_name else ""
            stored_name = f"{secrets.token_urlsafe(18)}.{extension}"
            destination = os.path.join(order_documents_directory(app, order.id), stored_name)
            shutil.copy2(source_path, destination)
            db.session.add(OrderDocument(
                order_id=order.id,
                document_type=request.form.get("document_type") if request.form.get("document_type") in ORDER_DOCUMENT_TYPES else order_document_type_for(order),
                original_name=incoming.original_name, stored_name=stored_name, mime_type=incoming.mime_type,
                size_bytes=incoming.size_bytes, source="Telegram",
            ))
            order.history.append(OrderHistory(status=order.status, note="Telegram'dan gelen belge eklendi"))
            db.session.delete(incoming)
            db.session.commit()
            os.remove(source_path)
            flash(f"{incoming.original_name} belgesi {order.order_no} siparişine eklendi.", "success")
        except (OSError, ValueError) as error:
            db.session.rollback()
            flash(str(error), "error")
        return redirect(url_for("telegram_documents"))

    @app.post("/telegram-belgeler/<int:incoming_id>/sil")
    def delete_telegram_document(incoming_id):
        incoming = db.get_or_404(TelegramIncomingDocument, incoming_id)
        try:
            path = telegram_incoming_path(app, incoming)
            create_database_backup(app, "before_telegram_document_delete")
            db.session.delete(incoming)
            db.session.commit()
            if os.path.isfile(path):
                os.remove(path)
            flash("Telegram belgesi kuyruktan silindi.", "success")
        except (OSError, ValueError) as error:
            db.session.rollback()
            flash(f"Belge silinemedi: {error}", "error")
        return redirect(url_for("telegram_documents"))

    @app.get("/e-arsiv-belgeleri")
    def earchive_documents():
        documents = EArchiveIncomingDocument.query.order_by(EArchiveIncomingDocument.received_at.desc()).all()
        orders = Order.query.order_by(Order.order_date.desc(), Order.id.desc()).limit(500).all()
        return render_template("earchive_documents.html", documents=documents, orders=orders, order_document_types=ORDER_DOCUMENT_TYPES)

    @app.post("/e-arsiv-belgeleri/kontrol")
    def refresh_earchive_documents():
        try:
            create_database_backup(app, "before_earchive_download_sync")
            added = sync_earchive_downloads(app)
            db.session.commit()
            flash(f"İndirilenler kontrol edildi: {added} yeni E-Arşiv faturası alındı.", "success")
        except (OSError, ValueError) as error:
            db.session.rollback()
            flash(f"E-Arşiv faturaları alınamadı: {error}", "error")
        return redirect(url_for("earchive_documents"))

    @app.get("/e-arsiv-belgeleri/<int:incoming_id>/ac")
    def view_earchive_document(incoming_id):
        incoming = db.get_or_404(EArchiveIncomingDocument, incoming_id)
        try:
            path = earchive_incoming_path(app, incoming)
        except ValueError:
            abort(404)
        if not os.path.isfile(path):
            abort(404)
        return send_file(path, as_attachment=False, download_name=incoming.original_name, mimetype="application/pdf")

    @app.get("/e-arsiv-belgeleri/<int:incoming_id>/indir")
    def download_earchive_document(incoming_id):
        incoming = db.get_or_404(EArchiveIncomingDocument, incoming_id)
        try:
            path = earchive_incoming_path(app, incoming)
        except ValueError:
            abort(404)
        if not os.path.isfile(path):
            abort(404)
        return send_file(path, as_attachment=True, download_name=incoming.original_name, mimetype="application/pdf")

    @app.post("/e-arsiv-belgeleri/<int:incoming_id>/bagla")
    def link_earchive_document(incoming_id):
        incoming = db.get_or_404(EArchiveIncomingDocument, incoming_id)
        order_no = request.form.get("order_no", "").strip().upper()
        order = Order.query.filter(func.upper(Order.order_no) == order_no).first()
        if not order:
            flash("Bağlanacak sipariş numarası bulunamadı.", "error")
            return redirect(url_for("earchive_documents"))
        try:
            source_path = earchive_incoming_path(app, incoming)
            if not os.path.isfile(source_path):
                raise ValueError("E-Arşiv belgesi yerel klasörde bulunamadı.")
            create_database_backup(app, "before_earchive_document_link")
            stored_name = f"{secrets.token_urlsafe(18)}.pdf"
            destination = os.path.join(order_documents_directory(app, order.id), stored_name)
            shutil.copy2(source_path, destination)
            db.session.add(OrderDocument(
                order_id=order.id,
                document_type=request.form.get("document_type") if request.form.get("document_type") in ORDER_DOCUMENT_TYPES else "Giden Fatura",
                original_name=incoming.original_name, stored_name=stored_name, mime_type="application/pdf",
                size_bytes=incoming.size_bytes, source="E-Arşiv Portal",
            ))
            order.history.append(OrderHistory(status=order.status, note="E-Arşiv Portal faturası eklendi"))
            db.session.delete(incoming)
            db.session.commit()
            os.remove(source_path)
            flash(f"{incoming.original_name} belgesi {order.order_no} siparişine eklendi.", "success")
        except (OSError, ValueError) as error:
            db.session.rollback()
            flash(str(error), "error")
        return redirect(url_for("earchive_documents"))

    @app.post("/e-arsiv-belgeleri/<int:incoming_id>/sil")
    def delete_earchive_document(incoming_id):
        incoming = db.get_or_404(EArchiveIncomingDocument, incoming_id)
        try:
            path = earchive_incoming_path(app, incoming)
            create_database_backup(app, "before_earchive_document_delete")
            db.session.delete(incoming)
            db.session.commit()
            if os.path.isfile(path):
                os.remove(path)
            flash("E-Arşiv belgesi kuyruktan silindi.", "success")
        except (OSError, ValueError) as error:
            db.session.rollback()
            flash(f"Belge silinemedi: {error}", "error")
        return redirect(url_for("earchive_documents"))

    @app.post("/yedekler/olustur")
    def create_manual_backup():
        backup_path = create_database_backup(app, "manual")
        archive_dir = os.path.expanduser("~/Documents/Business OS Yedekleri")
        create_database_backup(app, "manual", target_dir=archive_dir, keep=90)
        flash("Yeni yedek oluşturuldu ve Belgeler klasörüne de kopyalandı.", "success")
        return redirect(url_for("backups"))

    @app.get("/yedekler/guncel-indir")
    def download_current_backup():
        backup_path = create_database_backup(app, "download")
        return send_file(backup_path, as_attachment=True, download_name=f"Business-OS-Yedek-{date.today().isoformat()}.db")

    @app.get("/yedekler/<path:filename>/indir")
    def download_backup(filename):
        safe_name = os.path.basename(filename)
        backup_dir = os.path.realpath(os.path.join(app.instance_path, "backups"))
        backup_path = os.path.realpath(os.path.join(backup_dir, safe_name))
        if not backup_path.startswith(backup_dir + os.sep) or not os.path.isfile(backup_path):
            return "Yedek bulunamadı", 404
        return send_file(backup_path, as_attachment=True, download_name=safe_name)

    @app.get("/kar-zarar")
    def profitability():
        today = date.today()
        period = request.args.get("period", "month")
        if period not in {"day", "month", "year"}:
            period = "month"
        selected = parse_date(request.args.get("date")) or today
        if period == "day":
            start = end = selected
            title = selected.strftime("%d.%m.%Y")
        elif period == "year":
            start, end = date(selected.year, 1, 1), date(selected.year, 12, 31)
            title = str(selected.year)
        else:
            start = date(selected.year, selected.month, 1)
            end = date(selected.year, selected.month, calendar.monthrange(selected.year, selected.month)[1])
            title = start.strftime("%m.%Y")

        realized_sales = Order.query.filter(Order.order_type == "Satış", Order.status.in_(FINANCIAL_ORDER_STATUSES)).all()
        sales = [order for order in realized_sales if start <= order_realization_date(order) <= end]
        revenue = sum((order.net_amount for order in sales), Decimal("0"))
        cost = sum((sum((effective_sales_item_cost(item, order_realization_date(order)) * item.quantity for item in order.items), Decimal("0")) for order in sales), Decimal("0"))
        gross_profit = revenue - cost
        expenses = Expense.query.filter(Expense.expense_date >= start, Expense.expense_date <= end).all()
        expenses_total = sum((item.amount for item in expenses), Decimal("0"))
        net_profit = gross_profit - expenses_total
        markup = (gross_profit / cost * 100) if cost else None
        missing_cost_items = sum(1 for order in sales for item in order.items if not effective_sales_item_cost(item, order_realization_date(order)))

        buckets = {}
        if period == "year":
            for month in range(1, 13):
                buckets[(selected.year, month)] = {"label": f"{month:02d}.{selected.year}", "revenue": Decimal("0"), "cost": Decimal("0"), "expense": Decimal("0")}
        elif period == "month":
            for day_no in range(1, end.day + 1):
                day_value = date(selected.year, selected.month, day_no)
                buckets[day_value] = {"label": day_value.strftime("%d.%m"), "revenue": Decimal("0"), "cost": Decimal("0"), "expense": Decimal("0")}
        else:
            buckets[selected] = {"label": selected.strftime("%d.%m.%Y"), "revenue": Decimal("0"), "cost": Decimal("0"), "expense": Decimal("0")}
        for order in sales:
            realized = order_realization_date(order)
            key = (realized.year, realized.month) if period == "year" else realized
            buckets[key]["revenue"] += order.net_amount
            buckets[key]["cost"] += sum((effective_sales_item_cost(item, realized) * item.quantity for item in order.items), Decimal("0"))
        for expense in expenses:
            key = (expense.expense_date.year, expense.expense_date.month) if period == "year" else expense.expense_date
            buckets[key]["expense"] += expense.amount
        timeline = []
        for bucket in buckets.values():
            bucket["gross"] = bucket["revenue"] - bucket["cost"]
            bucket["net"] = bucket["gross"] - bucket["expense"]
            timeline.append(bucket)
        chart_max = max((max(abs(item["revenue"]), abs(item["cost"]), abs(item["net"])) for item in timeline), default=Decimal("1")) or Decimal("1")
        sales.sort(key=order_realization_date, reverse=True)
        return render_template("profitability.html", period=period, selected=selected, start=start, end=end, title=title,
            revenue=revenue, cost=cost, gross_profit=gross_profit, expenses_total=expenses_total, net_profit=net_profit,
            markup=markup, missing_cost_items=missing_cost_items, sales=sales, timeline=timeline, chart_max=chart_max,
            order_realization_date=order_realization_date, effective_sales_item_cost=effective_sales_item_cost)

    @app.get("/mali-tablolar")
    def financial_statements():
        today = date.today()
        selected = parse_date(request.args.get("date")) or today
        period = request.args.get("period", "month")
        if period not in {"month", "year"}:
            period = "month"
        if period == "year":
            period_start = date(selected.year, 1, 1)
            period_title = str(selected.year)
        else:
            period_start = date(selected.year, selected.month, 1)
            period_title = period_start.strftime("%m.%Y")
        period_end = selected

        realized_orders = Order.query.filter(Order.status.in_(FINANCIAL_ORDER_STATUSES)).all()
        period_sales = [order for order in realized_orders if order.order_type == "Satış" and period_start <= order_realization_date(order) <= period_end]
        sales_revenue = sum((order.net_amount for order in period_sales), Decimal("0"))
        sales_cost = sum((sum((effective_sales_item_cost(item, order_realization_date(order)) * item.quantity for item in order.items), Decimal("0")) for order in period_sales), Decimal("0"))
        gross_profit = sales_revenue - sales_cost
        period_expenses = Expense.query.filter(Expense.expense_date >= period_start, Expense.expense_date <= period_end).all()
        expense_by_category = {}
        for expense in period_expenses:
            expense_by_category.setdefault(expense.category, Decimal("0"))
            expense_by_category[expense.category] += expense.amount
        operating_expenses = sum(expense_by_category.values(), Decimal("0"))
        net_profit = gross_profit - operating_expenses

        balances = {customer.id: Decimal("0") for customer in Customer.query.all()}
        for order in realized_orders:
            if order_realization_date(order) <= selected:
                balances[order.customer_id] += order.total_amount if order.order_type == "Satış" else -order.total_amount
        transactions = AccountTransaction.query.filter(AccountTransaction.transaction_date <= selected).all()
        for transaction in transactions:
            balances[transaction.customer_id] += (transaction.debit or Decimal("0")) - (transaction.credit or Decimal("0"))
        receivables = sum((balance for balance in balances.values() if balance > 0), Decimal("0"))
        payables = sum((-balance for balance in balances.values() if balance < 0), Decimal("0"))

        cash_collections = sum((item.credit or Decimal("0") for item in transactions if item.transaction_type == "Tahsilat" and item.payment_method in LIQUID_PAYMENT_METHODS), Decimal("0"))
        cash_payments = sum((item.debit or Decimal("0") for item in transactions if item.transaction_type == "Ödeme" and item.payment_method in LIQUID_PAYMENT_METHODS), Decimal("0"))
        cash_expenses = sum((item.amount for item in Expense.query.filter(Expense.expense_date <= selected, Expense.payment_method == "Nakit").all()), Decimal("0"))
        cash_movements = CashMovement.query.filter(CashMovement.movement_date <= selected).all()
        manual_cash = sum((item.amount if item.movement_type == "Giriş" else -item.amount for item in cash_movements), Decimal("0"))
        cash_balance = cash_collections - cash_payments - cash_expenses + manual_cash
        pending_checks = [item for item in transactions if item.payment_method == "Çek" and item.check_status == "Bekliyor"]
        incoming_checks = sum((item.credit or Decimal("0") for item in pending_checks if item.transaction_type == "Tahsilat"), Decimal("0"))
        outgoing_checks = sum((item.debit or Decimal("0") for item in pending_checks if item.transaction_type == "Ödeme"), Decimal("0"))
        total_assets = cash_balance + receivables + incoming_checks
        total_liabilities = payables + outgoing_checks
        calculated_equity = total_assets - total_liabilities
        return render_template("financial_statements.html", selected=selected, period=period, period_start=period_start,
            period_end=period_end, period_title=period_title, sales_revenue=sales_revenue, sales_cost=sales_cost,
            gross_profit=gross_profit, expense_by_category=sorted(expense_by_category.items()), operating_expenses=operating_expenses,
            net_profit=net_profit, cash_balance=cash_balance, receivables=receivables, incoming_checks=incoming_checks,
            payables=payables, outgoing_checks=outgoing_checks, total_assets=total_assets,
            total_liabilities=total_liabilities, calculated_equity=calculated_equity)

    @app.route("/tahsilat-girisi", methods=["GET", "POST"])
    def quick_collection():
        customers = Customer.query.order_by(Customer.name).all()
        if request.method == "POST":
            customer_id = request.form.get("customer_id", type=int)
            customer = db.session.get(Customer, customer_id) if customer_id else None
            amount = parse_money(request.form.get("amount"))
            payment_method = request.form.get("payment_method", "")
            check_due_date = parse_date(request.form.get("check_due_date"))
            card_details, card_error = card_payment_details(payment_method, "Tahsilat")
            direct_supplier, supplier_error = direct_supplier_for_card_collection() if payment_method == "Kredi Kartı" else (None, None)
            if not customer:
                flash("Lütfen listeden geçerli bir cari seçin.", "error")
            elif amount <= 0:
                flash("Tahsilat tutarı sıfırdan büyük olmalıdır.", "error")
            elif payment_method not in COLLECTION_PAYMENT_METHODS:
                flash("Tahsilat şekli olarak Nakit, Çek, Banka veya Kredi Kartı seçin.", "error")
            elif payment_method == "Çek" and (not request.form.get("check_no", "").strip() or not check_due_date):
                flash("Çek numarası ve vade tarihi zorunludur.", "error")
            elif card_error:
                flash(card_error, "error")
            elif supplier_error:
                flash(supplier_error, "error")
            elif direct_supplier and direct_supplier.id == customer.id:
                flash("Müşteri ile doğrudan ödeme yapılan tedarikçi aynı cari olamaz.", "error")
            else:
                create_database_backup(app, "before_quick_collection")
                transaction_date = parse_date(request.form.get("transaction_date")) or date.today()
                reference_no = request.form.get("reference_no", "").strip()
                description = request.form.get("description", "").strip()
                if direct_supplier:
                    add_direct_card_collection_pair(customer, direct_supplier, amount, transaction_date, reference_no, description, card_details)
                else:
                    transaction = AccountTransaction(customer=customer, transaction_date=transaction_date,
                        transaction_type="Tahsilat", reference_no=reference_no, description=description or "Tahsilat",
                        debit=0, credit=amount, payment_method=payment_method,
                        check_no=request.form.get("check_no", "").strip() if payment_method == "Çek" else None,
                        check_bank=request.form.get("check_bank", "").strip() if payment_method == "Çek" else None,
                        check_due_date=check_due_date if payment_method == "Çek" else None,
                        check_status="Bekliyor" if payment_method == "Çek" else None, **card_details)
                    db.session.add(transaction)
                db.session.commit()
                if direct_supplier:
                    flash(f"{customer.name} tahsilatı ve {direct_supplier.name} ödemesi birlikte kaydedildi.", "success")
                else:
                    flash(f"{customer.name} için ₺{amount:,.2f} tahsilat kaydedildi.", "success")
                return redirect(url_for("dashboard"))
        return render_template("quick_collection.html", customers=customers, today=date.today().isoformat(), is_payment=False)

    @app.route("/odeme-girisi", methods=["GET", "POST"])
    def quick_payment():
        customers = Customer.query.order_by(Customer.name).all()
        if request.method == "POST":
            customer_id = request.form.get("customer_id", type=int)
            customer = db.session.get(Customer, customer_id) if customer_id else None
            amount = parse_money(request.form.get("amount"))
            payment_method = request.form.get("payment_method", "")
            check_due_date = parse_date(request.form.get("check_due_date"))
            card_details, card_error = card_payment_details(payment_method, "Ödeme")
            if not customer:
                flash("Lütfen listeden geçerli bir cari seçin.", "error")
            elif amount <= 0:
                flash("Ödeme tutarı sıfırdan büyük olmalıdır.", "error")
            elif payment_method not in ACCOUNT_PAYMENT_METHODS:
                flash("Ödeme şekli olarak Nakit, Çek, Banka veya Kredi Kartı seçin.", "error")
            elif payment_method == "Çek" and (not request.form.get("check_no", "").strip() or not check_due_date):
                flash("Çek numarası ve vade tarihi zorunludur.", "error")
            elif card_error:
                flash(card_error, "error")
            else:
                create_database_backup(app, "before_quick_payment")
                transaction = AccountTransaction(
                    customer=customer,
                    transaction_date=parse_date(request.form.get("transaction_date")) or date.today(),
                    transaction_type="Ödeme",
                    reference_no=request.form.get("reference_no", "").strip(),
                    description=request.form.get("description", "").strip() or "Ödeme",
                    debit=amount,
                    credit=0,
                    payment_method=payment_method,
                    check_no=request.form.get("check_no", "").strip() if payment_method == "Çek" else None,
                    check_bank=request.form.get("check_bank", "").strip() if payment_method == "Çek" else None,
                    check_due_date=check_due_date if payment_method == "Çek" else None,
                    check_status="Bekliyor" if payment_method == "Çek" else None,
                    **card_details,
                )
                db.session.add(transaction)
                db.session.commit()
                flash(f"{customer.name} için ₺{amount:,.2f} ödeme kaydedildi.", "success")
                return redirect(url_for("dashboard"))
        return render_template("quick_collection.html", customers=customers, today=date.today().isoformat(), is_payment=True)

    @app.route("/musteriler", methods=["GET", "POST"])
    def customers():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            if not name:
                flash("Müşteri adı zorunludur.", "error")
            else:
                db.session.add(Customer(name=name, code=request.form.get("code", "").strip() or None, contact_name=request.form.get("contact_name"), phone=request.form.get("phone"), mobile=request.form.get("mobile"), email=request.form.get("email"), city=request.form.get("city"), address=request.form.get("address"), notes=request.form.get("notes")))
                db.session.commit()
                flash("Müşteri kartı oluşturuldu.", "success")
                return redirect(url_for("customers"))
        query = request.args.get("q", "").strip()
        balance_filter = request.args.get("balance", "").strip()
        view = "balances" if request.args.get("view") == "balances" or balance_filter in {"debit", "credit"} else "cards"
        balance_sort = request.args.get("sort", "amount_desc").strip()
        if view == "balances":
            customer_records, balances, balance_filter, balance_sort = customer_balance_view(query, balance_filter, balance_sort)
            total_count = len(customer_records)
            page = total_pages = 1
        else:
            records = Customer.query
            if query:
                if db.engine.dialect.name == "sqlite":
                    pattern = f"%{normalize_search_text(query)}%"
                    records = records.filter(db.or_(
                        db.func.normalize_tr(Customer.name).like(pattern), db.func.normalize_tr(Customer.code).like(pattern),
                        db.func.normalize_tr(Customer.contact_name).like(pattern), db.func.normalize_tr(Customer.phone).like(pattern),
                        db.func.normalize_tr(Customer.mobile).like(pattern), db.func.normalize_tr(Customer.email).like(pattern),
                        db.func.normalize_tr(Customer.city).like(pattern),
                    ))
                else:
                    pattern = f"%{query}%"
                    records = records.filter(db.or_(Customer.name.ilike(pattern), Customer.code.ilike(pattern), Customer.contact_name.ilike(pattern), Customer.phone.ilike(pattern), Customer.mobile.ilike(pattern), Customer.email.ilike(pattern), Customer.city.ilike(pattern)))
            page_size = 50
            total_count = records.count()
            total_pages = max(1, math.ceil(total_count / page_size))
            page = min(max(request.args.get("page", 1, type=int) or 1, 1), total_pages)
            customer_records = records.order_by(Customer.name).offset((page - 1) * page_size).limit(page_size).all()
            balances = calculate_customer_balances([customer.id for customer in customer_records])
            balance_filter = ""
            balance_sort = "amount_desc"
        visible_debit_total = sum((balances[customer.id] for customer in customer_records if balances[customer.id] > 0), Decimal("0"))
        visible_credit_total = sum((-balances[customer.id] for customer in customer_records if balances[customer.id] < 0), Decimal("0"))
        return render_template("customers.html", customers=customer_records, customer_balances=balances, query=query, balance_filter=balance_filter, view=view, balance_sort=balance_sort, visible_debit_total=visible_debit_total, visible_credit_total=visible_credit_total, total_count=total_count, page=page, total_pages=total_pages)

    register_report(app, Customer, Order, AccountTransaction, normalize_search_text)

    @app.get("/musteriler/bakiyeler/excel")
    def export_customer_balances_xlsx():
        customers, balances, _balance_filter, _balance_sort = customer_balance_view(
            request.args.get("q", "").strip(), request.args.get("balance", "").strip(), request.args.get("sort", "amount_desc").strip())
        return send_file(build_customer_balances_xlsx(customers, balances),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True,
            download_name=f"Cari-Bakiyeler-{date.today().isoformat()}.xlsx")

    @app.get("/musteriler/bakiyeler/pdf")
    def export_customer_balances_pdf():
        customers, balances, _balance_filter, _balance_sort = customer_balance_view(
            request.args.get("q", "").strip(), request.args.get("balance", "").strip(), request.args.get("sort", "amount_desc").strip())
        return send_file(build_customer_balances_pdf(customers, balances), mimetype="application/pdf", as_attachment=True,
            download_name=f"Cari-Bakiyeler-{date.today().isoformat()}.pdf")

    @app.route("/musteriler/aktar", methods=["GET", "POST"])
    def import_customers():
        if request.method == "POST":
            uploaded = request.files.get("file")
            if not uploaded or not uploaded.filename:
                flash("Lütfen bir Excel dosyası seçin.", "error")
                return redirect(url_for("import_customers"))
            if not uploaded.filename.lower().endswith(".xlsx"):
                flash("Yalnızca .xlsx uzantılı Excel dosyaları desteklenir.", "error")
                return redirect(url_for("import_customers"))
            try:
                create_database_backup(app, "before_customer_import")
                from openpyxl import load_workbook
                workbook = load_workbook(uploaded, read_only=True, data_only=True)
                sheet = workbook.active
                rows = sheet.iter_rows(values_only=True)
                raw_headers = next(rows, None)
                if not raw_headers:
                    raise ValueError("Dosya boş.")
                headers = {str(value or "").strip().casefold(): index for index, value in enumerate(raw_headers)}
                aliases = {
                    "code": ["cari kodu", "cari kod", "kod"],
                    "name": ["ünvan", "unvan", "müşteri adı", "firma adı"],
                    "phone": ["telefon 1", "telefon", "tel"],
                    "mobile": ["cep tel", "cep telefonu", "mobil"],
                    "email": ["e-posta", "e-posta adresi", "email"],
                    "city": ["şehir", "sehir", "il"],
                }
                columns = {key: next((headers[a] for a in names if a in headers), None) for key, names in aliases.items()}
                if columns["name"] is None:
                    raise ValueError("'Ünvan' sütunu bulunamadı.")
                added = updated = unchanged = invalid = 0
                existing_by_code = {customer.code: customer for customer in Customer.query.filter(Customer.code.isnot(None)).all()}
                for row in rows:
                    def cell(field):
                        index = columns[field]
                        return str(row[index]).strip() if index is not None and index < len(row) and row[index] is not None else ""
                    name, code = cell("name"), cell("code")
                    if not name:
                        invalid += 1
                        continue
                    values = {"name": name, "phone": cell("phone"), "mobile": cell("mobile"), "email": cell("email"), "city": cell("city")}
                    if code and code in existing_by_code:
                        customer = existing_by_code[code]
                        changed = any((getattr(customer, field) or "") != value for field, value in values.items())
                        if changed:
                            for field, value in values.items():
                                setattr(customer, field, value)
                            updated += 1
                        else:
                            unchanged += 1
                        continue
                    customer = Customer(code=code or None, notes="DİA Excel aktarımı", **values)
                    db.session.add(customer)
                    if code:
                        existing_by_code[code] = customer
                    added += 1
                db.session.commit()
                workbook.close()
                flash(f"Aktarım tamamlandı: {added} yeni müşteri eklendi, {updated} müşteri güncellendi, {unchanged} kayıt zaten günceldi, {invalid} geçersiz satır atlandı.", "success")
                return redirect(url_for("customers"))
            except Exception as exc:
                db.session.rollback()
                flash(f"Excel dosyası aktarılamadı: {exc}", "error")
        return render_template("customer_import.html")

    @app.get("/musteriler/<int:customer_id>")
    def customer_detail(customer_id):
        customer = db.get_or_404(Customer, customer_id)
        orders = customer.orders.order_by(Order.order_date.desc()).all()
        return render_template("customer_detail.html", customer=customer, orders=orders)

    @app.get("/musteriler/<int:customer_id>/vergi-levhasi")
    def view_customer_tax_document(customer_id):
        customer = db.get_or_404(Customer, customer_id)
        try:
            path = customer_tax_document_path(app, customer)
        except ValueError:
            abort(404)
        return stored_file_response(app, path, customer.tax_document_name or "vergi-levhasi", customer.tax_document_mime_type or mimetypes.guess_type(path)[0], False)

    @app.post("/musteriler/<int:customer_id>/vergi-levhasi")
    def upload_customer_tax_document(customer_id):
        customer = db.get_or_404(Customer, customer_id)
        upload = request.files.get("tax_document")
        if not upload or not upload.filename:
            flash("Lütfen vergi levhası dosyasını seçin.", "error")
            return redirect(url_for("edit_customer", customer_id=customer.id))
        original_name = secure_filename(upload.filename) or "vergi-levhasi"
        extension = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
        if extension not in CUSTOMER_TAX_DOCUMENT_EXTENSIONS:
            flash("Yalnızca PDF, JPG, PNG, WEBP veya HEIC dosyası ekleyebilirsiniz.", "error")
            return redirect(url_for("edit_customer", customer_id=customer.id))
        upload.stream.seek(0, os.SEEK_END)
        size_bytes = upload.stream.tell()
        upload.stream.seek(0)
        if size_bytes <= 0 or size_bytes > ORDER_DOCUMENT_MAX_BYTES:
            flash("Vergi levhası dosyası boş olmamalı ve en fazla 25 MB olmalıdır.", "error")
            return redirect(url_for("edit_customer", customer_id=customer.id))
        create_database_backup(app, "before_customer_tax_document_upload")
        stored_name = f"{secrets.token_urlsafe(18)}.{extension}"
        destination = os.path.join(customer_tax_document_directory(app, customer.id), stored_name)
        try:
            upload.save(destination)
            persist_local_file(app, destination, upload.mimetype or mimetypes.guess_type(original_name)[0])
            customer.tax_document_name = original_name
            customer.tax_document_stored_name = stored_name
            customer.tax_document_mime_type = upload.mimetype or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
            db.session.commit()
        except OSError as error:
            db.session.rollback()
            flash(f"Vergi levhası kaydedilemedi: {error}", "error")
            return redirect(url_for("edit_customer", customer_id=customer.id))
        try:
            suggestion = tax_certificate_suggestion(recognized_tax_document_text(destination, extension))
            if suggestion:
                flash("Vergi levhası eklendi. Bulunan bilgileri kontrol edip kaydedin.", "success")
            else:
                flash("Vergi levhası eklendi. Okunabilir alan bulunamadı; bilgileri elle girebilirsiniz.", "info")
            return render_template("customer_edit.html", customer=customer, tax_suggestion=suggestion)
        except (subprocess.SubprocessError, ValueError) as error:
            flash(f"Vergi levhası eklendi; metin okunamadı ({error}). Bilgileri elle girebilirsiniz.", "info")
            return redirect(url_for("edit_customer", customer_id=customer.id))

    @app.get("/musteriler/<int:customer_id>/cari-hesap")
    def customer_account(customer_id):
        customer = db.get_or_404(Customer, customer_id)
        all_entries = build_account_statement(customer)
        start_date = parse_date(request.args.get("start_date"))
        end_date = parse_date(request.args.get("end_date"))
        period = statement_period(all_entries, start_date, end_date)
        return render_template("customer_account.html", customer=customer, all_customers=Customer.query.order_by(Customer.name).all(), **period, start_date=request.args.get("start_date", ""), end_date=request.args.get("end_date", ""), today=date.today().isoformat())

    @app.get("/musteriler/<int:customer_id>/cari-hesap/ekstre/<detail>/<file_format>")
    def customer_account_export(customer_id, detail, file_format):
        if detail not in {"ozet", "ayrintili"} or file_format not in {"excel", "pdf"}:
            return "Geçersiz ekstre biçimi", 404
        customer = db.get_or_404(Customer, customer_id)
        start = parse_date(request.args.get("start_date"))
        end = parse_date(request.args.get("end_date"))
        period = statement_period(build_account_statement(customer), start, end)
        exporter = account_export_xlsx if file_format == "excel" else account_export_pdf
        output = exporter(customer, period, start.strftime('%d.%m.%Y') if start else '', end.strftime('%d.%m.%Y') if end else '', detailed=detail == "ayrintili")
        extension = "xlsx" if file_format == "excel" else "pdf"
        return send_file(output, as_attachment=True, download_name=f"Cari-Ekstre-{customer.id}-{detail}-{date.today().isoformat()}.{extension}",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if file_format == "excel" else "application/pdf")

    @app.post("/musteriler/<int:customer_id>/cari-hesap/hareket")
    def add_account_transaction(customer_id):
        customer = db.get_or_404(Customer, customer_id)
        transaction_type = request.form.get("transaction_type", "")
        amount = parse_money(request.form.get("amount"))
        debit_types = {"Ödeme", "Borç Dekontu", "Borç Devir"}
        credit_types = {"Tahsilat", "Alacak Dekontu", "Alacak Devir"}
        payment_method = request.form.get("payment_method", "") if transaction_type in {"Tahsilat", "Ödeme"} else None
        check_due_date = parse_date(request.form.get("check_due_date"))
        card_details, card_error = card_payment_details(payment_method, transaction_type)
        direct_supplier, supplier_error = direct_supplier_for_card_collection() if transaction_type == "Tahsilat" and payment_method == "Kredi Kartı" else (None, None)
        if transaction_type not in debit_types | credit_types:
            flash("Lütfen geçerli bir hareket türü seçin.", "error")
        elif amount <= 0:
            flash("Tutar sıfırdan büyük olmalıdır.", "error")
        elif transaction_type == "Tahsilat" and payment_method not in COLLECTION_PAYMENT_METHODS:
            flash("Tahsilat için Nakit, Çek, Banka veya Kredi Kartı seçin.", "error")
        elif transaction_type == "Ödeme" and payment_method not in ACCOUNT_PAYMENT_METHODS:
            flash("Ödeme için Nakit, Çek, Banka veya Kredi Kartı seçin.", "error")
        elif payment_method == "Çek" and (not request.form.get("check_no", "").strip() or not check_due_date):
            flash("Çek numarası ve vade tarihi zorunludur.", "error")
        elif card_error:
            flash(card_error, "error")
        elif supplier_error:
            flash(supplier_error, "error")
        elif direct_supplier and direct_supplier.id == customer.id:
            flash("Müşteri ile doğrudan ödeme yapılan tedarikçi aynı cari olamaz.", "error")
        else:
            create_database_backup(app, "before_account_transaction")
            transaction_date = parse_date(request.form.get("transaction_date")) or date.today()
            reference_no = request.form.get("reference_no", "").strip()
            description = request.form.get("description", "").strip()
            if direct_supplier:
                add_direct_card_collection_pair(customer, direct_supplier, amount, transaction_date, reference_no, description, card_details)
            else:
                transaction = AccountTransaction(customer=customer, transaction_date=transaction_date, transaction_type=transaction_type, reference_no=reference_no, description=description or transaction_type, debit=amount if transaction_type in debit_types else 0, credit=amount if transaction_type in credit_types else 0, payment_method=payment_method, check_no=request.form.get("check_no", "").strip() if payment_method == "Çek" else None, check_bank=request.form.get("check_bank", "").strip() if payment_method == "Çek" else None, check_due_date=check_due_date if payment_method == "Çek" else None, check_status="Bekliyor" if payment_method == "Çek" else None, **card_details)
                db.session.add(transaction)
            db.session.commit()
            flash(f"{transaction_type} hareketi" + (f" ve {direct_supplier.name} ödemesi" if direct_supplier else "") + " cari hesaba kaydedildi.", "success")
        return redirect(url_for("customer_account", customer_id=customer.id))

    @app.route("/kasa-cek", methods=["GET"])
    def treasury():
        today = date.today()
        summary = calculate_treasury(today)
        checks = AccountTransaction.query.filter_by(payment_method="Çek").order_by(AccountTransaction.check_due_date, AccountTransaction.id).all()
        cash_movements = CashMovement.query.order_by(CashMovement.movement_date.desc(), CashMovement.id.desc()).limit(100).all()
        movements = []
        for transaction in AccountTransaction.query.filter(AccountTransaction.transaction_type.in_(["Tahsilat", "Ödeme"]), AccountTransaction.payment_method.in_(["Nakit", "Banka", "Çek"])).all():
            is_incoming = transaction.transaction_type == "Tahsilat"
            is_check = transaction.payment_method == "Çek"
            movements.append({
                "date": transaction.transaction_date,
                "kind": "Çek" if is_check else "Kasa",
                "direction": ("Alınan" if is_incoming else "Verilen") if is_check else ("Giriş" if is_incoming else "Çıkış"),
                "description": transaction.description,
                "party": transaction.customer.name,
                "reference": transaction.check_no if is_check else transaction.reference_no,
                "due_date": transaction.check_due_date if is_check else None,
                "status": transaction.check_status if is_check else "Gerçekleşti",
                "amount": transaction.credit if is_incoming else transaction.debit,
                "customer_id": transaction.customer_id,
                "sort_time": transaction.created_at,
                "source": ("Banka Tahsilatı" if is_incoming else "Banka Ödemesi") if transaction.payment_method == "Banka" else ("Cari Tahsilat" if is_incoming else "Cari Ödeme"),
                "manual_id": None,
            })
        for expense in Expense.query.filter_by(payment_method="Nakit").all():
            movements.append({"date": expense.expense_date, "kind": "Kasa", "direction": "Çıkış", "description": expense.description, "party": expense.payee or expense.category, "reference": expense.document_no, "due_date": None, "status": "Gerçekleşti", "amount": expense.amount, "customer_id": None, "sort_time": expense.created_at, "source": "Masraf", "manual_id": None})
        for movement in CashMovement.query.all():
            movements.append({"date": movement.movement_date, "kind": "Kasa", "direction": movement.movement_type, "description": movement.description, "party": "Kasa", "reference": None, "due_date": None, "status": "Gerçekleşti", "amount": movement.amount, "customer_id": None, "sort_time": movement.created_at, "source": "Manuel", "manual_id": movement.id})
        all_cash_movements = [movement for movement in movements if movement["kind"] == "Kasa"]
        all_cash_movements.sort(key=lambda movement: (movement["date"], movement["sort_time"]), reverse=True)
        movement_filter = request.args.get("movement", "all")
        direction_filter = request.args.get("direction", "all")
        status_filter = request.args.get("status", "all")
        query = request.args.get("q", "").strip()
        start_date = parse_date(request.args.get("start_date"))
        end_date = parse_date(request.args.get("end_date"))
        if movement_filter in {"cash", "check"}:
            expected_kind = "Kasa" if movement_filter == "cash" else "Çek"
            movements = [movement for movement in movements if movement["kind"] == expected_kind]
        else:
            movement_filter = "all"
        if direction_filter == "in":
            movements = [movement for movement in movements if movement["direction"] in {"Giriş", "Alınan"}]
        elif direction_filter == "out":
            movements = [movement for movement in movements if movement["direction"] in {"Çıkış", "Verilen"}]
        else:
            direction_filter = "all"
        if status_filter != "all":
            movements = [movement for movement in movements if movement["status"] == status_filter]
        if start_date:
            movements = [movement for movement in movements if movement["date"] >= start_date]
        if end_date:
            movements = [movement for movement in movements if movement["date"] <= end_date]
        if query:
            normalized_query = normalize_search_text(query)
            movements = [movement for movement in movements if normalized_query in normalize_search_text(" ".join(str(movement.get(field) or "") for field in ["description", "party", "reference"]))]
        movements.sort(key=lambda movement: (movement["date"], movement["sort_time"]), reverse=True)
        filtered_in = sum((movement["amount"] or 0 for movement in movements if movement["direction"] in {"Giriş", "Alınan"}), Decimal("0"))
        filtered_out = sum((movement["amount"] or 0 for movement in movements if movement["direction"] in {"Çıkış", "Verilen"}), Decimal("0"))
        return render_template("treasury.html", summary=summary, checks=checks, cash_movements=cash_movements, all_cash_movements=all_cash_movements, movements=movements, filtered_in=filtered_in, filtered_out=filtered_out, today=today.isoformat(), check_statuses=CHECK_STATUSES, movement_filter=movement_filter, direction_filter=direction_filter, status_filter=status_filter, query=query, start_date=request.args.get("start_date", ""), end_date=request.args.get("end_date", ""))

    @app.post("/kasa-cek/kasa-hareketi")
    def add_cash_movement():
        movement_type = request.form.get("movement_type", "")
        amount = parse_money(request.form.get("amount"))
        description = request.form.get("description", "").strip()
        if movement_type not in {"Giriş", "Çıkış"} or amount <= 0 or not description:
            flash("Kasa hareketinin türünü, açıklamasını ve tutarını kontrol edin.", "error")
        else:
            create_database_backup(app, "before_cash_movement")
            db.session.add(CashMovement(movement_date=parse_date(request.form.get("movement_date")) or date.today(), movement_type=movement_type, description=description, amount=amount))
            db.session.commit()
            flash("Kasa hareketi kaydedildi.", "success")
        return redirect(url_for("treasury"))

    @app.post("/kasa-cek/kasa-hareketi/<int:movement_id>/sil")
    def delete_cash_movement(movement_id):
        movement = db.get_or_404(CashMovement, movement_id)
        create_database_backup(app, "before_cash_movement_delete")
        db.session.delete(movement)
        db.session.commit()
        flash("Kasa hareketi silindi.", "success")
        return redirect(url_for("treasury"))

    @app.post("/kasa-cek/cek/<int:transaction_id>/durum")
    def update_check_status(transaction_id):
        transaction = db.get_or_404(AccountTransaction, transaction_id)
        status = request.form.get("check_status", "")
        if transaction.payment_method != "Çek" or status not in CHECK_STATUSES:
            flash("Geçersiz çek durumu.", "error")
        else:
            create_database_backup(app, "before_check_status")
            transaction.check_status = status
            db.session.commit()
            flash("Çek durumu güncellendi.", "success")
        return redirect(url_for("treasury"))

    @app.post("/musteriler/<int:customer_id>/cari-hesap/hareket/<int:transaction_id>/sil")
    def delete_account_transaction(customer_id, transaction_id):
        transaction = db.get_or_404(AccountTransaction, transaction_id)
        if transaction.customer_id != customer_id:
            return ("Geçersiz cari hareketi", 400)
        create_database_backup(app, "before_account_transaction_delete")
        linked = db.session.get(AccountTransaction, transaction.linked_transaction_id) if transaction.linked_transaction_id else None
        if linked:
            db.session.delete(linked)
        db.session.delete(transaction)
        db.session.commit()
        flash("Cari hesap hareketi silindi.", "success")
        return redirect(url_for("customer_account", customer_id=customer_id))

    @app.get("/musteriler/<int:customer_id>/cari-hesap/ekstre.csv")
    def customer_account_csv(customer_id):
        customer = db.get_or_404(Customer, customer_id)
        entries = build_account_statement(customer)
        start_date = parse_date(request.args.get("start_date"))
        end_date = parse_date(request.args.get("end_date"))
        entries = [entry for entry in entries if (not start_date or entry["date"] >= start_date) and (not end_date or entry["date"] <= end_date)]
        output = StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Cari Kodu", customer.code or "", "Cari Ünvanı", customer.name])
        writer.writerow(["Tarih", "Sipariş / Referans No", "Açıklama", "Borç", "Alacak", "Bakiye"])
        for entry in entries:
            writer.writerow([entry["date"].strftime("%d.%m.%Y"), entry["reference"], entry["description"], f"{entry['debit']:.2f}", f"{entry['credit']:.2f}", f"{entry['balance']:.2f}"])
        response = make_response("\ufeff" + output.getvalue())
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = f'attachment; filename="cari-ekstre-{customer.code or customer.id}.csv"'
        return response

    @app.route("/musteriler/<int:customer_id>/duzenle", methods=["GET", "POST"])
    def edit_customer(customer_id):
        customer = db.get_or_404(Customer, customer_id)
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            code = request.form.get("code", "").strip() or None
            duplicate = Customer.query.filter(Customer.code == code, Customer.id != customer.id).first() if code else None
            if not name:
                flash("Müşteri adı zorunludur.", "error")
            elif duplicate:
                flash("Bu cari kod başka bir müşteride kullanılıyor.", "error")
            else:
                customer.name = name
                customer.code = code
                customer.contact_name = request.form.get("contact_name", "").strip()
                customer.phone = request.form.get("phone", "").strip()
                customer.mobile = request.form.get("mobile", "").strip()
                customer.email = request.form.get("email", "").strip()
                customer.city = request.form.get("city", "").strip()
                customer.address = request.form.get("address", "").strip()
                customer.notes = request.form.get("notes", "").strip()
                customer.tax_office = request.form.get("tax_office", "").strip()
                customer.tax_number = re.sub(r"\D", "", request.form.get("tax_number", ""))[:20]
                customer.shipment_contact = request.form.get("shipment_contact", "").strip()
                customer.shipment_phone = request.form.get("shipment_phone", "").strip()
                customer.shipment_city = request.form.get("shipment_city", "").strip()
                customer.shipment_address = request.form.get("shipment_address", "").strip()
                customer.shipment_note = request.form.get("shipment_note", "").strip()
                create_database_backup(app, "before_customer_edit")
                db.session.commit()
                flash("Müşteri bilgileri güncellendi.", "success")
                return redirect(url_for("customer_detail", customer_id=customer.id))
        return render_template("customer_edit.html", customer=customer, tax_suggestion={})

    @app.post("/musteriler/<int:customer_id>/sil")
    def delete_customer(customer_id):
        customer = db.get_or_404(Customer, customer_id)
        if customer.orders.count() > 0:
            flash("Bu müşteriye ait siparişler bulunduğu için müşteri silinemedi. Sipariş geçmişinin korunması gerekir.", "error")
            return redirect(url_for("customer_detail", customer_id=customer.id))
        if customer.account_transactions:
            flash("Bu müşteriye ait cari hesap hareketleri bulunduğu için müşteri silinemedi.", "error")
            return redirect(url_for("customer_detail", customer_id=customer.id))
        create_database_backup(app, "before_customer_delete")
        name = customer.name
        db.session.delete(customer)
        db.session.commit()
        flash(f"{name} müşteri kaydı silindi.", "success")
        return redirect(url_for("customers"))

    @app.route("/urunler", methods=["GET", "POST"])
    def products():
        if request.method == "POST":
            if request.form.get("form_action") == "stock_movement":
                product_id = request.form.get("product_id", type=int)
                movement_type = request.form.get("movement_type", "")
                quantity = int(parse_money(request.form.get("quantity")))
                product = db.session.get(Product, product_id) if product_id else None
                if not product:
                    flash("Stok hareketi için bir ürün seçin.", "error")
                elif movement_type not in STOCK_MOVEMENT_TYPES:
                    flash("Geçerli bir stok hareketi seçin.", "error")
                elif quantity < 1:
                    flash("Stok adedi en az 1 olmalıdır.", "error")
                else:
                    create_database_backup(app, "before_stock_movement")
                    db.session.add(StockMovement(
                        product_id=product.id,
                        movement_type=movement_type,
                        quantity=quantity,
                        movement_date=parse_date(request.form.get("movement_date")) or date.today(),
                        note=request.form.get("note", "").strip() or None,
                    ))
                    db.session.commit()
                    flash(f"{product.name} için stok hareketi kaydedildi.", "success")
                    return redirect(url_for("products", q=request.form.get("q", "")))
            else:
                name = request.form.get("name", "").strip()
                if not name:
                    flash("Ürün adı zorunludur.", "error")
                else:
                    code = request.form.get("code", "").strip() or None
                    if code and Product.query.filter_by(code=code).first():
                        flash("Bu ürün kodu zaten kullanılıyor.", "error")
                    else:
                        db.session.add(Product(name=name, code=code, description=request.form.get("description"), special_code=request.form.get("special_code"), group_name=request.form.get("group_name"), default_variant=request.form.get("default_variant"), unit=request.form.get("unit") or "Adet", unit_price=parse_money(request.form.get("unit_price")), purchase_price=parse_money(request.form.get("purchase_price")), include_in_catalog=request.form.get("include_in_catalog") == "on", include_in_price_list=request.form.get("include_in_price_list") == "on"))
                        db.session.commit()
                        flash("Ürün kartı oluşturuldu.", "success")
                        return redirect(url_for("products"))
        query = request.args.get("q", "").strip()
        records = Product.query
        if query:
            if db.engine.dialect.name == "sqlite":
                pattern = f"%{normalize_search_text(query)}%"
                records = records.filter(db.or_(
                    db.func.normalize_tr(Product.name).like(pattern),
                    db.func.normalize_tr(Product.code).like(pattern),
                    db.func.normalize_tr(Product.description).like(pattern),
                    db.func.normalize_tr(Product.special_code).like(pattern),
                    db.func.normalize_tr(Product.group_name).like(pattern),
                    db.func.normalize_tr(Product.default_variant).like(pattern),
                ))
            else:
                pattern = f"%{query}%"
                records = records.filter(db.or_(Product.name.ilike(pattern), Product.code.ilike(pattern), Product.description.ilike(pattern), Product.special_code.ilike(pattern), Product.group_name.ilike(pattern), Product.default_variant.ilike(pattern)))
        products_list = records.order_by(Product.name).all()
        product_ids = [product.id for product in products_list]
        physical_stock = {product_id: 0 for product_id in product_ids}
        for movement in StockMovement.query.filter(StockMovement.product_id.in_(product_ids)).all() if product_ids else []:
            physical_stock[movement.product_id] += movement.signed_quantity
        reserved_stock = {product_id: 0 for product_id in product_ids}
        expected_stock = {product_id: 0 for product_id in product_ids}
        active_orders = Order.query.options(selectinload(Order.items)).filter(~Order.status.in_(FINANCIAL_ORDER_STATUSES + ["İptal Edildi"])).all()
        for order in active_orders:
            for item in order.items:
                if item.product_id not in physical_stock:
                    continue
                if order.order_type == "Satış":
                    reserved_stock[item.product_id] += item.quantity
                elif order.order_type == "Satın Alma":
                    expected_stock[item.product_id] += item.quantity
        stock_rows = [{
            "product": product,
            "physical": physical_stock[product.id],
            "reserved": reserved_stock[product.id],
            "available": physical_stock[product.id] - reserved_stock[product.id],
            "expected": expected_stock[product.id],
        } for product in products_list]
        stock_sort = request.args.get("stock_sort", "").strip()
        stock_direction = request.args.get("stock_direction", "").strip().lower()
        stock_sort_keys = {"physical", "reserved", "available", "expected"}
        if stock_sort in {"code", "name"}:
            stock_direction = "desc" if stock_direction == "desc" else "asc"
            stock_rows.sort(key=lambda row: stock_text_sort_key(getattr(row["product"], stock_sort)), reverse=stock_direction == "desc")
        elif stock_sort in stock_sort_keys:
            # İlk tıklamada en yüksek stok üstte görünür; aynı başlığa yeniden
            # tıklanınca sıralama tersine döner.
            stock_rows.sort(key=lambda row: row[stock_sort], reverse=stock_direction != "asc")
        else:
            stock_sort = ""
            stock_direction = ""
        selected_stock_product = db.session.get(Product, request.args.get("stock_product_id", type=int)) if request.args.get("stock_product_id", type=int) else None
        selected_stock_movements = []
        if selected_stock_product:
            selected_stock_movements = StockMovement.query.filter_by(product_id=selected_stock_product.id).order_by(
                StockMovement.movement_date.desc(), StockMovement.id.desc()
            ).all()
        recent_stock_movements = StockMovement.query.options(joinedload(StockMovement.product)).order_by(StockMovement.movement_date.desc(), StockMovement.id.desc()).limit(12).all()
        displayed_stock_movement_ids = {movement.id for movement in selected_stock_movements + recent_stock_movements}
        stock_movement_invoices = {
            item.stock_movement_id: item.invoice
            for item in InvoiceItem.query.options(joinedload(InvoiceItem.invoice)).filter(InvoiceItem.stock_movement_id.in_(displayed_stock_movement_ids)).all()
        } if displayed_stock_movement_ids else {}
        stock_picker_products = [
            {
                "id": product.id,
                "name": product.name,
                "code": product.code or "",
                "variant": product.default_variant or "",
            }
            for product in Product.query.filter_by(active=True).order_by(Product.name).all()
        ]
        return render_template("products.html", products=products_list, stock_rows=stock_rows, recent_stock_movements=recent_stock_movements,
            stock_movement_types=STOCK_MOVEMENT_TYPES, query=query, stock_sort=stock_sort, stock_direction=stock_direction,
            stock_picker_products=stock_picker_products, selected_stock_product=selected_stock_product,
            selected_stock_movements=selected_stock_movements,
            stock_movement_invoices=stock_movement_invoices,
            catalog_count=Product.query.filter_by(include_in_catalog=True, active=True).count(),
            price_list_count=Product.query.filter_by(include_in_price_list=True, active=True).count(), today=date.today().isoformat())

    @app.route("/urunler/<int:product_id>/duzenle", methods=["GET", "POST"])
    def edit_product(product_id):
        product = db.get_or_404(Product, product_id)
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            code = request.form.get("code", "").strip() or None
            duplicate = Product.query.filter(Product.code == code, Product.id != product.id).first() if code else None
            if not name:
                flash("Ürün adı zorunludur.", "error")
            elif duplicate:
                flash("Bu ürün kodu başka bir kartta kullanılıyor.", "error")
            else:
                product.name = name
                product.code = code
                product.description = request.form.get("description", "").strip()
                product.special_code = request.form.get("special_code", "").strip()
                product.group_name = request.form.get("group_name", "").strip()
                product.default_variant = request.form.get("default_variant", "").strip()
                product.unit = request.form.get("unit") or "Adet"
                product.unit_price = parse_money(request.form.get("unit_price"))
                product.purchase_price = parse_money(request.form.get("purchase_price"))
                product.include_in_catalog = request.form.get("include_in_catalog") == "on"
                product.include_in_price_list = request.form.get("include_in_price_list") == "on"
                db.session.commit()
                flash("Ürün kartı ve fiyatları güncellendi.", "success")
                return redirect(url_for("products", q=product.code or product.name))
        return render_template("product_edit.html", product=product)

    @app.post("/urunler/<int:product_id>/secim")
    def toggle_product_selection(product_id):
        product = db.get_or_404(Product, product_id)
        target = request.form.get("target")
        if target not in {"catalog", "price_list"}:
            flash("Geçersiz ürün seçimi.", "error")
            return redirect(url_for("products"))
        create_database_backup(app, "before_product_selection")
        if target == "catalog":
            product.include_in_catalog = not product.include_in_catalog
            label = "katalog"
            selected = product.include_in_catalog
        else:
            product.include_in_price_list = not product.include_in_price_list
            label = "fiyat listesi"
            selected = product.include_in_price_list
        db.session.commit()
        flash(f"{product.name} {label} seçimine {'eklendi' if selected else 'çıkarıldı'}.", "success")
        return redirect(url_for("products", q=request.form.get("q", "")))

    @app.get("/katalog")
    def product_catalog():
        selected_products = Product.query.filter_by(include_in_catalog=True, active=True).order_by(Product.group_name, Product.name).all()
        return render_template("product_catalog.html", products=selected_products, generated_at=datetime.now())

    @app.get("/fiyat-listesi")
    def product_price_list():
        selected_products = Product.query.filter_by(include_in_price_list=True, active=True).order_by(Product.group_name, Product.name).all()
        return render_template("product_price_list.html", products=selected_products, generated_at=datetime.now())

    @app.get("/fiyat-listesi/csv")
    def product_price_list_csv():
        selected_products = Product.query.filter_by(include_in_price_list=True, active=True).order_by(Product.group_name, Product.name).all()
        output = StringIO()
        output.write("\ufeff")
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Ürün Kodu", "Ürün Adı", "Ürün Grubu", "Ayrıntı", "Birim", "Satış Fiyatı (₺)"])
        for product in selected_products:
            writer.writerow([product.code or "", product.name, product.group_name or "", product.default_variant or "", product.unit, f"{product.unit_price:.2f}".replace(".", ",")])
        response = make_response(output.getvalue())
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = f"attachment; filename=fiyat-listesi-{date.today().isoformat()}.csv"
        return response

    @app.route("/urunler/aktar", methods=["GET", "POST"])
    def import_products():
        if request.method == "POST":
            uploaded = request.files.get("file")
            if not uploaded or not uploaded.filename:
                flash("Lütfen bir Excel dosyası seçin.", "error")
                return redirect(url_for("import_products"))
            if not uploaded.filename.lower().endswith(".xlsx"):
                flash("Yalnızca .xlsx uzantılı Excel dosyaları desteklenir.", "error")
                return redirect(url_for("import_products"))
            try:
                create_database_backup(app, "before_product_import")
                from openpyxl import load_workbook
                workbook = load_workbook(uploaded, read_only=True, data_only=True)
                sheet = workbook.active
                rows = sheet.iter_rows(values_only=True)
                raw_headers = next(rows, None)
                if not raw_headers:
                    raise ValueError("Dosya boş.")
                headers = {str(value or "").strip().casefold(): index for index, value in enumerate(raw_headers)}
                aliases = {
                    "code": ["kart kodu", "stok kodu", "ürün kodu", "urun kodu", "kod"],
                    "name": ["açıklama", "aciklama", "ürün adı", "urun adi", "stok adı"],
                    "special_code": ["özel kod 3", "ozel kod 3", "özel kod", "ozel kod"],
                    "group_name": ["grup kodu açıklama", "grup kodu aciklama", "grup açıklama", "ürün grubu"],
                }
                columns = {key: next((headers[alias] for alias in names if alias in headers), None) for key, names in aliases.items()}
                if columns["code"] is None or columns["name"] is None:
                    raise ValueError("'Kart Kodu' veya 'Açıklama' sütunu bulunamadı.")
                added = updated = unchanged = invalid = 0
                existing_by_code = {product.code: product for product in Product.query.filter(Product.code.isnot(None)).all()}
                for row in rows:
                    def cell(field):
                        index = columns[field]
                        return str(row[index]).strip() if index is not None and index < len(row) and row[index] is not None else ""
                    code, name = cell("code"), cell("name")
                    if not code or not name:
                        invalid += 1
                        continue
                    values = {"name": name, "special_code": cell("special_code"), "group_name": cell("group_name")}
                    if code in existing_by_code:
                        product = existing_by_code[code]
                        changed = any((getattr(product, field) or "") != value for field, value in values.items())
                        if changed:
                            for field, value in values.items():
                                setattr(product, field, value)
                            updated += 1
                        else:
                            unchanged += 1
                    else:
                        product = Product(code=code, unit="Adet", unit_price=0, active=True, **values)
                        db.session.add(product)
                        existing_by_code[code] = product
                        added += 1
                db.session.commit()
                workbook.close()
                flash(f"Ürün aktarımı tamamlandı: {added} yeni ürün eklendi, {updated} ürün güncellendi, {unchanged} kayıt zaten günceldi, {invalid} geçersiz satır atlandı.", "success")
                return redirect(url_for("products"))
            except Exception as exc:
                db.session.rollback()
                flash(f"Ürün Excel dosyası aktarılamadı: {exc}", "error")
        return render_template("product_import.html")

    @app.route("/ozon-satislari", methods=["GET", "POST"])
    def ozon_sales():
        today = date.today()
        selected_month = request.values.get("month", today.strftime("%Y-%m"))
        settings = load_ozon_settings(app)
        stores = settings["stores"]
        selected_store_key = request.values.get("store") or (stores[0]["key"] if stores else "all")
        if selected_store_key != "all" and not get_ozon_store(settings, selected_store_key):
            selected_store_key = "all"
        try:
            month_start = datetime.strptime(selected_month, "%Y-%m").date().replace(day=1)
        except ValueError:
            selected_month = today.strftime("%Y-%m")
            month_start = today.replace(day=1)
        month_end = date(month_start.year, month_start.month, calendar.monthrange(month_start.year, month_start.month)[1])

        if request.method == "POST":
            if selected_store_key == "all":
                flash("Satış kaydı eklemek için önce bir mağaza seçin.", "error")
                return redirect(url_for("ozon_sales", month=selected_month, store="all"))
            product_id = request.form.get("product_id", type=int)
            product = db.session.get(Product, product_id) if product_id else None
            ozon_sku = request.form.get("ozon_sku", "").strip()
            ozon_product = None
            if ozon_sku:
                ozon_product = OzonProduct.query.filter_by(store_key=selected_store_key, sku=ozon_sku).first()
                if not ozon_product:
                    ozon_product = OzonProduct.query.filter_by(store_key=selected_store_key, offer_id=ozon_sku).first()
            product_name = request.form.get("product_name", "").strip() or (product.name if product else (ozon_product.name if ozon_product else ""))
            quantity = request.form.get("quantity", type=int) or 1
            sales_amount = parse_money(request.form.get("sales_amount"))
            cost_amount = parse_money(request.form.get("cost_amount"))
            if cost_amount <= 0 and ozon_product:
                cost_amount = (ozon_product.cost_unit_price or Decimal("0")) * quantity
            if not product_name:
                flash("Ürün adı zorunludur.", "error")
            elif quantity < 1:
                flash("Adet en az 1 olmalıdır.", "error")
            elif sales_amount < 0:
                flash("Satış tutarı negatif olamaz.", "error")
            else:
                create_database_backup(app, "before_ozon_sale_add")
                db.session.add(OzonSale(
                    store_key=selected_store_key,
                    sale_date=parse_date(request.form.get("sale_date")) or today,
                    posting_number=request.form.get("posting_number", "").strip(),
                    product_id=product.id if product else None,
                    ozon_sku=ozon_sku,
                    product_name=product_name,
                    quantity=quantity,
                    sales_amount=sales_amount,
                    cost_amount=cost_amount,
                    commission_amount=parse_money(request.form.get("commission_amount")),
                    logistics_amount=parse_money(request.form.get("logistics_amount")),
                    advertising_amount=parse_money(request.form.get("advertising_amount")),
                    other_expense_amount=parse_money(request.form.get("other_expense_amount")),
                ))
                db.session.commit()
                flash("Ozon satış kaydı eklendi.", "success")
                return redirect(url_for("ozon_sales", month=selected_month, store=selected_store_key))

        records = OzonSale.query.filter(OzonSale.sale_date.between(month_start, month_end)).order_by(OzonSale.sale_date.desc(), OzonSale.id.desc()).all()
        if selected_store_key != "all":
            records = [record for record in records if record.store_key == selected_store_key]
        sales_total = sum((record.sales_amount or Decimal("0") for record in records), Decimal("0"))
        cost_total = sum((record.cost_amount or Decimal("0") for record in records), Decimal("0"))
        commission_total = sum((record.commission_amount or Decimal("0") for record in records), Decimal("0"))
        logistics_total = sum((record.logistics_amount or Decimal("0") for record in records), Decimal("0"))
        advertising_total = sum((record.advertising_amount or Decimal("0") for record in records), Decimal("0"))
        other_expense_total = sum((record.other_expense_amount or Decimal("0") for record in records), Decimal("0"))
        platform_expense_total = commission_total + logistics_total + advertising_total + other_expense_total
        net_revenue_total = sales_total - platform_expense_total
        profit_total = net_revenue_total - cost_total
        markup = (profit_total / cost_total * Decimal("100")) if cost_total else None
        products = Product.query.filter_by(active=True).order_by(Product.name).all()
        product_page = max(request.args.get("product_page", 1, type=int) or 1, 1)
        products_per_page = 20
        ozon_products_query = OzonProduct.query.order_by(OzonProduct.name)
        if selected_store_key != "all":
            ozon_products_query = ozon_products_query.filter_by(store_key=selected_store_key)
        ozon_product_count = ozon_products_query.count()
        product_total_pages = max((ozon_product_count + products_per_page - 1) // products_per_page, 1)
        product_page = min(product_page, product_total_pages)
        ozon_products = ozon_products_query.offset((product_page - 1) * products_per_page).limit(products_per_page).all()
        finance_snapshots = OzonFinanceSnapshot.query.filter_by(period_month=selected_month).order_by(OzonFinanceSnapshot.store_key).all()
        if selected_store_key != "all":
            finance_snapshots = [snapshot for snapshot in finance_snapshots if snapshot.store_key == selected_store_key]
        return render_template("ozon_sales.html", records=records, products=products, today=today.isoformat(), selected_month=selected_month,
            sales_total=sales_total, cost_total=cost_total, commission_total=commission_total, logistics_total=logistics_total,
            advertising_total=advertising_total, other_expense_total=other_expense_total, platform_expense_total=platform_expense_total,
            net_revenue_total=net_revenue_total, profit_total=profit_total, markup=markup,
            finance_snapshots=finance_snapshots, ozon_products=ozon_products, ozon_product_count=ozon_product_count,
            product_page=product_page, product_total_pages=product_total_pages, stores=stores, selected_store_key=selected_store_key,
            selected_store=get_ozon_store(settings, selected_store_key), api_configured=bool(get_ozon_store(settings, selected_store_key) and get_ozon_store(settings, selected_store_key)["api_key"]))

    @app.post("/ozon-satislari/urunleri-senkronize")
    def sync_ozon_products():
        selected_month = request.form.get("month", date.today().strftime("%Y-%m"))
        store_key = request.form.get("store", "")
        settings = load_ozon_settings(app)
        store = get_ozon_store(settings, store_key)
        if not store or not store["client_id"] or not store["api_key"]:
            flash("Ürünleri çekmek için önce tek bir mağaza seçin ve API bilgilerini kaydedin.", "error")
            return redirect(url_for("ozon_sales", month=selected_month, store=store_key or "all"))
        try:
            last_id, listed_items, page_count = "", [], 0
            while True:
                response = ozon_api_post(store, "/v3/product/list", {"filter": {"visibility": "ALL"}, "last_id": last_id, "limit": 1000})
                result = response.get("result", {})
                page_items = result.get("items", []) if isinstance(result, dict) else []
                listed_items.extend(page_items)
                page_count += 1
                last_id = str(result.get("last_id") or "")
                if not page_items or not last_id or page_count >= 100:
                    break
            product_ids = [item.get("product_id") or item.get("id") for item in listed_items]
            product_ids = [item for item in product_ids if item is not None]
            detailed_items = []
            for index in range(0, len(product_ids), 1000):
                details = ozon_api_post(store, "/v3/product/info/list", {"product_id": product_ids[index:index + 1000], "offer_id": [], "sku": []})
                detailed_items.extend(details.get("items") or details.get("result", {}).get("items", []))
        except urllib.error.HTTPError as error:
            flash(f"Ozon ürünleri alınamadı (HTTP {error.code}). Bu API anahtarının ürünleri görüntüleme yetkisini kontrol edin.", "error")
            return redirect(url_for("ozon_sales", month=selected_month, store=store_key))
        except (urllib.error.URLError, TimeoutError):
            flash("Ozon ürünlerine bağlanılamadı. İnternet bağlantısını kontrol edip tekrar deneyin.", "error")
            return redirect(url_for("ozon_sales", month=selected_month, store=store_key))
        except (ValueError, json.JSONDecodeError) as error:
            flash(f"Ozon ürün verisi okunamadı: {error}", "error")
            return redirect(url_for("ozon_sales", month=selected_month, store=store_key))

        source_by_id = {str(item.get("product_id") or item.get("id")): item for item in listed_items}
        added = updated = 0
        create_database_backup(app, "before_ozon_product_sync")
        for item in detailed_items:
            product_id = str(item.get("id") or item.get("product_id") or "")
            if not product_id:
                continue
            source = source_by_id.get(product_id, {})
            record = OzonProduct.query.filter_by(store_key=store_key, ozon_product_id=product_id).first()
            values = {
                "offer_id": str(item.get("offer_id") or source.get("offer_id") or ""),
                "sku": str(item.get("sku") or source.get("sku") or ""),
                "name": str(item.get("name") or source.get("name") or "Ürün adı alınamadı"),
                "current_price": ozon_money_from_item(item, "price", "marketing_price"),
                "old_price": ozon_money_from_item(item, "old_price"),
                "marketing_price": ozon_money_from_item(item, "marketing_price"),
                "is_archived": bool(item.get("is_archived") or source.get("is_archived")),
                "synced_at": datetime.utcnow(),
            }
            if record:
                for field, value in values.items():
                    setattr(record, field, value)
                updated += 1
            else:
                db.session.add(OzonProduct(store_key=store_key, ozon_product_id=product_id, cost_unit_price=0, **values))
                added += 1
        db.session.commit()
        flash(f"{store['name']} ürünleri güncellendi: {added} yeni, {updated} mevcut ürün. Maliyet alanları korunmuştur.", "success")
        return redirect(url_for("ozon_sales", month=selected_month, store=store_key))

    @app.post("/ozon-satislari/urunler/<int:product_id>/maliyet")
    def update_ozon_product_cost(product_id):
        record = db.get_or_404(OzonProduct, product_id)
        record.cost_unit_price = parse_money(request.form.get("cost_unit_price"))
        create_database_backup(app, "before_ozon_product_cost_update")
        db.session.commit()
        flash(f"{record.name} için birim maliyet kaydedildi.", "success")
        return redirect(url_for("ozon_sales", month=request.form.get("month", date.today().strftime("%Y-%m")), store=record.store_key))

    @app.post("/ozon-satislari/satislari-senkronize")
    def sync_ozon_sales():
        selected_month = request.form.get("month", date.today().strftime("%Y-%m"))
        store_key = request.form.get("store", "")
        try:
            month_start = datetime.strptime(selected_month, "%Y-%m").date().replace(day=1)
        except ValueError:
            flash("Geçerli bir ay seçin.", "error")
            return redirect(url_for("ozon_sales"))
        settings = load_ozon_settings(app)
        store = get_ozon_store(settings, store_key)
        if not store or not store["client_id"] or not store["api_key"]:
            flash("Satışları almak için önce tek bir mağaza seçin ve API bilgilerini kaydedin.", "error")
            return redirect(url_for("ozon_sales", month=selected_month, store=store_key or "all"))

        month_end = date(month_start.year, month_start.month, calendar.monthrange(month_start.year, month_start.month)[1])
        try:
            operations, page = [], 1
            while page <= 100:
                response = ozon_api_post(store, "/v3/finance/transaction/list", {
                    "filter": {"date": {"from": f"{month_start.isoformat()}T00:00:00.000Z", "to": f"{month_end.isoformat()}T23:59:59.999Z"}, "posting_number": "", "transaction_type": "all"},
                    "page": page,
                    "page_size": 1000,
                })
                result = response.get("result", {})
                page_operations = result.get("operations", []) if isinstance(result, dict) else []
                operations.extend(page_operations)
                page_count = int(result.get("page_count") or 0) if isinstance(result, dict) else 0
                if not page_operations or not page_count or page >= page_count:
                    break
                page += 1
        except urllib.error.HTTPError as error:
            flash(f"Ozon satışları alınamadı (HTTP {error.code}). API anahtarının finans hareketlerini görüntüleme yetkisini kontrol edin.", "error")
            return redirect(url_for("ozon_sales", month=selected_month, store=store_key))
        except (urllib.error.URLError, TimeoutError):
            flash("Ozon satışlarına bağlanılamadı. İnternet bağlantısını kontrol edip tekrar deneyin.", "error")
            return redirect(url_for("ozon_sales", month=selected_month, store=store_key))
        except (ValueError, json.JSONDecodeError) as error:
            flash(f"Ozon satış verisi okunamadı: {error}", "error")
            return redirect(url_for("ozon_sales", month=selected_month, store=store_key))

        products_by_sku = {product.sku: product for product in OzonProduct.query.filter_by(store_key=store_key).all() if product.sku}
        imported, updated, without_cost = 0, 0, 0
        create_database_backup(app, "before_ozon_sales_sync")
        for operation in operations:
            # Gerçekleşen satış tahakkuklarını alır; iade ve hizmet kesintileri
            # finans özetinde ayrıca korunur, satış kartlarına kopyalanmaz.
            accrual = parse_money(operation.get("accruals_for_sale"))
            if operation.get("type") != "orders" or accrual <= 0:
                continue
            operation_id = str(operation.get("operation_id") or "")
            posting = operation.get("posting") or {}
            posting_number = str(posting.get("posting_number") or "")
            if not operation_id or not posting_number:
                continue
            items = operation.get("items") or []
            item_names = [str(item.get("name") or "") for item in items if item.get("name")]
            item_skus = [str(item.get("sku") or "") for item in items if item.get("sku") is not None]
            quantity = max(len(items), 1)
            cost_amount = sum(((products_by_sku.get(sku).cost_unit_price or Decimal("0")) for sku in item_skus if products_by_sku.get(sku)), Decimal("0"))
            if not cost_amount:
                without_cost += 1
            services_amount = sum((abs(parse_money(service.get("price"))) for service in (operation.get("services") or [])), Decimal("0"))
            existing = OzonSale.query.filter_by(store_key=store_key, external_operation_id=operation_id).first()
            values = {
                "sale_date": parse_ozon_operation_date(operation.get("operation_date")),
                "posting_number": posting_number,
                "ozon_sku": item_skus[0] if len(item_skus) == 1 else "",
                "product_name": ", ".join(item_names) or f"Ozon siparişi {posting_number}",
                "quantity": quantity,
                "sales_amount": accrual,
                "commission_amount": abs(parse_money(operation.get("sale_commission"))),
                "logistics_amount": abs(parse_money(operation.get("delivery_charge"))) + abs(parse_money(operation.get("return_delivery_charge"))),
                "other_expense_amount": services_amount,
            }
            if existing:
                for field, value in values.items():
                    setattr(existing, field, value)
                if (existing.cost_amount or Decimal("0")) <= 0:
                    existing.cost_amount = cost_amount
                updated += 1
            else:
                db.session.add(OzonSale(store_key=store_key, external_operation_id=operation_id, cost_amount=cost_amount, **values))
                imported += 1
        db.session.commit()
        flash(f"{store['name']} satışları güncellendi: {imported} yeni, {updated} güncellenen kayıt. {without_cost} kaydın ürün maliyeti henüz girilmemiş olabilir.", "success")
        return redirect(url_for("ozon_sales", month=selected_month, store=store_key))

    @app.post("/ozon-satislari/api-ayarlari")
    def save_ozon_api_settings():
        month = request.form.get("month", date.today().strftime("%Y-%m"))
        settings = load_ozon_settings(app)
        stores = settings["stores"]
        requested_key = request.form.get("store_key", "new")
        name = request.form.get("store_name", "").strip()
        client_id = request.form.get("client_id", "").strip()
        existing = get_ozon_store(settings, requested_key)
        api_key = request.form.get("api_key", "").strip() or (existing["api_key"] if existing else "")
        if requested_key == "new":
            next_number = len(stores) + 1
            requested_key = f"magaza-{next_number}"
            while get_ozon_store(settings, requested_key):
                next_number += 1
                requested_key = f"magaza-{next_number}"
        if not name or not client_id or not api_key:
            flash("Mağaza adı, Ozon Client ID ve API Key zorunludur.", "error")
        else:
            updated = {"key": requested_key, "name": name, "client_id": client_id, "api_key": api_key}
            stores = [updated if store["key"] == requested_key else store for store in stores]
            if not existing:
                stores.append(updated)
            save_ozon_settings(app, stores)
            flash(f"{name} için Ozon Seller API bağlantısı kaydedildi.", "success")
        return redirect(url_for("ozon_sales", month=month, store=requested_key if name else "all"))

    @app.post("/ozon-satislari/senkronize")
    def sync_ozon_finance():
        selected_month = request.form.get("month", date.today().strftime("%Y-%m"))
        store_key = request.form.get("store", "")
        try:
            month_start = datetime.strptime(selected_month, "%Y-%m").date().replace(day=1)
        except ValueError:
            flash("Geçerli bir ay seçin.", "error")
            return redirect(url_for("ozon_sales"))
        settings = load_ozon_settings(app)
        store = get_ozon_store(settings, store_key)
        if not store or not store["client_id"] or not store["api_key"]:
            flash("Önce bu mağaza için Ozon Seller Client ID ve API Key bilgilerini kaydedin.", "error")
            return redirect(url_for("ozon_sales", month=selected_month, store=store_key or "all"))

        month_end = date(month_start.year, month_start.month, calendar.monthrange(month_start.year, month_start.month)[1])
        payload = {
            "date": {"from": f"{month_start.isoformat()}T00:00:00.000Z", "to": f"{month_end.isoformat()}T23:59:59.999Z"},
            "transaction_type": "all",
        }
        api_request = urllib.request.Request(
            "https://api-seller.ozon.ru/v3/finance/transaction/totals",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Client-Id": store["client_id"], "Api-Key": store["api_key"], "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(api_request, timeout=30) as response:
                api_response = json.loads(response.read().decode("utf-8"))
            result = api_response.get("result", {})
            if not isinstance(result, dict):
                raise ValueError("Ozon finans özeti beklenen biçimde gelmedi.")
        except urllib.error.HTTPError as error:
            flash(f"Ozon bağlantısı reddedildi (HTTP {error.code}). Client ID ve API Key bilgilerini kontrol edin.", "error")
            return redirect(url_for("ozon_sales", month=selected_month, store=store_key))
        except (urllib.error.URLError, TimeoutError):
            flash("Ozon'a bağlanılamadı. İnternet bağlantısını kontrol edip tekrar deneyin.", "error")
            return redirect(url_for("ozon_sales", month=selected_month, store=store_key))
        except (ValueError, json.JSONDecodeError) as error:
            flash(f"Ozon finans verisi okunamadı: {error}", "error")
            return redirect(url_for("ozon_sales", month=selected_month, store=store_key))

        create_database_backup(app, "before_ozon_finance_sync")
        snapshot_key = f"{store_key}:{selected_month}"
        snapshot = db.session.get(OzonFinanceSnapshot, snapshot_key) or OzonFinanceSnapshot(report_month=snapshot_key, store_key=store_key, period_month=selected_month)
        snapshot.sales_accrual = parse_money(result.get("accruals_for_sale"))
        snapshot.sale_commission = parse_money(result.get("sale_commission"))
        snapshot.processing_delivery = parse_money(result.get("processing_and_delivery"))
        snapshot.refunds_cancellations = parse_money(result.get("refunds_and_cancellations"))
        snapshot.services_amount = parse_money(result.get("services_amount"))
        snapshot.other_amount = parse_money(result.get("others_amount"))
        snapshot.money_transfer = parse_money(result.get("money_transfer"))
        snapshot.synced_at = datetime.utcnow()
        db.session.add(snapshot)
        db.session.commit()
        flash("Ozon aylık finans özeti güncellendi.", "success")
        return redirect(url_for("ozon_sales", month=selected_month, store=store_key))

    @app.post("/ozon-satislari/<int:sale_id>/sil")
    def delete_ozon_sale(sale_id):
        record = db.get_or_404(OzonSale, sale_id)
        month = record.sale_date.strftime("%Y-%m")
        create_database_backup(app, "before_ozon_sale_delete")
        db.session.delete(record)
        db.session.commit()
        flash("Ozon satış kaydı silindi.", "success")
        return redirect(url_for("ozon_sales", month=month, store=record.store_key))

    @app.route("/masraflar", methods=["GET", "POST"])
    def expenses():
        if request.method == "POST":
            category = request.form.get("category", "")
            payment_method = request.form.get("payment_method", "")
            amount = parse_money(request.form.get("amount"))
            description = request.form.get("description", "").strip()
            if category not in EXPENSE_CATEGORIES:
                flash("Lütfen geçerli bir masraf kategorisi seçin.", "error")
            elif payment_method not in PAYMENT_METHODS:
                flash("Lütfen geçerli bir ödeme yöntemi seçin.", "error")
            elif not description:
                flash("Masraf açıklaması zorunludur.", "error")
            elif amount <= 0:
                flash("Masraf tutarı sıfırdan büyük olmalıdır.", "error")
            else:
                create_database_backup(app, "before_expense_add")
                expense = Expense(expense_date=parse_date(request.form.get("expense_date")) or date.today(), category=category, document_no=request.form.get("document_no", "").strip(), payee=request.form.get("payee", "").strip(), description=description, payment_method=payment_method, amount=amount)
                db.session.add(expense)
                db.session.commit()
                flash("Masraf kaydı oluşturuldu.", "success")
                return redirect(url_for("expenses"))
        query = request.args.get("q", "").strip()
        category = request.args.get("category", "").strip()
        start_date = parse_date(request.args.get("start_date"))
        end_date = parse_date(request.args.get("end_date"))
        records = Expense.query
        if query:
            pattern = f"%{normalize_search_text(query)}%"
            if db.engine.dialect.name == "sqlite":
                records = records.filter(db.or_(db.func.normalize_tr(Expense.description).like(pattern), db.func.normalize_tr(Expense.document_no).like(pattern), db.func.normalize_tr(Expense.payee).like(pattern)))
            else:
                records = records.filter(db.or_(Expense.description.ilike(f"%{query}%"), Expense.document_no.ilike(f"%{query}%"), Expense.payee.ilike(f"%{query}%")))
        if category in EXPENSE_CATEGORIES:
            records = records.filter(Expense.category == category)
        if start_date:
            records = records.filter(Expense.expense_date >= start_date)
        if end_date:
            records = records.filter(Expense.expense_date <= end_date)
        expense_records = records.order_by(Expense.expense_date.desc(), Expense.id.desc()).all()
        total = sum((expense.amount for expense in expense_records), Decimal("0"))
        today = date.today()
        recurring_expenses = RecurringExpense.query.order_by(RecurringExpense.active.desc(), RecurringExpense.payment_day, RecurringExpense.description).all()
        recurring_due = [{"expense": item, "due_date": item.due_date_for(today), "recorded": item.is_recorded_for(today)} for item in recurring_expenses]
        return render_template("expenses.html", expenses=expense_records, total=total, categories=EXPENSE_CATEGORIES, payment_methods=PAYMENT_METHODS, selected_category=category, query=query, start_date=request.args.get("start_date", ""), end_date=request.args.get("end_date", ""), today=today.isoformat(), current_date=today, recurring_due=recurring_due)

    @app.post("/masraflar/aylik-plan/ekle")
    def add_recurring_expense():
        category = request.form.get("category", "")
        payment_method = request.form.get("payment_method", "")
        description = request.form.get("description", "").strip()
        amount = parse_money(request.form.get("amount"))
        try:
            payment_day = int(request.form.get("payment_day", "0"))
        except ValueError:
            payment_day = 0
        if category not in EXPENSE_CATEGORIES or payment_method not in PAYMENT_METHODS or not description or amount <= 0 or not 1 <= payment_day <= 31:
            flash("Aylık ödeme planındaki zorunlu bilgileri kontrol edin.", "error")
        else:
            create_database_backup(app, "before_recurring_expense_add")
            db.session.add(RecurringExpense(description=description, category=category, payee=request.form.get("payee", "").strip(), payment_method=payment_method, amount=amount, payment_day=payment_day))
            db.session.commit()
            flash("Aylık ödeme planı eklendi.", "success")
        return redirect(url_for("expenses") + "#aylik-odemeler")

    @app.post("/masraflar/aylik-plan/<int:plan_id>/isle")
    def record_recurring_expense(plan_id):
        plan = db.get_or_404(RecurringExpense, plan_id)
        today = date.today()
        if plan.is_recorded_for(today):
            flash("Bu ödeme bu ay zaten masraf olarak işlendi.", "error")
        else:
            create_database_backup(app, "before_recurring_expense_record")
            db.session.add(Expense(expense_date=today, category=plan.category, payee=plan.payee, description=plan.description, payment_method=plan.payment_method, amount=plan.amount))
            plan.last_recorded_month = today.strftime("%Y-%m")
            db.session.commit()
            flash("Planlı ödeme bu ayın masraf kaydına aktarıldı.", "success")
        return redirect(url_for("expenses") + "#aylik-odemeler")

    @app.post("/masraflar/aylik-plan/<int:plan_id>/sil")
    def delete_recurring_expense(plan_id):
        plan = db.get_or_404(RecurringExpense, plan_id)
        create_database_backup(app, "before_recurring_expense_delete")
        db.session.delete(plan)
        db.session.commit()
        flash("Aylık ödeme planı silindi. Önceki masraf kayıtları korundu.", "success")
        return redirect(url_for("expenses") + "#aylik-odemeler")

    @app.route("/masraflar/<int:expense_id>/duzenle", methods=["GET", "POST"])
    def edit_expense(expense_id):
        expense = db.get_or_404(Expense, expense_id)
        if request.method == "POST":
            category = request.form.get("category", "")
            payment_method = request.form.get("payment_method", "")
            amount = parse_money(request.form.get("amount"))
            description = request.form.get("description", "").strip()
            if category not in EXPENSE_CATEGORIES or payment_method not in PAYMENT_METHODS or amount <= 0 or not description:
                flash("Lütfen zorunlu masraf bilgilerini kontrol edin.", "error")
            else:
                create_database_backup(app, "before_expense_edit")
                expense.expense_date = parse_date(request.form.get("expense_date")) or expense.expense_date
                expense.category = category
                expense.document_no = request.form.get("document_no", "").strip()
                expense.payee = request.form.get("payee", "").strip()
                expense.description = description
                expense.payment_method = payment_method
                expense.amount = amount
                db.session.commit()
                flash("Masraf kaydı güncellendi.", "success")
                return redirect(url_for("expenses"))
        return render_template("expense_edit.html", expense=expense, categories=EXPENSE_CATEGORIES, payment_methods=PAYMENT_METHODS)

    @app.post("/masraflar/<int:expense_id>/sil")
    def delete_expense(expense_id):
        expense = db.get_or_404(Expense, expense_id)
        create_database_backup(app, "before_expense_delete")
        db.session.delete(expense)
        db.session.commit()
        flash("Masraf kaydı silindi.", "success")
        return redirect(url_for("expenses"))

    @app.get("/siparisler")
    def orders():
        query = request.args.get("q", "").strip()
        selected_statuses = selected_order_statuses(request.args)
        selected_invoice_status = selected_order_invoice_status(request.args)
        order_type = request.args.get("type", "").strip()
        customer_id = request.args.get("customer_id", type=int)
        customer_query = request.args.get("customer_q", "").strip()
        active_only = request.args.get("active") == "1"
        delivery_pending = request.args.get("delivery_pending") == "1"
        selected_customer = db.session.get(Customer, customer_id) if customer_id else None
        status_summary = {
            kind: {order_status: {"count": 0, "total": Decimal("0")} for order_status in ORDER_STATUSES}
            for kind in ORDER_TYPES
        }
        summary_orders = Order.query.options(selectinload(Order.items)).all()
        for summary_order in summary_orders:
            if summary_order.order_type not in status_summary or summary_order.status not in status_summary[summary_order.order_type]:
                continue
            bucket = status_summary[summary_order.order_type][summary_order.status]
            bucket["count"] += 1
            bucket["total"] += summary_order.total_amount
        records = Order.query.options(
            selectinload(Order.items),
            joinedload(Order.customer),
            selectinload(Order.invoices),
        ).join(Customer)
        if query:
            search_value = f"%{normalize_search_text(query)}%"
            records = records.filter(db.or_(
                func.normalize_tr(Order.order_no).like(search_value),
                func.normalize_tr(Customer.name).like(search_value),
                func.normalize_tr(Customer.code).like(search_value),
            ))
        if selected_statuses:
            records = records.filter(Order.status.in_(selected_statuses))
        records = apply_order_invoice_status_filter(records, selected_invoice_status)
        if active_only:
            records = records.filter(~Order.status.in_(["Teslim Edildi", "İptal Edildi"]))
        if delivery_pending:
            records = records.filter(Order.status != "İptal Edildi")
        if order_type in ORDER_TYPES:
            records = records.filter(Order.order_type == order_type)
        if selected_customer:
            records = records.filter(Order.customer_id == selected_customer.id)
        if customer_query:
            customer_search_value = f"%{normalize_search_text(customer_query)}%"
            records = records.filter(db.or_(
                func.normalize_tr(Customer.name).like(customer_search_value),
                func.normalize_tr(Customer.code).like(customer_search_value),
            ))
        if delivery_pending:
            all_cashflow_orders = Order.query.options(selectinload(Order.items)).filter(Order.status != "İptal Edildi").all()
            pending_expected = calculate_pending_delivery_amounts(all_cashflow_orders)
            open_ids = {order.id for order in all_cashflow_orders if pending_expected.get(order.id, 0) > 0}
            records = records.filter(Order.id.in_(open_ids)) if open_ids else records.filter(db.false())
            counts = {kind: sum(1 for order in all_cashflow_orders if order.order_type == kind and order.id in open_ids) for kind in ORDER_TYPES}
        elif active_only:
            counts = {kind: sum(order.order_type == kind and order.status not in {"Teslim Edildi", "İptal Edildi"} for order in summary_orders) for kind in ORDER_TYPES}
        else:
            counts = {kind: sum(order.order_type == kind for order in summary_orders) for kind in ORDER_TYPES}
        listed_orders = records.order_by(Order.delivery_date.asc().nullslast(), Order.order_date.desc(), Order.id.desc()).all() if delivery_pending else records.order_by(Order.order_date.desc(), Order.id.desc()).all()
        listed_sales_ids = [order.id for order in listed_orders if order.order_type == "Satış"]
        linked_by_source = {order_id: [] for order_id in listed_sales_ids}
        if listed_sales_ids:
            for linked_order in Order.query.options(selectinload(Order.items)).filter(Order.source_order_id.in_(listed_sales_ids)).order_by(Order.id).all():
                linked_by_source[linked_order.source_order_id].append(linked_order)
        listed_purchase_source_ids = {
            order.source_order_id for order in listed_orders
            if order.order_type == "Satın Alma" and order.source_order_id
        }
        source_orders_by_id = {
            source_order.id: source_order
            for source_order in Order.query.options(selectinload(Order.items)).filter(Order.id.in_(listed_purchase_source_ids)).all()
        } if listed_purchase_source_ids else {}
        procurement_summaries = {
            order.id: procurement_summary(order, linked_by_source.get(order.id, []))
            for order in listed_orders if order.order_type == "Satış"
        }
        invoice_counts = dict(db.session.query(Invoice.order_id, func.count(Invoice.id)).filter(
            Invoice.order_id.in_([order.id for order in listed_orders])
        ).group_by(Invoice.order_id).all()) if listed_orders else {}
        pending_expected = pending_expected if delivery_pending else {}
        listed_total = sum((pending_expected.get(order.id, order.total_amount) for order in listed_orders), Decimal("0"))
        export_args = {}
        if query:
            export_args["q"] = query
        if selected_statuses:
            export_args["status"] = selected_statuses
        if selected_invoice_status:
            export_args["invoice_status"] = selected_invoice_status
        if order_type in ORDER_TYPES:
            export_args["type"] = order_type
        if selected_customer:
            export_args["customer_id"] = str(selected_customer.id)
        if customer_query:
            export_args["customer_q"] = customer_query
        if active_only:
            export_args["active"] = "1"
        if delivery_pending:
            export_args["delivery_pending"] = "1"
        customers_list = Customer.query.join(Order).distinct().order_by(Customer.name).all()
        return render_template("orders.html", orders=listed_orders, statuses=ORDER_STATUSES, query=query, customer_query=customer_query, selected_statuses=selected_statuses, selected_invoice_status=selected_invoice_status, selected_type=order_type, selected_customer=selected_customer, customers=customers_list, active_only=active_only, delivery_pending=delivery_pending, listed_total=listed_total, pending_expected=pending_expected, type_counts=counts, status_summary=status_summary, procurement_summaries=procurement_summaries, invoice_counts=invoice_counts, source_orders_by_id=source_orders_by_id, export_args=export_args)

    @app.get("/siparisler/excel")
    def export_orders_excel():
        """Export the visible order list, including status, to a single Excel file."""
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter

        query = request.args.get("q", "").strip()
        selected_statuses = selected_order_statuses(request.args)
        selected_invoice_status = selected_order_invoice_status(request.args)
        order_type = request.args.get("type", "").strip()
        customer_id = request.args.get("customer_id", type=int)
        customer_query = request.args.get("customer_q", "").strip()
        active_only = request.args.get("active") == "1"
        delivery_pending = request.args.get("delivery_pending") == "1"
        selected_customer = db.session.get(Customer, customer_id) if customer_id else None

        records = Order.query.join(Customer)
        if query:
            search_value = f"%{normalize_search_text(query)}%"
            records = records.filter(db.or_(
                func.normalize_tr(Order.order_no).like(search_value),
                func.normalize_tr(Customer.name).like(search_value),
                func.normalize_tr(Customer.code).like(search_value),
            ))
        if selected_statuses:
            records = records.filter(Order.status.in_(selected_statuses))
        records = apply_order_invoice_status_filter(records, selected_invoice_status)
        if active_only:
            records = records.filter(~Order.status.in_(["Teslim Edildi", "İptal Edildi"]))
        if delivery_pending:
            records = records.filter(Order.status != "İptal Edildi")
        if order_type in ORDER_TYPES:
            records = records.filter(Order.order_type == order_type)
        if selected_customer:
            records = records.filter(Order.customer_id == selected_customer.id)
        if customer_query:
            customer_search_value = f"%{normalize_search_text(customer_query)}%"
            records = records.filter(db.or_(
                func.normalize_tr(Customer.name).like(customer_search_value),
                func.normalize_tr(Customer.code).like(customer_search_value),
            ))

        pending_expected = {}
        if delivery_pending:
            all_cashflow_orders = Order.query.filter(Order.status != "İptal Edildi").all()
            pending_expected = calculate_pending_delivery_amounts(all_cashflow_orders)
            open_ids = {order.id for order in all_cashflow_orders if pending_expected.get(order.id, 0) > 0}
            records = records.filter(Order.id.in_(open_ids)) if open_ids else records.filter(db.false())

        listed_orders = records.order_by(Order.delivery_date.asc().nullslast(), Order.order_date.desc(), Order.id.desc()).all() if delivery_pending else records.order_by(Order.order_date.desc(), Order.id.desc()).all()
        sales_ids = [order.id for order in listed_orders if order.order_type == "Satış"]
        linked_by_source = {order_id: [] for order_id in sales_ids}
        if sales_ids:
            for linked_order in Order.query.filter(Order.source_order_id.in_(sales_ids)).order_by(Order.id).all():
                linked_by_source[linked_order.source_order_id].append(linked_order)
        procurement_summaries = {
            order.id: procurement_summary(order, linked_by_source.get(order.id, []))
            for order in listed_orders if order.order_type == "Satış"
        }

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Sipariş Listesi"
        sheet.sheet_view.showGridLines = False
        sheet.freeze_panes = "A5"
        navy, blue, pale, line = "172033", "2563EB", "EEF4FF", "D8E0EC"
        headers = [
            "Tür", "Sipariş No", "Cari", "Sipariş Tarihi", "Teslim Tarihi", "Gönderim İli",
            "Toplam Adet", "Ara Toplam", "KDV", "Genel Toplam", "Durum", "Tedarik Durumu",
        ]
        sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
        sheet["A1"] = "BUSINESS OS - SİPARİŞ LİSTESİ"
        sheet["A1"].font = Font(name="Arial", size=18, bold=True, color="FFFFFF")
        sheet["A1"].fill = PatternFill("solid", fgColor=navy)
        sheet["A1"].alignment = Alignment(vertical="center")
        sheet.row_dimensions[1].height = 32
        selected_filters = []
        if order_type in ORDER_TYPES:
            selected_filters.append(order_type)
        if selected_statuses:
            selected_filters.append("Durum: " + ", ".join(selected_statuses))
        if selected_invoice_status:
            selected_filters.append("Fatura: " + selected_invoice_status)
        if active_only:
            selected_filters.append("Devam edenler")
        if delivery_pending:
            selected_filters.append("Açık tahsilat / ödeme")
        if query:
            selected_filters.append(f'Arama: {query}')
        if selected_customer:
            selected_filters.append(f"Cari: {selected_customer.name}")
        elif customer_query:
            selected_filters.append(f"Cari: {customer_query}")
        sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(headers))
        sheet["A2"] = f"Oluşturulma: {datetime.now().strftime('%d.%m.%Y %H:%M')} | " + (" · ".join(selected_filters) if selected_filters else "Tüm siparişler")
        sheet["A2"].font = Font(name="Arial", size=9, color="64748B")

        header_row = 4
        thin = Side(style="thin", color=line)
        for column, header in enumerate(headers, 1):
            cell = sheet.cell(header_row, column, header)
            cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=blue)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = Border(bottom=thin)
        sheet.row_dimensions[header_row].height = 28

        for row_no, order in enumerate(listed_orders, header_row + 1):
            procurement = procurement_summaries.get(order.id)
            procurement_label = procurement["label"] if procurement else "—"
            values = [
                order.order_type,
                order.order_no,
                order.customer.name,
                order.order_date,
                order.delivery_date,
                order.delivery_city or "",
                order.total_quantity,
                float(order.net_amount),
                float(order.vat_amount),
                float(order.total_amount),
                order.status,
                procurement_label,
            ]
            for column, value in enumerate(values, 1):
                cell = sheet.cell(row_no, column, value)
                cell.font = Font(name="Arial", size=10)
                cell.alignment = Alignment(vertical="center", wrap_text=column in (3, 6, 12))
                cell.border = Border(bottom=thin)
            for column in (4, 5):
                sheet.cell(row_no, column).number_format = "dd.mm.yyyy"
            for column in (8, 9, 10):
                sheet.cell(row_no, column).number_format = '₺#,##0.00'
            sheet.row_dimensions[row_no].height = 24

        total_row = header_row + len(listed_orders) + 1
        sheet.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=7)
        sheet.cell(total_row, 1, f"TOPLAM ({len(listed_orders)} sipariş)")
        for column, label in ((8, "Ara Toplam"), (9, "KDV"), (10, "Genel Toplam")):
            sheet.cell(total_row, column, label)
            sheet.cell(total_row, column).font = Font(name="Arial", size=10, bold=True, color=navy)
        first_data_row = header_row + 1
        last_data_row = total_row - 1
        if listed_orders:
            for column in (8, 9, 10):
                letter = get_column_letter(column)
                sheet.cell(total_row + 1, column, f"=SUM({letter}{first_data_row}:{letter}{last_data_row})")
                sheet.cell(total_row + 1, column).number_format = '₺#,##0.00'
        else:
            for column in (8, 9, 10):
                sheet.cell(total_row + 1, column, 0)
                sheet.cell(total_row + 1, column).number_format = '₺#,##0.00'
        for column in range(1, len(headers) + 1):
            sheet.cell(total_row, column).fill = PatternFill("solid", fgColor=pale)
            sheet.cell(total_row + 1, column).fill = PatternFill("solid", fgColor=pale)
            sheet.cell(total_row, column).border = Border(top=thin)
            sheet.cell(total_row + 1, column).border = Border(bottom=thin)
        sheet.cell(total_row + 1, 1, "TUTARLAR")
        sheet.cell(total_row + 1, 1).font = Font(name="Arial", size=10, bold=True, color=navy)

        widths = [14, 18, 38, 15, 15, 18, 13, 17, 15, 18, 19, 28]
        for column, width in enumerate(widths, 1):
            sheet.column_dimensions[get_column_letter(column)].width = width
        sheet.auto_filter.ref = f"A{header_row}:L{max(header_row, total_row - 1)}"
        sheet.print_title_rows = f"1:{header_row}"
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.fitToWidth = 1
        sheet.sheet_properties.pageSetUpPr.fitToPage = True

        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        return send_file(
            output,
            as_attachment=True,
            download_name=f"Business-OS-Siparis-Listesi-{date.today().isoformat()}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.get("/siparisler/excel/ayrintili")
    def export_orders_detailed_excel():
        """Export each filtered order line as a separate row in an Excel file."""
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter

        query = request.args.get("q", "").strip()
        selected_statuses = selected_order_statuses(request.args)
        selected_invoice_status = selected_order_invoice_status(request.args)
        order_type = request.args.get("type", "").strip()
        customer_id = request.args.get("customer_id", type=int)
        customer_query = request.args.get("customer_q", "").strip()
        active_only = request.args.get("active") == "1"
        delivery_pending = request.args.get("delivery_pending") == "1"
        selected_customer = db.session.get(Customer, customer_id) if customer_id else None

        records = Order.query.join(Customer)
        if query:
            search_value = f"%{normalize_search_text(query)}%"
            records = records.filter(db.or_(
                func.normalize_tr(Order.order_no).like(search_value),
                func.normalize_tr(Customer.name).like(search_value),
                func.normalize_tr(Customer.code).like(search_value),
            ))
        if selected_statuses:
            records = records.filter(Order.status.in_(selected_statuses))
        records = apply_order_invoice_status_filter(records, selected_invoice_status)
        if active_only:
            records = records.filter(~Order.status.in_(["Teslim Edildi", "İptal Edildi"]))
        if delivery_pending:
            records = records.filter(Order.status != "İptal Edildi")
        if order_type in ORDER_TYPES:
            records = records.filter(Order.order_type == order_type)
        if selected_customer:
            records = records.filter(Order.customer_id == selected_customer.id)
        if customer_query:
            customer_search_value = f"%{normalize_search_text(customer_query)}%"
            records = records.filter(db.or_(
                func.normalize_tr(Customer.name).like(customer_search_value),
                func.normalize_tr(Customer.code).like(customer_search_value),
            ))
        if delivery_pending:
            all_cashflow_orders = Order.query.filter(Order.status != "İptal Edildi").all()
            pending_expected = calculate_pending_delivery_amounts(all_cashflow_orders)
            open_ids = {order.id for order in all_cashflow_orders if pending_expected.get(order.id, 0) > 0}
            records = records.filter(Order.id.in_(open_ids)) if open_ids else records.filter(db.false())

        listed_orders = records.order_by(Order.delivery_date.asc().nullslast(), Order.order_date.desc(), Order.id.desc()).all() if delivery_pending else records.order_by(Order.order_date.desc(), Order.id.desc()).all()
        sales_ids = [order.id for order in listed_orders if order.order_type == "Satış"]
        linked_by_source = {order_id: [] for order_id in sales_ids}
        if sales_ids:
            for linked_order in Order.query.filter(Order.source_order_id.in_(sales_ids)).order_by(Order.id).all():
                linked_by_source[linked_order.source_order_id].append(linked_order)
        procurement_summaries = {
            order.id: procurement_summary(order, linked_by_source.get(order.id, []))
            for order in listed_orders if order.order_type == "Satış"
        }

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Sipariş Ayrıntıları"
        sheet.sheet_view.showGridLines = False
        navy, blue, pale, line = "172033", "2563EB", "EEF4FF", "D8E0EC"
        headers = [
            "Tür", "Sipariş No", "Cari", "Sipariş Tarihi", "Teslim Tarihi", "Gönderim İli", "Durum", "Tedarik Durumu",
            "Ürün", "Ayrıntı 1", "Ayrıntı 2", "Ayrıntı 3", "Adet", "Birim", "Birim Fiyat", "İskonto %", "İskonto Tutarı",
            "Ara Toplam", "KDV %", "KDV Tutarı", "KDV Dahil Satır Toplamı", "Kalem Notu", "Sipariş Notu",
        ]
        last_column = len(headers)
        sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_column)
        sheet["A1"] = "BUSINESS OS - AYRINTILI SİPARİŞ RAPORU"
        sheet["A1"].font = Font(name="Arial", size=18, bold=True, color="FFFFFF")
        sheet["A1"].fill = PatternFill("solid", fgColor=navy)
        sheet["A1"].alignment = Alignment(vertical="center")
        sheet.row_dimensions[1].height = 32
        selected_filters = []
        if order_type in ORDER_TYPES:
            selected_filters.append(order_type)
        if selected_statuses:
            selected_filters.append("Durum: " + ", ".join(selected_statuses))
        if active_only:
            selected_filters.append("Devam edenler")
        if delivery_pending:
            selected_filters.append("Açık tahsilat / ödeme")
        if query:
            selected_filters.append(f"Arama: {query}")
        if selected_customer:
            selected_filters.append(f"Cari: {selected_customer.name}")
        elif customer_query:
            selected_filters.append(f"Cari: {customer_query}")
        sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_column)
        sheet["A2"] = f"Oluşturulma: {datetime.now().strftime('%d.%m.%Y %H:%M')} | " + (" · ".join(selected_filters) if selected_filters else "Tüm siparişler")
        sheet["A2"].font = Font(name="Arial", size=9, color="64748B")
        header_row = 4
        thin = Side(style="thin", color=line)
        for column, header in enumerate(headers, 1):
            cell = sheet.cell(header_row, column, header)
            cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=blue)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = Border(bottom=thin)
        sheet.row_dimensions[header_row].height = 34

        row_no = header_row + 1
        for order in listed_orders:
            procurement = procurement_summaries.get(order.id)
            procurement_label = procurement["label"] if procurement else "—"
            for item in order.items:
                gross_amount = (item.unit_price or Decimal("0")) * item.quantity
                discount_amount = gross_amount * (item.discount_rate or Decimal("0")) / Decimal("100")
                values = [
                    order.order_type, order.order_no, order.customer.name, order.order_date, order.delivery_date, order.delivery_city or "", order.status, procurement_label,
                    item.product_name, item.variant or "", item.detail_2 or "", item.detail_3 or "", item.quantity, item.unit, float(item.unit_price or 0),
                    float(item.discount_rate or 0) / 100, float(discount_amount), float(item.net_amount), float(item.vat_rate or 0) / 100,
                    float(item.vat_amount), float(item.total_amount), item.note or "", order.notes or "",
                ]
                for column, value in enumerate(values, 1):
                    cell = sheet.cell(row_no, column, value)
                    cell.font = Font(name="Arial", size=10)
                    cell.alignment = Alignment(vertical="top", wrap_text=column in (3, 8, 9, 10, 11, 12, 22, 23))
                    cell.border = Border(bottom=thin)
                for column in (4, 5):
                    sheet.cell(row_no, column).number_format = "dd.mm.yyyy"
                for column in (15, 17, 18, 20, 21):
                    sheet.cell(row_no, column).number_format = '₺#,##0.00'
                for column in (16, 19):
                    sheet.cell(row_no, column).number_format = "0.00%"
                sheet.row_dimensions[row_no].height = 26
                row_no += 1

        total_row = row_no + 1
        data_start, data_end = header_row + 1, row_no - 1
        sheet.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=17)
        sheet.cell(total_row, 1, f"TOPLAM ({len(listed_orders)} sipariş / {max(0, data_end - data_start + 1)} kalem)")
        for column, label in ((18, "Ara Toplam"), (20, "Toplam KDV"), (21, "Genel Toplam")):
            sheet.cell(total_row, column, label)
        formula_row = total_row + 1
        for column in (18, 20, 21):
            letter = get_column_letter(column)
            sheet.cell(formula_row, column, f"=SUM({letter}{data_start}:{letter}{data_end})" if data_end >= data_start else 0)
            sheet.cell(formula_row, column).number_format = '₺#,##0.00'
        sheet.cell(formula_row, 1, "TUTARLAR")
        for current_row in (total_row, formula_row):
            for column in range(1, last_column + 1):
                sheet.cell(current_row, column).fill = PatternFill("solid", fgColor=pale)
                sheet.cell(current_row, column).border = Border(top=thin if current_row == total_row else None, bottom=thin if current_row == formula_row else None)
            sheet.cell(current_row, 1).font = Font(name="Arial", size=10, bold=True, color=navy)
        for column in (18, 20, 21):
            sheet.cell(total_row, column).font = Font(name="Arial", size=10, bold=True, color=navy)
            sheet.cell(formula_row, column).font = Font(name="Arial", size=10, bold=True, color=blue)

        widths = [14, 18, 34, 15, 15, 17, 18, 27, 32, 19, 19, 19, 10, 10, 15, 13, 17, 17, 11, 15, 22, 28, 32]
        for column, width in enumerate(widths, 1):
            sheet.column_dimensions[get_column_letter(column)].width = width
        sheet.freeze_panes = "A5"
        sheet.auto_filter.ref = f"A{header_row}:{get_column_letter(last_column)}{max(header_row, data_end)}"
        sheet.print_title_rows = f"1:{header_row}"
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.fitToWidth = 1
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        return send_file(
            output,
            as_attachment=True,
            download_name=f"Business-OS-Ayrintili-Siparis-Raporu-{date.today().isoformat()}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/siparisler/yeni", methods=["GET", "POST"])
    def new_order():
        customers_list = Customer.query.order_by(Customer.name).all()
        products_list = Product.query.filter_by(active=True).order_by(Product.name).all()
        if request.method == "POST":
            customer_id = request.form.get("customer_id", type=int)
            order_type = request.form.get("order_type", "Satış")
            names = request.form.getlist("product_name[]")
            quantities = request.form.getlist("quantity[]")
            if order_type not in ORDER_TYPES:
                flash("Lütfen geçerli bir sipariş türü seçin.", "error")
            elif not customer_id or not db.session.get(Customer, customer_id):
                flash("Lütfen bir müşteri seçin.", "error")
            elif order_type == "Satış" and request.form.get("payment_method", "") not in ORDER_PAYMENT_METHODS:
                flash("Lütfen satış siparişi için ödeme yöntemini seçin.", "error")
            elif not any(name.strip() for name in names):
                flash("En az bir sipariş kalemi ekleyin.", "error")
            else:
                order = Order(order_no=next_order_no(order_type), order_type=order_type, customer_id=customer_id, order_date=parse_date(request.form.get("order_date")) or date.today(), delivery_date=parse_date(request.form.get("delivery_date")), delivery_city=request.form.get("delivery_city", "").strip() if order_type == "Satın Alma" else None, shipment_contact=request.form.get("shipment_contact", "").strip() if order_type == "Satın Alma" else None, shipment_phone=request.form.get("shipment_phone", "").strip() if order_type == "Satın Alma" else None, shipment_address=request.form.get("shipment_address", "").strip() if order_type == "Satın Alma" else None, shipment_note=request.form.get("shipment_note", "").strip() if order_type == "Satın Alma" else None, customer_company=request.form.get("customer_company", "").strip() if order_type == "Satın Alma" else None, payment_method=request.form.get("payment_method", "") if order_type == "Satış" else None, notes=request.form.get("notes"), status="Bekliyor")
                db.session.add(order)
                product_ids = request.form.getlist("product_id[]")
                variants = request.form.getlist("variant[]")
                details_2 = request.form.getlist("detail_2[]")
                details_3 = request.form.getlist("detail_3[]")
                units = request.form.getlist("unit[]")
                prices = request.form.getlist("unit_price[]")
                discount_rates = request.form.getlist("discount_rate[]")
                vat_rates = request.form.getlist("vat_rate[]")
                vat_included_values = request.form.getlist("vat_included[]")
                item_notes = request.form.getlist("item_note[]")
                for i, name in enumerate(names):
                    if not name.strip():
                        continue
                    quantity = int(quantities[i]) if i < len(quantities) and quantities[i].isdigit() else 1
                    vat_rate = parse_money(vat_rates[i] if i < len(vat_rates) else "10")
                    discount_rate = parse_money(discount_rates[i] if i < len(discount_rates) else "0")
                    product_id = int(product_ids[i]) if i < len(product_ids) and product_ids[i].isdigit() else None
                    order.items.append(OrderItem(product_id=product_id, product_name=name.strip(), description="", variant=variants[i] if i < len(variants) else "", detail_2=details_2[i] if i < len(details_2) else "", detail_3=details_3[i] if i < len(details_3) else "", quantity=max(quantity, 1), unit=units[i] if i < len(units) and units[i] else "Adet", unit_price=parse_money(prices[i] if i < len(prices) else "0"), discount_rate=max(Decimal("0"), min(discount_rate, Decimal("100"))), cost_unit_price=Decimal("0"), vat_rate=max(Decimal("0"), min(vat_rate, Decimal("100"))), vat_included=(vat_included_values[i] if i < len(vat_included_values) else "0") == "1", note=item_notes[i] if i < len(item_notes) else ""))
                order.history.append(OrderHistory(status="Bekliyor", note="Sipariş oluşturuldu"))
                db.session.commit()
                flash(f"{order.order_no} numaralı sipariş oluşturuldu.", "success")
                return redirect(url_for("order_detail", order_id=order.id))
        return render_template("order_form.html", customers=customers_list, products=products_list, order_types=ORDER_TYPES, order_payment_methods=ORDER_PAYMENT_METHODS, selected_type=request.args.get("type", "Satış"), today=date.today().isoformat())

    @app.get("/siparisler/<int:order_id>")
    def order_detail(order_id):
        order = db.get_or_404(Order, order_id)
        converted_orders = Order.query.filter_by(source_order_id=order.id).order_by(Order.id).all() if order.order_type == "Satış" else []
        procurement = procurement_summary(order, converted_orders) if order.order_type == "Satış" else None
        source_order = db.session.get(Order, order.source_order_id) if order.order_type == "Satın Alma" and order.source_order_id else None
        collection_item = next((item for item in delivered_sales_collection_tracking() if item["order"].id == order.id), None) if order.order_type == "Satış" and order.status == "Teslim Edildi" else None
        return render_template(
            "order_detail.html",
            order=order,
            converted_orders=converted_orders,
            procurement=procurement,
            source_order=source_order,
            collection_item=collection_item,
            statuses=ORDER_STATUSES,
            order_document_types=ORDER_DOCUMENT_TYPES,
            default_document_type=order_document_type_for(order),
        )

    @app.post("/siparisler/<int:order_id>/belgeler")
    def upload_order_documents(order_id):
        order = db.get_or_404(Order, order_id)
        uploads = [item for item in request.files.getlist("files") if item and item.filename]
        if not uploads:
            flash("Eklenecek dosya seçin.", "error")
            return redirect(url_for("order_detail", order_id=order.id))
        saved = []
        try:
            create_database_backup(app, "before_order_document_upload")
            for upload in uploads:
                saved.append(store_order_document(app, order, upload, request.form.get("document_type"), "Yapıştırıldı" if request.form.get("source") == "paste" else "Manuel"))
            order.history.append(OrderHistory(status=order.status, note=f"{len(saved)} belge eklendi"))
            db.session.commit()
            flash(f"{len(saved)} belge siparişe eklendi.", "success")
        except (ValueError, OSError) as error:
            db.session.rollback()
            for document in saved:
                order_document_path(app, document).unlink(missing_ok=True)
            flash(str(error), "error")
        return redirect(url_for("order_detail", order_id=order.id))

    @app.get("/siparis-belgeleri/<int:document_id>/indir")
    def download_order_document(document_id):
        document = db.get_or_404(OrderDocument, document_id)
        try:
            path = order_document_path(app, document)
        except ValueError:
            abort(404)
        return stored_file_response(app, path, document.original_name, document.mime_type, True)

    @app.get("/siparis-belgeleri/<int:document_id>/ac")
    def view_order_document(document_id):
        document = db.get_or_404(OrderDocument, document_id)
        try:
            path = order_document_path(app, document)
        except ValueError:
            abort(404)
        return stored_file_response(app, path, document.original_name, document.mime_type, False)

    @app.post("/siparis-belgeleri/<int:document_id>/sil")
    def delete_order_document(document_id):
        document = db.get_or_404(OrderDocument, document_id)
        order_id = document.order_id
        try:
            path = order_document_path(app, document)
        except ValueError:
            path = None
        create_database_backup(app, "before_order_document_delete")
        if path:
            delete_persistent_file(app, path)
        db.session.delete(document)
        db.session.commit()
        flash("Belge siparişten kaldırıldı.", "success")
        return redirect(url_for("order_detail", order_id=order_id))

    @app.get("/siparisler/<int:order_id>/excel")
    def export_order_excel(order_id):
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

        order = db.get_or_404(Order, order_id)
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Sipariş"
        sheet.sheet_view.showGridLines = False
        sheet.freeze_panes = "A9"
        blue, navy, pale, line = "2563EB", "172033", "EEF4FF", "D8E0EC"
        sheet.merge_cells("A1:M1")
        sheet["A1"] = f"BUSINESS OS - {order.order_type.upper()} SİPARİŞİ"
        sheet["A1"].font = Font(name="Arial", size=18, bold=True, color="FFFFFF")
        sheet["A1"].fill = PatternFill("solid", fgColor=navy)
        sheet["A1"].alignment = Alignment(vertical="center")
        sheet.row_dimensions[1].height = 34
        info = [
            ("Sipariş No", order.order_no, "Cari", order.customer.name),
            ("Sipariş Tarihi", order.order_date, "Teslim Tarihi", order.delivery_date or "Belirtilmedi"),
            ("Durum", order.status, "Tür", order.order_type),
        ]
        if order.order_type == "Satın Alma":
            info.append(("Gönderim İli", order.delivery_city or "Belirtilmedi", "", ""))
            if any([order.shipment_contact, order.shipment_phone, order.shipment_address, order.shipment_note]):
                info.append(("Sevkiyat Yetkilisi", order.shipment_contact or "", "Sevkiyat Telefonu", order.shipment_phone or ""))
                info.append(("Sevkiyat Adresi", order.shipment_address or "", "Sevkiyat Notu", order.shipment_note or ""))
        elif order.payment_method:
            info.append(("Ödeme Yöntemi", order.payment_method, "", ""))
        for row_no, values in enumerate(info, 3):
            sheet.cell(row_no, 1, values[0]); sheet.cell(row_no, 2, values[1])
            sheet.cell(row_no, 6, values[2]); sheet.cell(row_no, 7, values[3])
            sheet.merge_cells(start_row=row_no, start_column=2, end_row=row_no, end_column=5)
            sheet.merge_cells(start_row=row_no, start_column=7, end_row=row_no, end_column=13)
            for col in (1, 6):
                sheet.cell(row_no, col).font = Font(name="Arial", bold=True, color="64748B")
            for col in (2, 7):
                sheet.cell(row_no, col).font = Font(name="Arial", bold=True, color=navy)
                sheet.cell(row_no, col).data_type = "s" if isinstance(sheet.cell(row_no, col).value, str) else sheet.cell(row_no, col).data_type
                sheet.cell(row_no, col).alignment = Alignment(wrap_text=True, vertical="top")
            sheet.row_dimensions[row_no].height = max(30, 15 * max((len(str(value)) // 40 + str(value).count('\n') + 1) for value in (values[1], values[3])))
        for cell_ref in ("B4", "G4"):
            if hasattr(sheet[cell_ref].value, "year"):
                sheet[cell_ref].number_format = "dd.mm.yyyy"
        headers = [
            "Sıra", "Ürün", "Ayrıntı 1", "Ayrıntı 2", "Ayrıntı 3", "Adet", "Birim",
            "Liste Birim Fiyat", "İskonto %", "İskonto Sonrası Tutar", "KDV %", "KDV Tutarı", "KDV Dahil Toplam",
        ]
        header_row = max(8, len(info) + 4)
        sheet.freeze_panes = f"A{header_row + 1}"
        for column, heading in enumerate(headers, 1):
            cell = sheet.cell(header_row, column, heading)
            cell.font = Font(name="Arial", bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=blue)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        sheet.row_dimensions[header_row].height = 30
        thin = Side(style="thin", color=line)
        first_data_row = header_row + 1
        for index, item in enumerate(order.items, 1):
            row_no = header_row + index
            values = [
                index, item.product_name, item.variant or "", item.detail_2 or "", item.detail_3 or "", item.quantity,
                item.unit, float(item.unit_price or 0), float(item.discount_rate or 0) / 100, None, float(item.vat_rate or 0) / 100,
            ]
            for column, value in enumerate(values, 1):
                cell = sheet.cell(row_no, column, value)
                cell.font = Font(name="Arial", size=10)
                cell.alignment = Alignment(vertical="top", wrap_text=column in (2, 3, 4, 5))
                cell.border = Border(bottom=thin)
            sheet.cell(row_no, 10, f"=F{row_no}*H{row_no}*(1-I{row_no})")
            sheet.cell(row_no, 12, f"=J{row_no}*K{row_no}")
            sheet.cell(row_no, 13, f"=J{row_no}+L{row_no}")
            for column in (8, 10, 12, 13):
                sheet.cell(row_no, column).number_format = '₺#,##0.00'
            for column in (9, 11):
                sheet.cell(row_no, column).number_format = "0.00%"
            sheet.row_dimensions[row_no].height = 30
        last_data_row = header_row + len(order.items)
        total_row = last_data_row + 2
        sheet.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=7)
        total_lines = [
            ("Liste Fiyatı Toplamı", f"=SUMPRODUCT(F{first_data_row}:F{last_data_row},H{first_data_row}:H{last_data_row})"),
            ("Toplam İskonto", f"=SUMPRODUCT(F{first_data_row}:F{last_data_row},H{first_data_row}:H{last_data_row},I{first_data_row}:I{last_data_row})"),
            ("İskonto Sonrası Ara Toplam", f"=SUM(J{first_data_row}:J{last_data_row})"),
            ("Toplam KDV", f"=SUM(L{first_data_row}:L{last_data_row})"),
            ("KDV Dahil Genel Toplam", f"=SUM(M{first_data_row}:M{last_data_row})"),
        ]
        for offset, (label, formula) in enumerate(total_lines):
            row_no = total_row + offset
            sheet.merge_cells(start_row=row_no, start_column=8, end_row=row_no, end_column=12)
            sheet.cell(row_no, 8, label)
            sheet.cell(row_no, 13, formula)
            sheet.cell(row_no, 8).font = Font(name="Arial", bold=True, color=navy)
            sheet.cell(row_no, 13).font = Font(name="Arial", bold=True, color=blue, size=12 if offset == len(total_lines) - 1 else 10)
            sheet.cell(row_no, 13).number_format = '₺#,##0.00'
            sheet.cell(row_no, 8).fill = sheet.cell(row_no, 13).fill = PatternFill("solid", fgColor=pale)
        if order.notes:
            note_row = total_row + len(total_lines) + 1
            sheet.merge_cells(start_row=note_row, start_column=1, end_row=note_row, end_column=13)
            sheet.cell(note_row, 1, f"Sipariş Notu: {order.notes}")
            sheet.cell(note_row, 1).alignment = Alignment(wrap_text=True, vertical="top")
            sheet.cell(note_row, 1).fill = PatternFill("solid", fgColor="FFF8E8")
            sheet.row_dimensions[note_row].height = 36
        widths = [7, 30, 18, 18, 18, 10, 10, 16, 11, 19, 10, 15, 18]
        for column, width in enumerate(widths, 1):
            sheet.column_dimensions[chr(64 + column)].width = width
        sheet.auto_filter.ref = f"A{header_row}:M{last_data_row}"
        sheet.print_title_rows = f"1:{header_row}"
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.fitToWidth = 1
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name=f"{order.order_no}.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/siparisler/<int:order_id>/pdf")
    def export_order_pdf(order_id):
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT, TA_RIGHT
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        order = db.get_or_404(Order, order_id)
        regular_font = "/System/Library/Fonts/Supplemental/Arial.ttf"
        bold_font = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        if os.path.exists(regular_font) and os.path.exists(bold_font):
            pdfmetrics.registerFont(TTFont("BusinessArial", regular_font))
            pdfmetrics.registerFont(TTFont("BusinessArialBold", bold_font))
            font_name, bold_name = "BusinessArial", "BusinessArialBold"
        else:
            font_name, bold_name = "Helvetica", "Helvetica-Bold"
        output = BytesIO()
        document = SimpleDocTemplate(output, pagesize=landscape(A4), rightMargin=12*mm, leftMargin=12*mm, topMargin=12*mm, bottomMargin=12*mm, title=order.order_no)
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle("TitleTR", parent=styles["Title"], fontName=bold_name, fontSize=17, leading=20, textColor=colors.HexColor("#172033"), alignment=TA_LEFT)
        body_style = ParagraphStyle("BodyTR", parent=styles["BodyText"], fontName=font_name, fontSize=7.5, leading=9)
        body_bold = ParagraphStyle("BodyBoldTR", parent=body_style, fontName=bold_name)
        header_style = ParagraphStyle("HeaderTR", parent=body_bold, textColor=colors.white, alignment=TA_LEFT)
        right_style = ParagraphStyle("RightTR", parent=body_style, alignment=TA_RIGHT)
        story = [Paragraph(f"BUSINESS OS - {order.order_type.upper()} SİPARİŞİ", title_style), Spacer(1, 4*mm)]
        info_data = [[Paragraph("Sipariş No", body_bold), Paragraph(order.order_no, body_style), Paragraph("Cari", body_bold), Paragraph(order.customer.name, body_style)], [Paragraph("Sipariş Tarihi", body_bold), Paragraph(order.order_date.strftime("%d.%m.%Y"), body_style), Paragraph("Teslim Tarihi", body_bold), Paragraph(order.delivery_date.strftime("%d.%m.%Y") if order.delivery_date else "Belirtilmedi", body_style)], [Paragraph("Durum", body_bold), Paragraph(order.status, body_style), Paragraph("Tür", body_bold), Paragraph(order.order_type, body_style)]]
        if order.order_type == "Satış" and order.payment_method:
            info_data.append([Paragraph("Ödeme Yöntemi", body_bold), Paragraph(order.payment_method, body_style), Paragraph("", body_bold), Paragraph("", body_style)])
        if order.order_type == "Satın Alma":
            info_data.append([Paragraph("Gönderim İli", body_bold), Paragraph(order.delivery_city or "Belirtilmedi", body_style), Paragraph("", body_bold), Paragraph("", body_style)])
            if any([order.shipment_contact, order.shipment_phone, order.shipment_address, order.shipment_note]):
                contact = " · ".join(item for item in [order.shipment_contact, order.shipment_phone] if item) or "Belirtilmedi"
                info_data.append([Paragraph("Sevkiyat Yetkilisi", body_bold), Paragraph(xml_escape(contact), body_style), Paragraph("Sevkiyat Adresi", body_bold), Paragraph(xml_escape(order.shipment_address or "Belirtilmedi").replace('\n', '<br/>'), body_style)])
                if order.shipment_note:
                    info_data.append([Paragraph("Sevkiyat Notu", body_bold), Paragraph(xml_escape(order.shipment_note).replace('\n', '<br/>'), body_style), Paragraph("", body_bold), Paragraph("", body_style)])
        info_table = Table(info_data, colWidths=[28*mm, 70*mm, 28*mm, 125*mm])
        info_table.setStyle(TableStyle([("BACKGROUND",(0,0),(0,-1),colors.HexColor("#EEF4FF")),("BACKGROUND",(2,0),(2,-1),colors.HexColor("#EEF4FF")),("FONTNAME",(0,0),(-1,-1),font_name),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#D8E0EC")),("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]))
        story.extend([info_table, Spacer(1, 5*mm)])
        headers = [
            "Sıra", "Ürün", "Ayrıntı 1", "Ayrıntı 2", "Ayrıntı 3", "Adet", "Birim",
            "Liste Fiyat", "İskonto %", "İskonto Sonrası", "KDV %", "KDV", "Toplam",
        ]
        data = [[Paragraph(value, header_style) for value in headers]]
        money_text = lambda value: f"TL {Decimal(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        percentage_text = lambda value: f"%{Decimal(value or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        for index, item in enumerate(order.items, 1):
            data.append([
                Paragraph(str(index), body_style), Paragraph(item.product_name, body_style), Paragraph(item.variant or "-", body_style),
                Paragraph(item.detail_2 or "-", body_style), Paragraph(item.detail_3 or "-", body_style), Paragraph(str(item.quantity), right_style),
                Paragraph(item.unit, body_style), Paragraph(money_text(item.unit_price or 0), right_style),
                Paragraph(percentage_text(item.discount_rate), right_style), Paragraph(money_text(item.net_amount), right_style),
                Paragraph(percentage_text(item.vat_rate), right_style), Paragraph(money_text(item.vat_amount), right_style), Paragraph(money_text(item.total_amount), right_style),
            ])
        item_table = Table(data, repeatRows=1, colWidths=[9*mm, 33*mm, 19*mm, 19*mm, 20*mm, 10*mm, 13*mm, 20*mm, 14*mm, 22*mm, 13*mm, 19*mm, 22*mm])
        item_table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#2563EB")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("FONTNAME",(0,0),(-1,-1),font_name),("VALIGN",(0,0),(-1,-1),"TOP"),("LINEBELOW",(0,0),(-1,-1),0.35,colors.HexColor("#D8E0EC")),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F8FAFC")]),("LEFTPADDING",(0,0),(-1,-1),4),("RIGHTPADDING",(0,0),(-1,-1),4),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]))
        story.extend([item_table, Spacer(1, 4*mm)])
        list_total = sum(((item.unit_price or Decimal("0")) * item.quantity for item in order.items), Decimal("0"))
        discount_total = list_total - order.net_amount
        totals = [
            [Paragraph("Liste Fiyatı Toplamı", body_bold), Paragraph(money_text(list_total), right_style)],
            [Paragraph("Toplam İskonto", body_bold), Paragraph(money_text(discount_total), right_style)],
            [Paragraph("İskonto Sonrası Ara Toplam", body_bold), Paragraph(money_text(order.net_amount), right_style)],
            [Paragraph("Toplam KDV", body_bold), Paragraph(money_text(order.vat_amount), right_style)],
            [Paragraph("KDV Dahil Genel Toplam", body_bold), Paragraph(money_text(order.total_amount), right_style)],
        ]
        totals_table = Table(totals, colWidths=[55*mm, 38*mm], hAlign="RIGHT")
        totals_table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#EEF4FF")),("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#D8E0EC")),("FONTNAME",(0,0),(-1,-1),font_name),("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]))
        story.append(totals_table)
        if order.notes:
            story.extend([Spacer(1, 4*mm), Paragraph(f"Sipariş Notu: {order.notes}", body_bold)])
        def page_number(canvas, doc):
            canvas.saveState(); canvas.setFont(font_name, 7); canvas.setFillColor(colors.HexColor("#64748B")); canvas.drawRightString(landscape(A4)[0]-12*mm, 7*mm, f"Sayfa {doc.page}"); canvas.restoreState()
        document.build(story, onFirstPage=page_number, onLaterPages=page_number)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name=f"{order.order_no}-{order.order_date.isoformat()}.pdf", mimetype="application/pdf")

    @app.route("/siparisler/<int:order_id>/duzenle", methods=["GET", "POST"])
    def edit_order(order_id):
        order = db.get_or_404(Order, order_id)
        customers_list = Customer.query.order_by(Customer.name).all()
        products_list = Product.query.filter_by(active=True).order_by(Product.name).all()
        sales_orders = Order.query.filter_by(order_type="Satış").order_by(Order.order_date.desc(), Order.id.desc()).all()
        if request.method == "POST":
            customer_id = request.form.get("customer_id", type=int)
            source_order_id = request.form.get("source_order_id", type=int) if order.order_type == "Satın Alma" else None
            source_order = db.session.get(Order, source_order_id) if source_order_id else None
            names = request.form.getlist("product_name[]")
            quantities = request.form.getlist("quantity[]")
            if not customer_id or not db.session.get(Customer, customer_id):
                flash("Lütfen bir müşteri veya tedarikçi seçin.", "error")
            elif source_order_id and (not source_order or source_order.order_type != "Satış"):
                flash("Bağlamak için geçerli bir satış siparişi seçin.", "error")
            elif order.order_type == "Satış" and request.form.get("payment_method", "") not in ORDER_PAYMENT_METHODS:
                flash("Lütfen satış siparişi için ödeme yöntemini seçin.", "error")
            elif not any(name.strip() for name in names):
                flash("En az bir sipariş kalemi ekleyin.", "error")
            else:
                create_database_backup(app, "before_order_edit")
                previous_source_order_id = order.source_order_id
                order.customer_id = customer_id
                order.order_date = parse_date(request.form.get("order_date")) or order.order_date
                order.delivery_date = parse_date(request.form.get("delivery_date"))
                order.delivery_city = request.form.get("delivery_city", "").strip() if order.order_type == "Satın Alma" else None
                order.shipment_contact = request.form.get("shipment_contact", "").strip() if order.order_type == "Satın Alma" else None
                order.shipment_phone = request.form.get("shipment_phone", "").strip() if order.order_type == "Satın Alma" else None
                order.shipment_address = request.form.get("shipment_address", "").strip() if order.order_type == "Satın Alma" else None
                order.shipment_note = request.form.get("shipment_note", "").strip() if order.order_type == "Satın Alma" else None
                order.customer_company = request.form.get("customer_company", "").strip() if order.order_type == "Satın Alma" else None
                order.payment_method = request.form.get("payment_method", "") if order.order_type == "Satış" else None
                order.source_order_id = source_order.id if source_order else None
                order.notes = request.form.get("notes", "").strip()
                previous_costs = {(item.product_id, item.product_name): item.cost_unit_price for item in order.items}
                order.items.clear()
                product_ids = request.form.getlist("product_id[]")
                variants = request.form.getlist("variant[]")
                details_2 = request.form.getlist("detail_2[]")
                details_3 = request.form.getlist("detail_3[]")
                units = request.form.getlist("unit[]")
                prices = request.form.getlist("unit_price[]")
                discount_rates = request.form.getlist("discount_rate[]")
                vat_rates = request.form.getlist("vat_rate[]")
                vat_included_values = request.form.getlist("vat_included[]")
                item_notes = request.form.getlist("item_note[]")
                for index, name in enumerate(names):
                    if not name.strip():
                        continue
                    quantity_text = quantities[index] if index < len(quantities) else "1"
                    try:
                        quantity = max(int(quantity_text), 1)
                    except (TypeError, ValueError):
                        quantity = 1
                    product_id = int(product_ids[index]) if index < len(product_ids) and product_ids[index].isdigit() else None
                    vat_rate = parse_money(vat_rates[index] if index < len(vat_rates) else "10")
                    discount_rate = parse_money(discount_rates[index] if index < len(discount_rates) else "0")
                    saved_cost = previous_costs.get((product_id, name.strip()))
                    cost = saved_cost if saved_cost is not None else Decimal("0")
                    order.items.append(OrderItem(product_id=product_id, product_name=name.strip(), description="", variant=variants[index] if index < len(variants) else "", detail_2=details_2[index] if index < len(details_2) else "", detail_3=details_3[index] if index < len(details_3) else "", quantity=quantity, unit=units[index] if index < len(units) and units[index] else "Adet", unit_price=parse_money(prices[index] if index < len(prices) else "0"), discount_rate=max(Decimal("0"), min(discount_rate, Decimal("100"))), cost_unit_price=cost, vat_rate=max(Decimal("0"), min(vat_rate, Decimal("100"))), vat_included=(vat_included_values[index] if index < len(vat_included_values) else "0") == "1", note=item_notes[index] if index < len(item_notes) else ""))
                if previous_source_order_id != order.source_order_id:
                    source_note = f"{source_order.order_no} numaralı satış siparişi bağlandı" if source_order else "Satış siparişi bağlantısı kaldırıldı"
                    order.history.append(OrderHistory(status=order.status, note=source_note))
                order.history.append(OrderHistory(status=order.status, note="Sipariş bilgileri düzenlendi"))
                db.session.commit()
                flash(f"{order.order_no} numaralı sipariş güncellendi.", "success")
                return redirect(url_for("order_detail", order_id=order.id))
        initial_items = [{"product_id": item.product_id, "product_name": item.product_name, "product_label": (f"{item.product.name} · {item.product.code}" if item.product and item.product.code else item.product.name if item.product else item.product_name), "variant": item.variant or "", "detail_2": item.detail_2 or "", "detail_3": item.detail_3 or "", "quantity": item.quantity, "unit": item.unit, "unit_price": str(item.unit_price or 0), "discount_rate": str(item.discount_rate or 0), "vat_rate": str(item.vat_rate or 0), "vat_included": bool(item.vat_included), "note": item.note or ""} for item in order.items]
        return render_template("order_form.html", order=order, initial_items=initial_items, customers=customers_list, products=products_list, sales_orders=sales_orders, order_types=ORDER_TYPES, order_payment_methods=ORDER_PAYMENT_METHODS, selected_type=order.order_type, today=order.order_date.isoformat())

    @app.route("/siparisler/<int:order_id>/satinalmaya-donustur", methods=["GET", "POST"])
    def convert_to_purchase(order_id):
        source_order = db.get_or_404(Order, order_id)
        if source_order.order_type != "Satış":
            flash("Yalnızca satış siparişleri satın alma siparişine dönüştürülebilir.", "error")
            return redirect(url_for("order_detail", order_id=source_order.id))
        suppliers = Customer.query.order_by(Customer.name).all()
        converted_orders = Order.query.filter_by(source_order_id=source_order.id).order_by(Order.id).all()
        procurement = procurement_summary(source_order, converted_orders)
        if request.method == "POST":
            supplier_id = request.form.get("customer_id", type=int)
            selected_ids = {value for value in request.form.getlist("source_item_id[]") if value.isdigit()}
            selected_items = [item for item in source_order.items if str(item.id) in selected_ids and procurement["items"][item.id]["remaining"] > 0]
            if not supplier_id or not db.session.get(Customer, supplier_id):
                flash("Lütfen bir tedarikçi seçin.", "error")
            elif not selected_items:
                flash("Satın alma siparişine aktarılacak en az bir ürün seçin.", "error")
            else:
                create_database_backup(app, "before_order_conversion")
                purchase = Order(order_no=next_order_no("Satın Alma"), order_type="Satın Alma", source_order_id=source_order.id, customer_id=supplier_id, order_date=parse_date(request.form.get("order_date")) or date.today(), delivery_date=parse_date(request.form.get("delivery_date")), delivery_city=request.form.get("delivery_city", "").strip(), customer_company=request.form.get("customer_company", "").strip() or source_order.customer.name, notes=request.form.get("notes", "").strip(), status="Bekliyor")
                for field in ("shipment_contact", "shipment_phone", "shipment_address", "shipment_note"):
                    setattr(purchase, field, request.form.get(field, "").strip())
                for source_item in selected_items:
                    item_id = source_item.id
                    vat_rate = parse_money(request.form.get(f"vat_rate_{item_id}", "10"))
                    remaining_quantity = procurement["items"][source_item.id]["remaining"]
                    quantity = request.form.get(f"quantity_{item_id}", type=int) or remaining_quantity
                    purchase.items.append(OrderItem(source_order_item_id=source_item.id, product_id=source_item.product_id, product_name=source_item.product_name, description="", variant=request.form.get(f"variant_{item_id}", source_item.variant), detail_2=request.form.get(f"detail_2_{item_id}", source_item.detail_2), detail_3=request.form.get(f"detail_3_{item_id}", source_item.detail_3), quantity=max(1, min(quantity, remaining_quantity)), unit=source_item.unit, unit_price=parse_money(request.form.get(f"unit_price_{item_id}", "0")), vat_rate=max(Decimal("0"), min(vat_rate, Decimal("100"))), vat_included=request.form.get(f"vat_included_{item_id}", "0") == "1", note=request.form.get(f"item_note_{item_id}", source_item.note)))
                purchase.history.append(OrderHistory(status="Bekliyor", note="Satış siparişindeki ürün detaylarından oluşturuldu"))
                db.session.add(purchase)
                db.session.commit()
                flash(f"{purchase.order_no} numaralı satın alma siparişi oluşturuldu. Satış müşterisi ve satış fiyatları aktarılmadı.", "success")
                return redirect(url_for("order_detail", order_id=purchase.id))
        return render_template("order_convert.html", source_order=source_order, suppliers=suppliers, converted_orders=converted_orders, procurement=procurement, today=date.today().isoformat())

    @app.post("/siparisler/<int:order_id>/durum")
    def update_order_status(order_id):
        order = db.get_or_404(Order, order_id)
        status = request.form.get("status")
        if status not in ORDER_STATUSES:
            flash("Geçersiz sipariş durumu.", "error")
        elif status != order.status:
            order.status = status
            if order.order_type == "Satış" and status in FINANCIAL_ORDER_STATUSES:
                for item in order.items:
                    if not item.cost_unit_price:
                        item.cost_unit_price = latest_delivered_purchase_cost(item)
            order.history.append(OrderHistory(status=status, note=request.form.get("note", "").strip() or "Durum güncellendi"))
            db.session.commit()
            flash("Sipariş durumu güncellendi.", "success")
        return_to = request.form.get("return_to", "").strip()
        if return_to.startswith("/siparisler") and not return_to.startswith("//"):
            return redirect(return_to)
        return redirect(url_for("order_detail", order_id=order.id))

    @app.route("/sahsi-hesaplar", methods=["GET", "POST"])
    def personal_finance():
        tab = request.values.get("tab", "payments")
        selected_month = request.values.get("month")
        selected_person_id = request.values.get("person_id", type=int)

        def period_label(value):
            try:
                return datetime.strptime(value, "%Y-%m").strftime("%m / %Y")
            except (TypeError, ValueError):
                return value

        def optional_money(field_name):
            raw = request.form.get(field_name, "").strip()
            return parse_money(raw) if raw else None

        if request.method == "POST":
            action = request.form.get("action")
            if action == "save_payments":
                create_database_backup(app, "before_personal_payments")
                for entry_id in request.form.getlist("entry_id"):
                    entry = db.session.get(PersonalPayment, int(entry_id))
                    if not entry:
                        continue
                    entry.name = request.form.get(f"name_{entry.id}", "").strip() or entry.name
                    entry.kind = request.form.get(f"kind_{entry.id}", entry.kind)
                    entry.due_day = request.form.get(f"due_day_{entry.id}", type=int)
                    entry.credit_limit = optional_money(f"credit_limit_{entry.id}")
                    entry.debt = optional_money(f"debt_{entry.id}")
                    entry.minimum_payment = optional_money(f"minimum_payment_{entry.id}")
                    entry.payment = optional_money(f"payment_{entry.id}")
                    entry.available_limit = optional_money(f"available_limit_{entry.id}")
                    entry.remaining_debt = optional_money(f"remaining_debt_{entry.id}") if entry.kind != "card" else None
                db.session.commit()
                flash("Şahsi ödeme tablosu kaydedildi.", "success")
            elif action == "add_payment":
                order = db.session.query(db.func.max(PersonalPayment.sort_order)).filter_by(month=selected_month).scalar() or 0
                db.session.add(PersonalPayment(month=selected_month, kind="other", name="Yeni ödeme", sort_order=order + 1))
                db.session.commit()
            elif action == "delete_payment":
                entry = db.session.get(PersonalPayment, request.form.get("entry_id", type=int))
                if entry:
                    db.session.delete(entry)
                    db.session.commit()
            elif action == "new_month":
                new_month = " ".join(request.form.get("new_month", "").split())
                source = db.session.get(PersonalMonth, selected_month)
                duplicate = PersonalMonth.query.filter(
                    db.func.normalize_tr(PersonalMonth.month) == normalize_search_text(new_month)
                ).first() if new_month else None
                if new_month and len(new_month) <= 80 and not duplicate:
                    target = PersonalMonth(month=new_month)
                    db.session.add(target)
                    db.session.flush()
                    for entry in source.entries if source else []:
                        db.session.add(PersonalPayment(month=new_month, kind=entry.kind, name=entry.name,
                            credit_limit=entry.credit_limit, due_day=entry.due_day, sort_order=entry.sort_order))
                    db.session.commit()
                    selected_month = new_month
                    flash("Yeni dönem oluşturuldu.", "success")
                else:
                    flash("Dönem adı boş, çok uzun veya zaten mevcut.", "error")
            elif action == "delete_month":
                target = db.session.get(PersonalMonth, selected_month)
                if target:
                    entry_count = len(target.entries)
                    target_label = period_label(target.month)
                    create_database_backup(app, "before_personal_period_delete")
                    db.session.delete(target)
                    db.session.commit()
                    selected_month = None
                    flash(f"{target_label} dönemi ve {entry_count} ödeme satırı silindi.", "success")
            elif action == "add_person":
                name = request.form.get("person_name", "").strip()
                if name and not PersonalPerson.query.filter(db.func.normalize_tr(PersonalPerson.name) == normalize_search_text(name)).first():
                    person = PersonalPerson(name=name)
                    db.session.add(person)
                    db.session.commit()
                    selected_person_id = person.id
                    flash(f"{name} kişi hesabı eklendi.", "success")
                else:
                    flash("Kişi adı boş veya zaten mevcut.", "error")
            elif action == "save_ledger":
                create_database_backup(app, "before_personal_ledger")
                for transaction_id in request.form.getlist("transaction_id"):
                    transaction = db.session.get(PersonalLedgerTransaction, int(transaction_id))
                    if not transaction:
                        continue
                    transaction.transaction_date = parse_date(request.form.get(f"transaction_date_{transaction.id}"))
                    transaction.description = request.form.get(f"description_{transaction.id}", "").strip()
                    transaction.sent_amount = optional_money(f"sent_amount_{transaction.id}")
                    transaction.received_amount = optional_money(f"received_amount_{transaction.id}")
                    if transaction.sent_amount:
                        transaction.received_amount = None
                new_dates = request.form.getlist("new_transaction_date[]")
                new_descriptions = request.form.getlist("new_description[]")
                new_sent = request.form.getlist("new_sent_amount[]")
                new_received = request.form.getlist("new_received_amount[]")
                order = db.session.query(db.func.max(PersonalLedgerTransaction.sort_order)).filter_by(person_id=selected_person_id).scalar() or 0
                for index, description in enumerate(new_descriptions):
                    sent = parse_money(new_sent[index]) if index < len(new_sent) and new_sent[index].strip() else None
                    received = parse_money(new_received[index]) if index < len(new_received) and new_received[index].strip() else None
                    if not description.strip() and not sent and not received:
                        continue
                    order += 1
                    db.session.add(PersonalLedgerTransaction(person_id=selected_person_id,
                        transaction_date=parse_date(new_dates[index]) if index < len(new_dates) else date.today(),
                        description=description.strip(), sent_amount=sent, received_amount=None if sent else received, sort_order=order))
                db.session.commit()
                flash("Borç–alacak hesabı kaydedildi.", "success")
            elif action == "delete_transaction":
                transaction = db.session.get(PersonalLedgerTransaction, request.form.get("transaction_id", type=int))
                if transaction:
                    selected_person_id = transaction.person_id
                    db.session.delete(transaction)
                    db.session.commit()
            return redirect(url_for("personal_finance", tab=tab, month=selected_month, person_id=selected_person_id))

        months = PersonalMonth.query.order_by(PersonalMonth.created_at.desc(), PersonalMonth.month.desc()).all()
        if not months:
            selected_month = date.today().strftime("%m / %Y")
            db.session.add(PersonalMonth(month=selected_month))
            db.session.commit()
            months = PersonalMonth.query.all()
        if not selected_month or not db.session.get(PersonalMonth, selected_month):
            selected_month = months[0].month
        period_options = [{"value": item.month, "label": period_label(item.month)} for item in months]
        entries = PersonalPayment.query.filter_by(month=selected_month).order_by(PersonalPayment.sort_order, PersonalPayment.id).all()
        payment_totals = {
            "debt": sum((item.debt or Decimal("0") for item in entries), Decimal("0")),
            "minimum": sum((item.minimum_payment or Decimal("0") for item in entries), Decimal("0")),
            "remaining_minimum": sum((item.calculated_remaining_minimum for item in entries), Decimal("0")),
            "payment": sum((item.payment or Decimal("0") for item in entries), Decimal("0")),
            "available_limit": sum((item.available_limit or Decimal("0") for item in entries if item.kind == "card"), Decimal("0")),
            "remaining": sum((item.calculated_remaining for item in entries), Decimal("0")),
        }
        people = PersonalPerson.query.order_by(PersonalPerson.name).all()
        person = db.session.get(PersonalPerson, selected_person_id) if selected_person_id else (people[0] if people else None)
        transactions = list(person.transactions) if person else []
        sent_total = sum((item.sent_amount or Decimal("0") for item in transactions), Decimal("0"))
        received_total = sum((item.received_amount or Decimal("0") for item in transactions), Decimal("0"))
        return render_template("personal_finance.html", tab=tab, months=months, selected_month=selected_month,
            entries=entries, payment_totals=payment_totals, people=people, person=person, transactions=transactions,
            sent_total=sent_total, received_total=received_total, balance=received_total-sent_total, today=date.today().isoformat(),
            period_options=period_options, selected_period_label=period_label(selected_month))

    @app.post("/sahsi-hesaplar/odeme/<int:entry_id>/sil")
    def delete_personal_payment(entry_id):
        entry = db.get_or_404(PersonalPayment, entry_id)
        month = entry.month
        db.session.delete(entry)
        db.session.commit()
        flash("Ödeme satırı silindi.", "success")
        return redirect(url_for("personal_finance", tab="payments", month=month))

    @app.post("/sahsi-hesaplar/hareket/<int:transaction_id>/sil")
    def delete_personal_transaction(transaction_id):
        transaction = db.get_or_404(PersonalLedgerTransaction, transaction_id)
        person_id = transaction.person_id
        db.session.delete(transaction)
        db.session.commit()
        flash("Hareket silindi.", "success")
        return redirect(url_for("personal_finance", tab="ledger", person_id=person_id))

    @app.get("/sahsi-hesaplar/borc-alacak/<int:person_id>/pdf")
    def export_personal_ledger_pdf(person_id):
        person = db.get_or_404(PersonalPerson, person_id)
        filename = f"{safe_export_name(person.name)}-Borc-Alacak-Ekstresi.pdf"
        return send_file(build_personal_ledger_pdf(person), mimetype="application/pdf", as_attachment=True, download_name=filename)

    @app.get("/sahsi-hesaplar/borc-alacak/<int:person_id>/excel")
    def export_personal_ledger_xlsx(person_id):
        person = db.get_or_404(PersonalPerson, person_id)
        filename = f"{safe_export_name(person.name)}-Borc-Alacak-Ekstresi.xlsx"
        return send_file(build_personal_ledger_xlsx(person),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name=filename)

    @app.cli.command("init-db")
    def init_db_command():
        db.create_all()
        print("Veritabanı hazırlandı.")

    with app.app_context():
        create_database_backup(app, "startup")
        db.create_all()
        import_personal_finance_data(app)
        # Küçük SQLite kurulumlarında ayrıca bir migration aracı gerektirmeden
        # eski müşteri tablolarını yeni alanlarla uyumlu hale getirir.
        if db.engine.dialect.name == "sqlite":
            customer_columns = {column["name"] for column in inspect(db.engine).get_columns("customer")}
            if "code" not in customer_columns:
                db.session.execute(text("ALTER TABLE customer ADD COLUMN code VARCHAR(80)"))
                db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_customer_code ON customer (code)"))
            if "mobile" not in customer_columns:
                db.session.execute(text("ALTER TABLE customer ADD COLUMN mobile VARCHAR(40)"))
            if "tax_office" not in customer_columns:
                db.session.execute(text("ALTER TABLE customer ADD COLUMN tax_office VARCHAR(120)"))
            if "tax_number" not in customer_columns:
                db.session.execute(text("ALTER TABLE customer ADD COLUMN tax_number VARCHAR(20)"))
                db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_customer_tax_number ON customer (tax_number)"))
            if "shipment_contact" not in customer_columns:
                db.session.execute(text("ALTER TABLE customer ADD COLUMN shipment_contact VARCHAR(120)"))
            if "shipment_phone" not in customer_columns:
                db.session.execute(text("ALTER TABLE customer ADD COLUMN shipment_phone VARCHAR(40)"))
            if "shipment_city" not in customer_columns:
                db.session.execute(text("ALTER TABLE customer ADD COLUMN shipment_city VARCHAR(100)"))
            if "shipment_address" not in customer_columns:
                db.session.execute(text("ALTER TABLE customer ADD COLUMN shipment_address TEXT"))
            if "shipment_note" not in customer_columns:
                db.session.execute(text("ALTER TABLE customer ADD COLUMN shipment_note TEXT"))
            if "tax_document_name" not in customer_columns:
                db.session.execute(text("ALTER TABLE customer ADD COLUMN tax_document_name VARCHAR(255)"))
            if "tax_document_stored_name" not in customer_columns:
                db.session.execute(text("ALTER TABLE customer ADD COLUMN tax_document_stored_name VARCHAR(255)"))
            if "tax_document_mime_type" not in customer_columns:
                db.session.execute(text("ALTER TABLE customer ADD COLUMN tax_document_mime_type VARCHAR(120)"))
            order_columns = {column["name"] for column in inspect(db.engine).get_columns("order")}
            if "order_type" not in order_columns:
                db.session.execute(text("ALTER TABLE 'order' ADD COLUMN order_type VARCHAR(30) NOT NULL DEFAULT 'Satış'"))
                db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_order_order_type ON 'order' (order_type)"))
            if "delivery_city" not in order_columns:
                db.session.execute(text("ALTER TABLE 'order' ADD COLUMN delivery_city VARCHAR(100)"))
            if "shipment_contact" not in order_columns:
                db.session.execute(text("ALTER TABLE 'order' ADD COLUMN shipment_contact VARCHAR(120)"))
            if "shipment_phone" not in order_columns:
                db.session.execute(text("ALTER TABLE 'order' ADD COLUMN shipment_phone VARCHAR(40)"))
            if "shipment_address" not in order_columns:
                db.session.execute(text("ALTER TABLE 'order' ADD COLUMN shipment_address TEXT"))
            if "shipment_note" not in order_columns:
                db.session.execute(text("ALTER TABLE 'order' ADD COLUMN shipment_note TEXT"))
            if "customer_company" not in order_columns:
                db.session.execute(text("ALTER TABLE 'order' ADD COLUMN customer_company VARCHAR(180)"))
            if "payment_method" not in order_columns:
                db.session.execute(text("ALTER TABLE 'order' ADD COLUMN payment_method VARCHAR(40)"))
            product_columns = {column["name"] for column in inspect(db.engine).get_columns("product")}
            if "special_code" not in product_columns:
                db.session.execute(text("ALTER TABLE product ADD COLUMN special_code VARCHAR(160)"))
            if "group_name" not in product_columns:
                db.session.execute(text("ALTER TABLE product ADD COLUMN group_name VARCHAR(160)"))
                db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_product_group_name ON product (group_name)"))
            if "purchase_price" not in product_columns:
                db.session.execute(text("ALTER TABLE product ADD COLUMN purchase_price NUMERIC(12, 2) NOT NULL DEFAULT 0"))
            if "include_in_catalog" not in product_columns:
                db.session.execute(text("ALTER TABLE product ADD COLUMN include_in_catalog BOOLEAN NOT NULL DEFAULT 0"))
                db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_product_include_in_catalog ON product (include_in_catalog)"))
            if "include_in_price_list" not in product_columns:
                db.session.execute(text("ALTER TABLE product ADD COLUMN include_in_price_list BOOLEAN NOT NULL DEFAULT 0"))
                db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_product_include_in_price_list ON product (include_in_price_list)"))
            if "source_order_id" not in order_columns:
                db.session.execute(text("ALTER TABLE 'order' ADD COLUMN source_order_id INTEGER"))
            source_index = next((item for item in inspect(db.engine).get_indexes("order") if item["name"] == "ix_order_source_order_id"), None)
            if source_index and source_index.get("unique"):
                db.session.execute(text("DROP INDEX ix_order_source_order_id"))
            db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_order_source_order_id ON 'order' (source_order_id)"))
            invoice_order_index = next((item for item in inspect(db.engine).get_indexes("invoice") if item["name"] == "ix_invoice_order_id"), None)
            if invoice_order_index and invoice_order_index.get("unique"):
                db.session.execute(text("DROP INDEX ix_invoice_order_id"))
            db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_invoice_order_id ON invoice (order_id)"))
            item_columns = {column["name"] for column in inspect(db.engine).get_columns("order_item")}
            if "detail_2" not in item_columns:
                db.session.execute(text("ALTER TABLE order_item ADD COLUMN detail_2 VARCHAR(160)"))
            if "detail_3" not in item_columns:
                db.session.execute(text("ALTER TABLE order_item ADD COLUMN detail_3 VARCHAR(160)"))
            if "vat_rate" not in item_columns:
                # Eski siparişlerin toplamını değiştirmemek için geçmiş kalemlerde KDV %0 kalır.
                db.session.execute(text("ALTER TABLE order_item ADD COLUMN vat_rate NUMERIC(5, 2) NOT NULL DEFAULT 0"))
            if "vat_included" not in item_columns:
                # Eski kayıtlar önceki hesaplama biçimiyle uyumlu olarak KDV hariç kalır.
                db.session.execute(text("ALTER TABLE order_item ADD COLUMN vat_included BOOLEAN NOT NULL DEFAULT 0"))
            if "discount_rate" not in item_columns:
                db.session.execute(text("ALTER TABLE order_item ADD COLUMN discount_rate NUMERIC(5, 2) NOT NULL DEFAULT 0"))
            if "cost_unit_price" not in item_columns:
                db.session.execute(text("ALTER TABLE order_item ADD COLUMN cost_unit_price NUMERIC(12, 2) NOT NULL DEFAULT 0"))
                # Mevcut satış kalemleri için ürün kartındaki güncel alış fiyatını
                # bir defaya mahsus başlangıç maliyeti olarak sabitler.
                db.session.execute(text("""
                    UPDATE order_item
                    SET cost_unit_price = COALESCE((
                        SELECT product.purchase_price FROM product
                        WHERE product.id = order_item.product_id
                    ), 0)
                    WHERE order_id IN (SELECT id FROM 'order' WHERE order_type = 'Satış')
                """))
            if "source_order_item_id" not in item_columns:
                db.session.execute(text("ALTER TABLE order_item ADD COLUMN source_order_item_id INTEGER"))
            db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_order_item_source_order_item_id ON order_item (source_order_item_id)"))
            invoice_item_columns = {column["name"] for column in inspect(db.engine).get_columns("invoice_item")}
            if "discount_rate" not in invoice_item_columns:
                db.session.execute(text("ALTER TABLE invoice_item ADD COLUMN discount_rate NUMERIC(5, 2) NOT NULL DEFAULT 0"))
            account_columns = {column["name"] for column in inspect(db.engine).get_columns("account_transaction")}
            if "payment_method" not in account_columns:
                db.session.execute(text("ALTER TABLE account_transaction ADD COLUMN payment_method VARCHAR(30)"))
            if "check_no" not in account_columns:
                db.session.execute(text("ALTER TABLE account_transaction ADD COLUMN check_no VARCHAR(80)"))
            if "check_bank" not in account_columns:
                db.session.execute(text("ALTER TABLE account_transaction ADD COLUMN check_bank VARCHAR(120)"))
            if "check_due_date" not in account_columns:
                db.session.execute(text("ALTER TABLE account_transaction ADD COLUMN check_due_date DATE"))
                db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_account_transaction_check_due_date ON account_transaction (check_due_date)"))
            if "check_status" not in account_columns:
                db.session.execute(text("ALTER TABLE account_transaction ADD COLUMN check_status VARCHAR(40)"))
            if "card_installments" not in account_columns:
                db.session.execute(text("ALTER TABLE account_transaction ADD COLUMN card_installments INTEGER"))
            if "card_owner_type" not in account_columns:
                db.session.execute(text("ALTER TABLE account_transaction ADD COLUMN card_owner_type VARCHAR(40)"))
            if "card_customer_id" not in account_columns:
                db.session.execute(text("ALTER TABLE account_transaction ADD COLUMN card_customer_id INTEGER"))
            if "card_customer_name" not in account_columns:
                db.session.execute(text("ALTER TABLE account_transaction ADD COLUMN card_customer_name VARCHAR(160)"))
            if "linked_transaction_id" not in account_columns:
                db.session.execute(text("ALTER TABLE account_transaction ADD COLUMN linked_transaction_id INTEGER"))
            db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_account_transaction_linked_transaction_id ON account_transaction (linked_transaction_id)"))
            ozon_sale_columns = {column["name"] for column in inspect(db.engine).get_columns("ozon_sale")}
            if "store_key" not in ozon_sale_columns:
                db.session.execute(text("ALTER TABLE ozon_sale ADD COLUMN store_key VARCHAR(60) NOT NULL DEFAULT 'magaza-1'"))
                db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_ozon_sale_store_key ON ozon_sale (store_key)"))
            if "external_operation_id" not in ozon_sale_columns:
                db.session.execute(text("ALTER TABLE ozon_sale ADD COLUMN external_operation_id VARCHAR(80)"))
            db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_ozon_sale_store_operation ON ozon_sale (store_key, external_operation_id)"))
            # Ozon ürün tablosu db.create_all ile yeni kurulumlarda oluşur.
            # Eski kurulumlarda da bu bölüm çalıştığında tablo zaten oluşturulmuştur.
            finance_columns = {column["name"] for column in inspect(db.engine).get_columns("ozon_finance_snapshot")}
            if "store_key" not in finance_columns:
                db.session.execute(text("ALTER TABLE ozon_finance_snapshot ADD COLUMN store_key VARCHAR(60) NOT NULL DEFAULT 'magaza-1'"))
                db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_ozon_finance_snapshot_store_key ON ozon_finance_snapshot (store_key)"))
            if "period_month" not in finance_columns:
                db.session.execute(text("ALTER TABLE ozon_finance_snapshot ADD COLUMN period_month VARCHAR(7)"))
                db.session.execute(text("UPDATE ozon_finance_snapshot SET period_month = substr(report_month, -7) WHERE period_month IS NULL"))
                db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_ozon_finance_snapshot_period_month ON ozon_finance_snapshot (period_month)"))
            db.session.commit()
    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
