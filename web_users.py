"""Restricted web accounts; business data and desktop authentication stay separate."""
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Table, inspect, select
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

# Deliberately outside business metadata: importing a business backup must not
# replace credentials or restore access to a disabled user.
metadata = MetaData()
users = Table("web_user", metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String(80), nullable=False, unique=True),
    Column("password_hash", String(256), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("session_token", String(64), nullable=False),
    Column("failed_attempts", Integer, nullable=False, default=0),
    Column("locked_until", DateTime),
)
PRIVATE_PREFIXES = ("/sahsi-hesaplar", "/yedekler", "/yonetim", "/kullanicilar")


def owner_only_request():
    path = request.url_rule.rule if request.url_rule else request.path
    return any(path == prefix or path.startswith(prefix + "/") for prefix in PRIVATE_PREFIXES)


def ready(connection):
    return inspect(connection).has_table(users.name)


def current_user(db, user_id, token):
    if not isinstance(user_id, int) or not isinstance(token, str):
        return None
    with db.engine.connect() as connection:
        row = connection.execute(select(users.c.id, users.c.username, users.c.active,
                                        users.c.session_token).where(users.c.id == user_id)).mappings().first()
    if row and row["active"] and secrets.compare_digest(row["session_token"], token):
        return row
    return None


def authenticate(db, username, password):
    with db.engine.begin() as connection:
        if not ready(connection):
            return None
        # Serialize concurrent attempts so the lock threshold cannot be lost.
        row = connection.execute(select(users).where(users.c.username == username.casefold())
                                 .with_for_update()).mappings().first()
        if not row or not row["active"]:
            return None
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if row["locked_until"] and row["locked_until"] > now:
            return None
        if not check_password_hash(row["password_hash"], password):
            failures = (0 if row["locked_until"] else row["failed_attempts"]) + 1
            connection.execute(users.update().where(users.c.id == row["id"]).values(
                failed_attempts=failures, locked_until=now + timedelta(minutes=10) if failures >= 5 else None))
            return None
        connection.execute(users.update().where(users.c.id == row["id"]).values(failed_attempts=0, locked_until=None))
        return row


def password_error(password, confirmation):
    if not 16 <= len(password) <= 1000:
        return "Şifre en az 16, en fazla 1000 karakter olmalıdır."
    if password != confirmation:
        return "Şifreler aynı değil."
    return None


def install_user_management(app, db, owner_username):
    @app.route("/kullanicilar", methods=["GET", "POST"])
    def web_users():
        if not g.web_is_owner:
            abort(403)
        error = None
        if request.method == "POST":
            name = request.form.get("username", "").strip().casefold()
            password = request.form.get("password", "")
            error = password_error(password, request.form.get("confirmation", ""))
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,79}", name):
                error = "Kullanıcı adı 3–80 karakter olmalı; İngilizce harf, rakam, nokta, tire veya alt çizgi kullanın."
            elif name == owner_username.casefold():
                error = "Bu kullanıcı adı yönetici hesabına ait."
            if not error:
                try:
                    password_hash = generate_password_hash(password, method="scrypt")
                    with db.engine.begin() as connection:
                        # Explicit owner action only; never DDL during startup or login.
                        users.create(connection, checkfirst=True)
                        connection.execute(users.insert().values(username=name, password_hash=password_hash,
                            active=True, session_token=secrets.token_hex(32)))
                    flash("Kullanıcı oluşturuldu. Şahsi Hesaplar'a erişimi yok; diğer iş bölümlerinde işlem yapabilir.", "success")
                    return redirect(url_for("web_users"))
                except IntegrityError:
                    error = "Bu kullanıcı adı zaten kayıtlı."
        with db.engine.connect() as connection:
            rows = connection.execute(select(users.c.id, users.c.username, users.c.active)
                .order_by(users.c.username)).mappings().all() if ready(connection) else []
        return render_template("web_users.html", users=rows, owner_username=owner_username, error=error), 400 if error else 200

    @app.post("/kullanicilar/<int:user_id>/durum")
    def web_user_status(user_id):
        if not g.web_is_owner:
            abort(403)
        active = request.form.get("active")
        if active not in ("0", "1"):
            abort(400)
        with db.engine.begin() as connection:
            result = connection.execute(users.update().where(users.c.id == user_id).values(
                active=active == "1", session_token=secrets.token_hex(32), failed_attempts=0, locked_until=None))
            if not result.rowcount:
                abort(404)
        flash("Kullanıcı durumu güncellendi; önceki oturumları kapatıldı.", "success")
        return redirect(url_for("web_users"))

    @app.post("/kullanicilar/<int:user_id>/sifre")
    def web_user_password(user_id):
        if not g.web_is_owner:
            abort(403)
        password = request.form.get("password", "")
        error = password_error(password, request.form.get("confirmation", ""))
        if error:
            flash(error, "error")
            return redirect(url_for("web_users"))
        with db.engine.begin() as connection:
            result = connection.execute(users.update().where(users.c.id == user_id).values(
                password_hash=generate_password_hash(password, method="scrypt"),
                session_token=secrets.token_hex(32), failed_attempts=0, locked_until=None))
            if not result.rowcount:
                abort(404)
        flash("Şifre güncellendi; önceki oturumlar kapatıldı.", "success")
        return redirect(url_for("web_users"))
