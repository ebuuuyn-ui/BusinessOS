"""Deactivate stock cards without modifying historical records or prices."""
import hashlib
import json
import re
from io import BytesIO
from uuid import uuid4
from flask import abort, g, redirect, render_template, request, send_file, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from stock_excel_import import snapshots


def archive_plan(products, codes):
    before = snapshots(products)
    known = {str(p.code or '').strip().upper() for p in products}
    missing = sorted(set(codes) - known)
    if missing:
        raise ValueError('Bu kodlar stoklarda bulunamadı: ' + ', '.join(missing))
    changes = [p for p in before if p['active'] and str(p['code'] or '').strip().upper() not in codes]
    return dict(before=before, changes=changes,
                fingerprint=hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest())


def register_stock_archive(app, db, Product, StoredFile):
    signer = URLSafeTimedSerializer(app.secret_key, salt='stock-archive-v1')

    def owner():
        if app.config.get('WEB_AUTH_ENABLED') and not getattr(g, 'web_is_owner', False):
            abort(403)

    @app.route('/yonetim/stok-arsivle', methods=['GET', 'POST'])
    def stock_archive():
        owner()
        if request.method == 'GET':
            return render_template('stock_archive.html')
        try:
            if request.form.get('action') == 'apply':
                payload = signer.loads(request.form.get('plan', ''), max_age=1800)
                key = 'stock-archive/' + payload['nonce'] + '.json'
                if StoredFile.query.filter_by(storage_key=key).first():
                    return redirect(url_for('stock_archive_result', key=payload['nonce']))
                products = Product.query.order_by(Product.id).with_for_update().all()
                plan = archive_plan(products, set(payload['codes']))
                if plan['fingerprint'] != payload['fingerprint']:
                    raise ValueError('Stok kartları değişti. Yeniden önizleyin; hiçbir kayıt değiştirilmedi.')
                ids = {p['id'] for p in plan['changes']}
                for product in products:
                    if product.id in ids:
                        product.active = False
                db.session.flush()
                after = snapshots(products)
                for old, new in zip(plan['before'], after):
                    expected = dict(old)
                    if old['id'] in ids:
                        expected['active'] = False
                    if expected != new:
                        raise ValueError('Kontrol başarısız; işlem geri alındı.')
                result = dict(before=plan['before'], after=after, changes=plan['changes'],
                              codes=payload['codes'], actor=getattr(g, 'web_username', 'desktop'))
                content = json.dumps(result, ensure_ascii=False, indent=2).encode()
                db.session.add(StoredFile(storage_key=key, content=content, mime_type='application/json',
                                          size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest()))
                db.session.commit()
                return redirect(url_for('stock_archive_result', key=payload['nonce']))
            codes = sorted(set(c.strip().upper() for c in request.form.get('codes', '').splitlines() if c.strip()))
            if not codes or len(codes) > 5000 or any(not re.fullmatch(r'[A-Z0-9._-]{1,80}', c) for c in codes):
                raise ValueError('Her satıra bir geçerli stok kodu yazın. Boş listeyle işlem yapılamaz.')
            products = Product.query.order_by(Product.id).all()
            plan = archive_plan(products, set(codes))
            token = signer.dumps(dict(codes=codes, fingerprint=plan['fingerprint'], nonce=uuid4().hex))
            return render_template('stock_archive.html', plan=plan, token=token, code_count=len(codes))
        except (ValueError, BadSignature) as exc:
            db.session.rollback()
            return render_template('stock_archive.html', error=str(exc) if isinstance(exc, ValueError) else 'Önizleme geçersiz veya süresi dolmuş.'), 400
        except Exception:
            db.session.rollback()
            app.logger.exception('Stock archive failed')
            return render_template('stock_archive.html', error='İşlem tamamlanamadı; değişiklikler geri alındı.'), 500

    @app.get('/yonetim/stok-arsiv-sonuc/<key>')
    def stock_archive_result(key):
        owner()
        if not re.fullmatch(r'[a-f0-9]{32}', key):
            abort(404)
        saved = StoredFile.query.filter_by(storage_key='stock-archive/' + key + '.json').first_or_404()
        if request.args.get('download') == 'backup':
            return send_file(BytesIO(saved.content), mimetype='application/json', as_attachment=True,
                             download_name='stok-pasife-alma-yedegi-' + key + '.json')
        return render_template('stock_archive.html', result=json.loads(saved.content), key=key)
