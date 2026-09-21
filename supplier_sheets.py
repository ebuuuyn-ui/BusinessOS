"""Explicit, price-free ABIKA purchase-order export to the supplier's sheet."""
import hashlib
import json
import os
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from flask import abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import text
from itsdangerous import BadSignature, URLSafeTimedSerializer

SPREADSHEET_ID = "1FNYd6lkPpKQeps-MyRbwKxd-9Qkl4eg9OdRBLSUxs-Q"
SHEET_ID = 0
SUPPLIER_ID = 1339  # Verified ABİKA customer record in this installation.
HEADERS = [
    "Kayıt Anahtarı", "Sipariş No", "Sipariş Tarihi", "Teslim Tarihi", "Sipariş Durumu",
    "Tedarikçi", "Müşteri Firma", "Sevkiyat İli", "Sevkiyat Yetkilisi", "Sevkiyat Telefonu",
    "Sevkiyat Adresi", "Sevkiyat Notu", "Ürün Kodu", "Ürün", "Ürün Açıklaması",
    "Ayrıntı 1", "Ayrıntı 2", "Ayrıntı 3", "Adet", "Birim", "Kalem Notu",
    "Sipariş Notu", "Aktaran Kullanıcı", "Aktarım Zamanı",
]


class SheetExportError(Exception):
    pass


def allowed_order(order):
    return order.order_type == "Satın Alma" and order.customer_id == SUPPLIER_ID


def order_rows(order, actor, now=None):
    if not allowed_order(order):
        raise SheetExportError("Bu tabloya yalnızca ABİKA satın alma siparişleri aktarılabilir.")
    stamp = (now or datetime.now(ZoneInfo("Europe/Istanbul"))).strftime("%d.%m.%Y %H:%M")
    day = lambda d: d.strftime("%d.%m.%Y") if d else ""
    # Whitelist only. Never serialize model __dict__, prices, taxes, totals, payment or cost fields.
    return [[
        f"bos:order:{order.id}:item:{item.id}", order.order_no, day(order.order_date),
        day(order.delivery_date), order.status, order.customer.name, order.customer_company or "",
        order.delivery_city or "", order.shipment_contact or "", order.shipment_phone or "",
        order.shipment_address or "", order.shipment_note or "",
        item.product.code if item.product and item.product.code else "", item.product_name,
        item.description or "", item.variant or "", item.detail_2 or "", item.detail_3 or "",
        item.quantity, item.unit, item.note or "", order.notes or "", actor, stamp,
    ] for item in order.items]


