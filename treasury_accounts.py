"""Named fund locations. Assignments and counterparty mirrors commit atomically."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from navigation import return_destination
from flask import request, render_template, redirect, url_for, flash, abort, has_request_context, g, current_app
from sqlalchemy import inspect, select, event, or_
from sqlalchemy.exc import IntegrityError

class TreasuryLocked(ValueError):
    pass

DEFAULTS = [('cash','Nakit Kasa','Nakit'),('kuveyt','Kuveyt Türk','Banka'),('enpara','Enpara','Banka'),('ahmet','Ahmet Tahsilat Sistemi','Tahsilat Sistemi'),('direct','Doğrudan Tedarikçi Aktarımı','Mahsup')]

CARD_DEFAULTS = [('card-maximum','Maximum Kredi Kartı'),('card-bonus','Bonus Kredi Kartı'),('card-garanti','Garanti Kredi Kartı'),('card-ahmet-enpara','Ahmet Enpara Kredi Kartı')]

def register_accounts(app, db, Customer, Transaction, Expense, Cash, backup):
    def table(name,*columns):
        return db.metadata.tables[name] if name in db.metadata.tables else db.Table(name,db.metadata,*columns)
    accounts=table('treasury_account',db.Column('id',db.Integer,primary_key=True),db.Column('slug',db.String(60),unique=True,nullable=False),db.Column('name',db.String(120),nullable=False),db.Column('kind',db.String(40),nullable=False),db.Column('customer_id',db.Integer,db.ForeignKey('customer.id'),unique=True),db.Column('created_at',db.DateTime,nullable=False))
    postings=table('treasury_posting',db.Column('id',db.String(36),primary_key=True),db.Column('account_id',db.Integer,db.ForeignKey('treasury_account.id'),nullable=False,index=True),db.Column('source_key',db.String(100),unique=True,nullable=False),db.Column('date',db.Date,nullable=False),db.Column('amount',db.Numeric(18,2),nullable=False),db.Column('description',db.String(500),nullable=False),db.Column('mirror_id',db.Integer,db.ForeignKey('account_transaction.id'),unique=True),db.Column('batch',db.String(36),index=True),db.Column('actor',db.String(100)),db.Column('created_at',db.DateTime,nullable=False))
    audit=table('treasury_account_audit',db.Column('id',db.String(36),primary_key=True),db.Column('action',db.String(40),nullable=False),db.Column('detail',db.Text,nullable=False),db.Column('actor',db.String(100)),db.Column('created_at',db.DateTime,nullable=False))
    tables=(accounts,postings,audit)
    def ready():
        inspector=inspect(db.session.connection())
        return all(inspector.has_table(t.name) for t in tables)
    def rows():
        return [dict(r) for r in db.session.execute(select(accounts).order_by(accounts.c.id)).mappings()] if ready() else []
    def log(action,detail):
        db.session.execute(audit.insert().values(id=str(uuid4()),action=action,detail=str(detail),actor=getattr(g,'web_username','local') if has_request_context() else 'test',created_at=datetime.utcnow()))
    def money(value):
        try: n=Decimal(str(value))
        except InvalidOperation: raise ValueError('Geçerli bir tutar girin.')
        if not n.is_finite() or n<=0 or n!=n.quantize(Decimal('.01')) or n>=Decimal('1000000000000'):
            raise ValueError('Tutar pozitif ve en fazla iki ondalık basamaklı olmalıdır.')
        return n
    def account(account_id):
        row=db.session.execute(select(accounts).where(accounts.c.id==int(account_id)).with_for_update()).mappings().first()
        if not row: raise ValueError('Geçerli bir kasa / hesap seçin.')
        return dict(row)
    def posted(key):
        return db.session.execute(select(postings).where(postings.c.source_key==key)).mappings().first()
    def add(a,key,day,amount,description,batch=None):
        if posted(key): raise ValueError('Bu hareket zaten bir hesaba bağlı.')
        mirror=None
        if a['customer_id']:
            mirror=Transaction(customer_id=a['customer_id'],transaction_type='Hesap Mahsubu',transaction_date=day,payment_method='Hesap Mahsubu',description=(a['name']+' · '+description)[:240],reference_no=key[:80],debit=max(amount,Decimal(0)),credit=max(-amount,Decimal(0)))
            db.session.add(mirror);db.session.flush()
        db.session.execute(postings.insert().values(id=str(uuid4()),account_id=a['id'],source_key=key,date=day,amount=amount,description=description[:500],mirror_id=mirror.id if mirror else None,batch=batch,actor=getattr(g,'web_username','local') if has_request_context() else 'test',created_at=datetime.utcnow()))
        log('post',key+' -> '+a['name']+' '+str(amount))
    def source(key):
        kind,raw=key.split(':',1)
        model={'tx':Transaction,'expense':Expense,'cash':Cash}.get(kind)
        if not model: raise ValueError('Geçersiz kaynak.')
        obj=db.session.execute(select(model).where(model.id==int(raw)).with_for_update()).scalar_one_or_none()
        if not obj: raise ValueError('Kaynak hareket bulunamadı.')
        if kind=='tx':
            if obj.transaction_type not in ('Tahsilat','Ödeme') or obj.payment_method=='Çek': raise ValueError('Bu hareket kasa hesabına bağlanamaz. Çekler çek takibinde kalır.')
            return obj,obj.transaction_date,Decimal(obj.credit or 0)-Decimal(obj.debit or 0),obj.description,obj.payment_method
        if kind=='expense': return obj,obj.expense_date,-Decimal(obj.amount),obj.description,obj.payment_method
        return obj,obj.movement_date,Decimal(obj.amount)*(1 if obj.movement_type=='Giriş' else -1),obj.description,'Nakit'
    def assign(key,aid):
        obj,day,amount,description,method=source(key)
        a=account(aid)
        if isinstance(obj,Transaction) and obj.linked_transaction_id:
            partner,pday,pamount,pdesc,pmethod=source('tx:'+str(obj.linked_transaction_id))
            if partner.linked_transaction_id!=obj.id or method!='Kredi Kartı' or pmethod!='Kredi Kartı' or pamount+amount!=0:
                raise ValueError('Bağlı kart işlemleri uyuşmuyor.')
            if a['kind']!='Mahsup': raise ValueError('Doğrudan tedarikçi işlemleri Mahsup hesabına birlikte bağlanmalıdır.')
            batch=str(uuid4());add(a,key,day,amount,description,batch);add(a,'tx:'+str(partner.id),pday,pamount,pdesc,batch)
            return
        if a['kind']=='Mahsup': raise ValueError('Mahsup hesabı yalnızca bağlı tedarikçi kart işlemleri içindir.')
        allowed={'Nakit':{'Nakit'},'Banka':{'Banka','Nakit'},'Tahsilat Sistemi':{'Kredi Kartı','Nakit'},'Kredi Kartı':{'Kredi Kartı'}}
        own_card = isinstance(obj,Transaction) and obj.transaction_type=='Ödeme' and obj.card_owner_type=='Kendi Kartımız' and method=='Kredi Kartı'
        if a['kind']=='Kredi Kartı' and not (own_card or isinstance(obj,Expense) and method=='Kredi Kartı'):
            raise ValueError('Kredi kartı hesabına yalnızca kendi kartınızla yapılan ödemeyi bağlayın.')
        if own_card and a['kind']!='Kredi Kartı':
            raise ValueError('Kendi kartınızla yapılan ödeme için kredi kartı hesabı seçin.')
        if method not in allowed[a['kind']]: raise ValueError('Ödeme şekli ile seçilen hesap türü uyuşmuyor.')
        if isinstance(obj,Transaction) and a['customer_id']==obj.customer_id: raise ValueError('Hesaba bağlı carinin kendi hareketi için hesaplar arası transfer kullanın.')
        if a['kind']=='Tahsilat Sistemi' and method=='Nakit' and not isinstance(obj,Transaction):
            raise ValueError('Nakit masrafı tahsilat sistemine bağlanamaz.')
        add(a,key,day,amount,description)
    def unassign(key):
        obj=posted(key)
        if not obj: raise ValueError('Hesap bağlantısı bulunamadı.')
        group=list(db.session.execute(select(postings).where(postings.c.batch==obj['batch'] if obj['batch'] else postings.c.id==obj['id'])).mappings())
        db.session.info['treasury_internal']=True
        try:
            for row in group:
                db.session.execute(postings.delete().where(postings.c.id==row['id']))
                if row['mirror_id']:
                    mirror=db.session.get(Transaction,row['mirror_id'])
                    if mirror: db.session.delete(mirror)
                log('unassign',row['source_key'])
            db.session.flush()
        finally: db.session.info.pop('treasury_internal',None)
    def choices(): return rows()
    app.jinja_env.globals['treasury_accounts']=choices
    app.extensions['treasury_accounts']={'ready':ready,'rows':rows,'assign':assign,'unassign':unassign,'tables':tables,'postings':postings}

    def expense_account(expense):
        if not expense or not expense.id or not ready(): return ''
        p=posted('expense:'+str(expense.id))
        return str(p['account_id']) if p else ''
    def prepare_expense(expense):
        chosen=request.form.get('treasury_account_id',expense_account(expense)).strip()
        if chosen:
            if not ready(): raise TreasuryLocked('Önce Kasalar ekranından hesapları hazırlayın.')
            try: a=account(chosen)
            except (ValueError,TypeError): raise TreasuryLocked('Geçerli bir ödeme hesabı seçin.')
            if a['kind'] not in ('Nakit','Banka','Kredi Kartı'): raise TreasuryLocked('Masraf için kasa, banka veya kendi kredi kartınızı seçin.')
        if expense and expense.id and ready() and posted('expense:'+str(expense.id)):
            unassign('expense:'+str(expense.id))
        return chosen
    def finish_expense(expense,chosen):
        if chosen:
            expense.payment_method=account(chosen)['kind']
            db.session.flush()
            try: assign('expense:'+str(expense.id),chosen)
            except (ValueError,TypeError) as error: raise TreasuryLocked(str(error))
    app.extensions['treasury_accounts'].update(expense_account=expense_account,prepare_expense=prepare_expense,finish_expense=finish_expense)

    def total_balance(account_id=None):
        if not ready(): return Decimal(0)
        stmt=select(db.func.coalesce(db.func.sum(postings.c.amount),0))
        if account_id is not None: stmt=stmt.where(postings.c.account_id==account_id)
        return db.session.execute(stmt).scalar()
    app.extensions['treasury_accounts']['total_balance']=total_balance

    def liquid_transfer(end=None):
        if not ready(): return Decimal(0)
        stmt=select(db.func.coalesce(db.func.sum(postings.c.amount),0)).join(accounts,postings.c.account_id==accounts.c.id).where(postings.c.source_key.like('transfer:%'),accounts.c.kind.in_(['Nakit','Banka']))
        if end: stmt=stmt.where(postings.c.date<=end)
        total = db.session.execute(stmt).scalar()
        # Legacy cash-labelled receipts held by a third party are receivables,
        # not cash in our own cashbox/bank. The original receipt stays intact.
        held = select(db.func.coalesce(db.func.sum(postings.c.amount),0)).select_from(postings).join(accounts,postings.c.account_id==accounts.c.id).join(Transaction,postings.c.source_key==('tx:'+db.cast(Transaction.id,db.String))).where(accounts.c.kind=='Tahsilat Sistemi',Transaction.payment_method=='Nakit')
        if end: held=held.where(postings.c.date<=end)
        return total - db.session.execute(held).scalar()
    app.extensions['treasury_accounts']['liquid_transfer']=liquid_transfer
    def enrich(movements):
        if not ready():
            for m in movements: m['account_name']='Hesap Seçilmemiş';m['account_id']=None
            return movements
        amap={a['id']:a for a in rows()}
        pp=[dict(r) for r in db.session.execute(select(postings)).mappings()]
        bykey={p['source_key']:p for p in pp}
        for m in movements:
            p=bykey.get(m.get('source_key'));a=amap.get(p['account_id']) if p else None
            m['account_name']=a['name'] if a else 'Hesap Seçilmemiş';m['account_id']=a['id'] if a else None
        for p in pp:
            if not p['source_key'].startswith('transfer:'): continue
            a=amap[p['account_id']]
            movements.append(dict(date=p['date'],kind=a['kind'],direction='Giriş' if p['amount']>0 else 'Çıkış',description=p['description'],details='Hesaplar arası transfer',party=a['name'],reference=None,due_date=None,status='Gerçekleşti',amount=abs(p['amount']),customer_id=None,sort_time=p['created_at'],source='Transfer',manual_id=None,check_id=None,account_name=a['name'],account_id=a['id']))
        return movements
    app.extensions['treasury_accounts']['enrich']=enrich
    def finish_inner(new_transactions):
        chosen=request.form.get('treasury_account_id','').strip()
        if not ready():
            if chosen: raise ValueError('Önce Kasalar ekranından sistemi hazırlayın.')
            return
        db.session.flush()
        for tx in new_transactions:
            if posted('tx:'+str(tx.id)): continue
            if tx.linked_transaction_id:
                direct=next((a for a in rows() if a['slug']=='direct'),None)
                if direct: assign('tx:'+str(tx.id),direct['id'])
            elif chosen: assign('tx:'+str(tx.id),chosen)
            elif tx.transaction_type=='Ödeme' and tx.payment_method=='Kredi Kartı' and tx.card_owner_type=='Kendi Kartımız' and any(a['kind']=='Kredi Kartı' for a in rows()):
                raise ValueError('Ödemenin yapıldığı kredi kartını seçin.')
    def finish(new_transactions):
        try: finish_inner(new_transactions)
        except (ValueError,TypeError) as e: raise TreasuryLocked(str(e))
    app.extensions['treasury_accounts']['finish']=finish

    # Protect linked amounts and mirrors against independent edits/deletes.
    def guard(session,flush_context,instances):
        if current_app._get_current_object() is not app or session.info.get('treasury_internal'): return
        watched=[o for o in list(session.deleted)+list(session.dirty) if isinstance(o,(Transaction,Expense,Cash)) and o.id and (o in session.deleted or session.is_modified(o,include_collections=False))]
        if not watched or not ready(): return
        for obj in watched:
            key=('tx:' if isinstance(obj,Transaction) else 'expense:' if isinstance(obj,Expense) else 'cash:')+str(obj.id)
            clause=postings.c.source_key==key
            if isinstance(obj,Transaction): clause=or_(clause,postings.c.mirror_id==obj.id)
            if session.execute(select(postings.c.id).where(clause).limit(1)).first():
                raise TreasuryLocked('Bu hareket bir kasaya bağlı. Önce Kasalar ekranında bağlantıyı kaldırın; ardından değiştirin veya silin.')
    # One application module instance is used by the production process.
    event.listen(db.session,'before_flush',guard)
    @app.errorhandler(TreasuryLocked)
    def locked(error):
        db.session.rollback();flash(str(error),'error');return redirect(url_for('treasury_accounts_page'))

    @app.route('/musteriler/<int:customer_id>/cari-hesap/hareket/<int:transaction_id>/duzenle',methods=['GET','POST'])
    def edit_account_transaction(customer_id,transaction_id):
        tx=db.session.execute(select(Transaction).where(Transaction.id==transaction_id,Transaction.customer_id==customer_id).with_for_update()).scalar_one_or_none()
        if not tx or tx.transaction_type not in ('Tahsilat','Ödeme'): abort(404)
        partner=db.session.execute(select(Transaction).where(Transaction.id==tx.linked_transaction_id).with_for_update()).scalar_one_or_none() if tx.linked_transaction_id else None
        key='tx:'+str(tx.id)
        current=posted(key) if ready() else None
        selected=str(current['account_id']) if current else ''
        own=tx.transaction_type=='Ödeme' and tx.card_owner_type=='Kendi Kartımız' and tx.payment_method=='Kredi Kartı'
        kinds={'Mahsup'} if partner else {'Kredi Kartı'} if own else {'Tahsilat Sistemi'} if tx.payment_method=='Kredi Kartı' else {'Nakit','Banka'} if tx.payment_method=='Banka' else {'Nakit','Banka','Tahsilat Sistemi'}
        options=[a for a in choices() if a['kind'] in kinds and a['customer_id']!=tx.customer_id] if ready() and tx.payment_method!='Çek' else []
        def show():
            return render_template('account_transaction_edit.html',transaction=tx,partner=partner,account_options=options,selected_account=selected)
        if request.method=='POST':
            note=request.form.get('description','').strip() or tx.description
            chosen=request.form.get('treasury_account_id',selected).strip()
            if not note or len(note)>240:
                flash('Açıklama 1–240 karakter olmalıdır.','error')
            elif chosen and chosen not in {str(a['id']) for a in options}:
                flash('Bu hareket için uygun bir hesap seçin.','error')
            elif request.form.get('original_account_id',selected)!=selected:
                flash('Hesap bağlantısı değişmiş. Sayfayı yenileyip tekrar seçin.','error')
            elif request.form.get('original_description') != tx.description:
                flash('Bu kayıt değişmiş. Güncel açıklamayı kontrol edip yeniden kaydedin.','error')
            else:
                changes=[(tx,tx.description,note)]
                if partner and partner.linked_transaction_id==tx.id and tx.transaction_type=='Tahsilat':
                    suffix=' · '+tx.description
                    if (partner.description or '').endswith(suffix): changes.append((partner,partner.description,partner.description[:-len(suffix)]+' · '+note))
                if any(len(new)>240 for _,_,new in changes):
                    flash('Bağlı ödeme açıklaması çok uzun olacak. Lütfen daha kısa bir açıklama yazın.','error')
                    return show()
                backup()
                db.session.info['treasury_internal']=True
                try:
                    if chosen!=selected and current: unassign(key)
                    db.session.info['treasury_internal']=True
                    for item,old,new in changes:
                        item.description=new
                        if ready():
                            p=posted('tx:'+str(item.id))
                            if p:
                                db.session.execute(postings.update().where(postings.c.id==p['id']).values(description=new[:500]))
                                if p['mirror_id']:
                                    mirror=db.session.get(Transaction,p['mirror_id'])
                                    if mirror: mirror.description=(account(p['account_id'])['name']+' · '+new)[:240]
                            import json
                            log('edit_description',json.dumps(dict(source='tx:'+str(item.id),old=old,new=new),ensure_ascii=False))
                    if chosen!=selected and chosen:
                        target=account(chosen)
                        if not partner and target['kind'] in ('Nakit','Banka'):
                            tx.payment_method=target['kind']
                        db.session.flush()
                        assign(key,chosen)
                    db.session.commit()
                except ValueError as error:
                    db.session.rollback();flash(str(error),'error');return show()
                except Exception:
                    db.session.rollback();raise
                finally: db.session.info.pop('treasury_internal',None)
                flash('Hareket güncellendi. Tutar korundu; hesap seçiminiz kaydedildi.','success')
                return redirect(return_destination(url_for('customer_account',customer_id=customer_id)))
        return show()

    @app.route('/kasa-cek/kasalar',methods=['GET','POST'])
    def treasury_accounts_page():
        if request.method=='POST':
            action=request.form.get('action')
            try:
                if action=='setup':
                    backup()
                    for t in tables: t.create(bind=db.session.connection(),checkfirst=True)
                    existing={a['slug'] for a in rows()}
                    for slug,name,kind in DEFAULTS:
                        if slug not in existing: db.session.execute(accounts.insert().values(slug=slug,name=name,kind=kind,created_at=datetime.utcnow()))
                    log('setup','Default accounts; no source assignments or linked customers')
                elif not ready(): raise ValueError('Önce kasaları hazırlayın.')
                elif action=='setup_cards':
                    backup();existing={a['slug'] for a in rows()}
                    for slug,name in CARD_DEFAULTS:
                        if slug not in existing: db.session.execute(accounts.insert().values(slug=slug,name=name,kind='Kredi Kartı',created_at=datetime.utcnow()))
                    log('setup_cards','Credit card accounts; no historical assignments')
                elif action=='add_card':
                    name=request.form.get('name','').strip()
                    if not name or len(name)>120: raise ValueError('Kart adını 1–120 karakter olarak girin.')
                    if any(a['name'].casefold()==name.casefold() for a in rows()): raise ValueError('Bu isimde bir hesap zaten var.')
                    backup();db.session.execute(accounts.insert().values(slug='card-'+str(uuid4()),name=name,kind='Kredi Kartı',created_at=datetime.utcnow()));log('add_card',name)
                elif action=='assign':
                    backup();assign(request.form.get('source_key',''),request.form.get('account_id',''))
                elif action=='unassign':
                    backup();unassign(request.form.get('source_key',''))
                elif action=='link':
                    a=account(request.form.get('account_id',''))
                    if a['kind']!='Tahsilat Sistemi': raise ValueError('Cari yalnızca tahsilat sistemine bağlanabilir.')
                    if db.session.execute(select(postings.c.id).where(postings.c.account_id==a['id']).limit(1)).first(): raise ValueError('Cariyi değiştirmeden önce bu hesaptaki hareket bağlantılarını kaldırın.')
                    cid=request.form.get('customer_id',type=int)
                    if cid and not db.session.get(Customer,cid): raise ValueError('Cari bulunamadı.')
                    if cid and db.session.execute(select(accounts.c.id).where(accounts.c.customer_id==cid,accounts.c.id!=a['id'])).first(): raise ValueError('Bu cari başka bir hesaba bağlı.')
                    backup();db.session.execute(accounts.update().where(accounts.c.id==a['id']).values(customer_id=cid));log('link',str(a['id'])+' -> '+str(cid))
                elif action=='transfer':
                    a=account(request.form.get('from_id',''));b=account(request.form.get('to_id',''))
                    if a['id']==b['id'] or 'Mahsup' in (a['kind'],b['kind']): raise ValueError('Farklı iki para hesabı seçin.')
                    n=money(request.form.get('amount',''));day=date.fromisoformat(request.form.get('date',''));desc=request.form.get('description','').strip()
                    if not desc: raise ValueError('Transfer açıklaması girin.')
                    token=request.form.get('token','')
                    from itsdangerous import URLSafeTimedSerializer, BadSignature
                    try: batch=URLSafeTimedSerializer(app.secret_key,salt='treasury-transfer').loads(token,max_age=3600)
                    except BadSignature: raise ValueError('Transfer formunu yenileyin.')
                    backup();add(a,'transfer:'+batch+':out',day,-n,'Transfer → '+b['name']+' · '+desc,batch);add(b,'transfer:'+batch+':in',day,n,'Transfer ← '+a['name']+' · '+desc,batch)
                else: raise ValueError('Geçersiz işlem.')
                db.session.commit();flash('Kasa işlemi kaydedildi.','success')
            except (ValueError,TypeError) as error:
                db.session.rollback();flash(str(error),'error')
            except IntegrityError:
                db.session.rollback();flash('Bu hareket daha önce işlendi veya hesap bağlantısı çakışıyor. Sayfayı yenileyin.','error')
            return redirect(url_for('treasury_accounts_page',account=request.form.get('view_account','')))
        data=rows();entries=[]
        if ready():
            entries=[dict(r) for r in db.session.execute(select(postings).order_by(postings.c.date.desc(),postings.c.created_at.desc())).mappings()]
        for a in data:
            subset=[e for e in entries if e['account_id']==a['id']]
            a['incoming']=sum((e['amount'] for e in subset if e['amount']>0),Decimal(0));a['outgoing']=-sum((e['amount'] for e in subset if e['amount']<0),Decimal(0));a['balance']=a['incoming']-a['outgoing']
            a['customer']=db.session.get(Customer,a['customer_id']) if a['customer_id'] else None
        amap={a['id']:a for a in data};bykey={e['source_key']:e for e in entries}
        pending=[]
        for tx in Transaction.query.options(db.joinedload(Transaction.customer)).filter(Transaction.transaction_type.in_(['Tahsilat','Ödeme']),or_(Transaction.payment_method!='Çek',Transaction.payment_method.is_(None))).all():
            pending.append(dict(key='tx:'+str(tx.id),date=tx.transaction_date,method=tx.payment_method or 'Belirtilmemiş',party=tx.customer.name,description=tx.description,amount=Decimal(tx.credit or 0)-Decimal(tx.debit or 0),direct=bool(tx.linked_transaction_id),own_card=tx.transaction_type=='Ödeme' and tx.payment_method=='Kredi Kartı' and tx.card_owner_type=='Kendi Kartımız'))
        for x in Expense.query.all(): pending.append(dict(key='expense:'+str(x.id),date=x.expense_date,method=x.payment_method,party=x.payee or x.category,description=x.description,amount=-Decimal(x.amount),direct=False,own_card=x.payment_method=='Kredi Kartı'))
        for x in Cash.query.all(): pending.append(dict(key='cash:'+str(x.id),date=x.movement_date,method='Nakit',party='Kasa',description=x.description,amount=Decimal(x.amount)*(1 if x.movement_type=='Giriş' else -1),direct=False,own_card=False))
        for p in pending:
            p['posting']=bykey.get(p['key']);p['account']=amap.get(p['posting']['account_id']) if p['posting'] else None
        pending.sort(key=lambda p:(p['date'],p['key']),reverse=True)
        selected=request.args.get('account','');q=request.args.get('q','').strip()
        from unicodedata import normalize
        def norm(s):return normalize('NFKD',s.replace('ı','i')).encode('ascii','ignore').decode().lower()
        if selected=='unassigned': pending=[p for p in pending if not p['posting']]
        elif selected.isdigit(): pending=[p for p in pending if p['account'] and p['account']['id']==int(selected)]
        if q: pending=[p for p in pending if norm(q) in norm(p['party']+' '+p['description'])]
        transfers=[e for e in entries if e['source_key'].startswith('transfer:') and (not selected or selected.isdigit() and e['account_id']==int(selected))]
        for e in transfers: e['account']=amap[e['account_id']]
        from itsdangerous import URLSafeTimedSerializer
        token=URLSafeTimedSerializer(app.secret_key,salt='treasury-transfer').dumps(str(uuid4()))
        return render_template('treasury_accounts.html',accounts=data,pending=pending,transfers=transfers,selected=selected,query=q,customers=Customer.query.order_by(Customer.name).all(),token=token,today=date.today().isoformat())