def fingerprint(rows):
    # Metadata about who/when does not affect whether the preview is still current.
    body = json.dumps([r[:-2] for r in rows], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def cell(value):
    # Explicit stringValue prevents product names/notes starting '=' becoming formulas.
    return {"userEnteredValue": {"numberValue": value} if isinstance(value, (int, float))
            else {"stringValue": str(value or "")}}


def build_updates(rows, key_rows, sheet_id=SHEET_ID):
    """Update matching stable keys, append new lines, clear removed lines of this order only."""
    if not rows:
        raise SheetExportError("Aktarılacak sipariş kalemi yok.")
    existing = {}
    for index, values in enumerate(key_rows, start=1):  # Data begins at row 2 (zero-based 1).
        key = str(values[0]) if values else ""
        if key.startswith("bos:") and key in existing:
            raise SheetExportError("Tabloda yinelenen kayıt anahtarı var; aktarım yapılmadı.")
        if key:
            existing[key] = index
    prefix = rows[0][0].split(":item:", 1)[0] + ":item:"
    wanted = {r[0] for r in rows}
    requests, added = [], []
    for row in rows:
        values = {"values": [cell(v) for v in row]}
        if row[0] in existing:
            requests.append({"updateCells": {"start": {"sheetId": sheet_id,
                "rowIndex": existing[row[0]], "columnIndex": 0}, "rows": [values],
                "fields": "userEnteredValue"}})
        else:
            added.append(values)
    for key, row_index in existing.items():
        if key.startswith(prefix) and key not in wanted:
            requests.append({"updateCells": {"range": {"sheetId": sheet_id,
                "startRowIndex": row_index, "endRowIndex": row_index + 1,
                "startColumnIndex": 0, "endColumnIndex": len(HEADERS)},
                "rows": [], "fields": "userEnteredValue"}})
    if added:
        requests.append({"appendCells": {"sheetId": sheet_id, "rows": added,
                                        "fields": "userEnteredValue"}})
    return requests


class SheetsClient:
    def __init__(self):
        # Credentials live only in server environment; never DB, HTML, Git or logs.
        from google.auth import identity_pool
        from google.auth.transport.requests import AuthorizedSession
        audience = os.getenv("BUSINESSOS_GOOGLE_WIF_AUDIENCE", "")
        if not audience.startswith("//iam.googleapis.com/projects/") or "/workloadIdentityPools/" not in audience:
            raise SheetExportError("Google Sheets bağlantısı henüz hazır değil.")
        token = request.headers.get("x-vercel-oidc-token", "")
        if not token:
            raise SheetExportError("Vercel bağlantı kimliği alınamadı. Canlı uygulamadan tekrar deneyin.")

        class VercelTokenSupplier(identity_pool.SubjectTokenSupplier):
            def get_subject_token(self, context, transport):
                return token

        # Google validates issuer, audience and the exact production project condition.
        credentials = identity_pool.Credentials(
            audience=audience,
            subject_token_type="urn:ietf:params:oauth:token-type:jwt",
            token_url="https://sts.googleapis.com/v1/token",
            subject_token_supplier=VercelTokenSupplier(),
            service_account_impersonation_url=(
                "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
                "businessos-supplier-sheets@august-edge-509315-r9.iam.gserviceaccount.com:generateAccessToken"),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self.session = AuthorizedSession(credentials, refresh_timeout=10)
        self.base = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}"

    def api(self, method, suffix="", **kwargs):
        try:
            response = self.session.request(method, self.base + suffix, timeout=12, **kwargs)
            if not response.ok:
                raise SheetExportError("Google Sheets işlemi tamamlanamadı. Bağlantı ve dosya izinlerini kontrol edin.")
            return response.json()
        except SheetExportError:
            raise
        except Exception:
            # Provider exceptions can include URLs/credentials. Do not expose them.
            raise SheetExportError("Google Sheets yanıtı alınamadı. Sonucu tabloda kontrol edip yeniden deneyebilirsiniz.") from None

    def target(self):
        result = self.api("GET", params={"fields": "sheets(properties)"})
        target = next((s["properties"] for s in result["sheets"] if s["properties"]["sheetId"] == SHEET_ID), None)
        if not target:
            raise SheetExportError("Hedef Sayfa1 sekmesi bulunamadı.")
        if target["gridProperties"]["rowCount"] > 50000:
            raise SheetExportError("Tablo 50.000 satırı aştı; aktarım alanının düzenlenmesi gerekiyor.")
        return target

    def values(self, title, range_name):
        name = "'" + title.replace("'", "''") + "'!" + range_name
        return self.api("GET", "/values/" + quote(name, safe=""),
                        params={"valueRenderOption": "UNFORMATTED_VALUE"}).get("values", [])

    def export(self, rows):
        target = self.target()
        title = target["title"]
        header = self.values(title, "A1:X1")
        if not header or header[0] != HEADERS:
            raise SheetExportError("Tedarikçi tablosunun başlıkları hazır değil veya değiştirilmiş. Aktarım yapılmadı.")
        keys = self.values(title, f"A2:A{target['gridProperties']['rowCount']}")
        self.api("POST", ":batchUpdate", json={"requests": build_updates(rows, keys)})
        # A successful API response is not enough: read back exact exported rows.
        updated_target = self.target()
        new_keys = self.values(title, f"A2:A{updated_target['gridProperties']['rowCount']}")
        locations = {}
        for index, key in enumerate(new_keys, start=2):
            if key:
                locations.setdefault(str(key[0]), []).append(index)
        for row in rows:
            positions = locations.get(row[0], [])
            if len(positions) != 1:
                raise SheetExportError("Aktarım sonucu doğrulanamadı. Tabloyu kontrol edip tekrar deneyin.")
        # Keep URLs bounded for orders with many lines.
        for offset in range(0, len(rows), 40):
            expected_rows = rows[offset:offset + 40]
            ranges = ["'" + title.replace("'", "''") + f"'!A{locations[r[0]][0]}:X{locations[r[0]][0]}" for r in expected_rows]
            result = self.api("GET", "/values:batchGet", params={"ranges": ranges, "valueRenderOption": "UNFORMATTED_VALUE"})
            value_ranges = result.get("valueRanges", [])
            if len(value_ranges) != len(expected_rows):
                raise SheetExportError("Aktarım sonucu doğrulanamadı.")
            for expected, value_range in zip(expected_rows, value_ranges):
                actual = (value_range.get("values") or [[]])[0]
                actual += [""] * (len(expected) - len(actual))
                if actual != expected:
                    raise SheetExportError("Aktarım sonucu doğrulanamadı. Tabloyu kontrol edip tekrar deneyin.")
        return len(rows)


def register_supplier_sheets(app, db, Order, OrderHistory):
    app.jinja_env.globals["supplier_sheet_allowed"] = allowed_order

    @app.get("/yonetim/tedarikci-tablosu/kontrol")
    def supplier_sheet_connection_check():
        if not app.config.get("WEB_AUTH_ENABLED"):
            abort(404)
        if not getattr(g, "web_is_owner", False):
            abort(403)
        try:
            client = SheetsClient()
            target = client.target()
            header = client.values(target["title"], "A1:X1")
            if header != [HEADERS]:
                raise SheetExportError("Tedarikçi tablosunun başlıkları beklenen düzenle uyuşmuyor.")
            return {"ok": True, "message": "Google Sheets bağlantısı ve tablo başlıkları doğrulandı. Sipariş aktarımı yapılmadı."}
        except SheetExportError as exc:
            return {"ok": False, "message": str(exc)}, 400

    @app.route("/siparisler/<int:order_id>/tedarikci-tablosu", methods=["GET", "POST"])
    def supplier_sheet_export(order_id):
        if not app.config.get("WEB_AUTH_ENABLED"):
            abort(404)
        order = db.get_or_404(Order, order_id)
        if not allowed_order(order):
            abort(403)
        rows = order_rows(order, g.web_username)
        signer = URLSafeTimedSerializer(app.secret_key, salt="supplier-sheet-preview-v1")
        error = None
        if request.method == "POST":
            try:
                preview = signer.loads(request.form.get("preview", ""), max_age=1800)
                if preview != {"order_id": order.id, "fingerprint": fingerprint(rows)}:
                    raise SheetExportError("Sipariş önizlemeden sonra değişti. Güncel bilgileri kontrol edip yeniden aktarın.")
                if db.engine.dialect.name != "postgresql":
                    raise SheetExportError("Bu bağlantı web sürümü için hazırlanmıştır.")
                # One sheet-wide transaction lock serializes requests across serverless instances.
                db.session.execute(text("SET LOCAL lock_timeout = '5s'"))
                db.session.execute(text("SELECT pg_advisory_xact_lock(72109342)"))
                # Re-read after waiting for another export, so an old preview cannot win the race.
                db.session.expire_all()
                rows = order_rows(order, g.web_username)
                if preview != {"order_id": order.id, "fingerprint": fingerprint(rows)}:
                    raise SheetExportError("Sipariş önizlemeden sonra değişti. Güncel bilgileri kontrol edip yeniden aktarın.")
                count = SheetsClient().export(rows)
                order.history.append(OrderHistory(status=order.status,
                    note=f"Tedarikçi tablosuna aktarıldı · {count} kalem · {g.web_username}"))
                db.session.commit()
                flash("Sipariş, fiyat bilgileri olmadan tedarikçi tablosuna aktarıldı.", "success")
                return redirect(url_for("order_detail", order_id=order.id))
            except BadSignature:
                db.session.rollback()
                error = "Önizleme süresi dolmuş. Bilgileri kontrol edip yeniden aktarın."
            except SheetExportError as exc:
                db.session.rollback()
                error = str(exc)
            except Exception:
                db.session.rollback()
                error = "Aktarım tamamlanamadı. Tabloyu kontrol edip yeniden deneyin."
        return render_template("supplier_sheet_preview.html", order=order, headers=HEADERS[1:],
            rows=[r[1:] for r in rows], error=error,
            connected=bool(os.getenv("BUSINESSOS_GOOGLE_WIF_AUDIENCE")),
            sheet_url=f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid={SHEET_ID}",
            token=signer.dumps({"order_id": order.id, "fingerprint": fingerprint(rows)})), 400 if error else 200
