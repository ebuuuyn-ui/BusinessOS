"""Owner and restricted web login; desktop SQLite starts without authentication."""
import hashlib
import hmac
import os
from datetime import timedelta
from urllib.parse import urlsplit

from flask import abort, g, redirect, render_template, request, session, url_for


def install_web_auth(app, db=None):
    uri = app.config['SQLALCHEMY_DATABASE_URI']
    enabled = bool(os.getenv('VERCEL')) or uri.startswith(('postgresql', 'postgres:'))
    app.config['WEB_AUTH_ENABLED'] = enabled
    if not enabled:
        return

    username = os.getenv('BUSINESSOS_WEB_USERNAME', '').strip()
    password = os.getenv('BUSINESSOS_WEB_PASSWORD', '')
    secret = app.config.get('SECRET_KEY') or ''
    configuration_errors = []
    if not username:
        configuration_errors.append('BUSINESSOS_WEB_USERNAME: missing or blank')
    if not password:
        configuration_errors.append('BUSINESSOS_WEB_PASSWORD: missing or blank')
    elif len(password) < 16:
        configuration_errors.append('BUSINESSOS_WEB_PASSWORD: must contain at least 16 characters')
    if not secret or secret == 'development-key-change-in-production':
        configuration_errors.append('SECRET_KEY: missing or development default')
    elif len(secret) < 32:
        configuration_errors.append('SECRET_KEY: must contain at least 32 characters')
    configured = not configuration_errors
    fingerprint = hmac.new(str(secret).encode(), (username + '\0' + password).encode(), hashlib.sha256).hexdigest()
    app.config.update(SESSION_COOKIE_NAME='__Host-businessos', SESSION_COOKIE_SECURE=True,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                      SESSION_COOKIE_PATH='/', SESSION_COOKIE_DOMAIN=None,
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=8), SESSION_REFRESH_EACH_REQUEST=False)

    @app.before_request
    def require_web_login():
        # Fail closed, including when a deployment is missing its credentials.
        if not configured:
            # Never log credential values, their hashes, or request data.
            app.logger.error('BUSINESSOS_AUTH_CONFIG: %s', '; '.join(configuration_errors))
            return render_template(
                'web_login.html',
                unavailable=True,
                setup_errors=configuration_errors,
            ), 503
        g.web_is_owner = False
        if request.endpoint == 'web_login':
            return None
        # OAuth GET only displays a same-origin continuation form; it cannot exchange tokens.
        # Keep the main session cookie Strict. The authenticated POST completes the flow.
        if request.endpoint == 'supplier_chat_callback' and request.method == 'GET':
            return None
        g.web_is_owner = hmac.compare_digest(str(session.get('web_auth', '')), fingerprint)
        user = None
        if not g.web_is_owner and db is not None and session.get('web_user_id'):
            from web_users import current_user
            user = current_user(db, session.get('web_user_id'), session.get('web_user_token'))
        if not g.web_is_owner and user is None:
            session.clear()
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                abort(401)
            return redirect(url_for('web_login'))
        g.web_username = username if g.web_is_owner else user["username"]
        if not g.web_is_owner:
            from web_users import owner_only_request
            if owner_only_request():
                abort(403)
        # Existing forms and fetch requests send Origin in modern browsers.
        # Reject cross-site and origin-less writes before any application handler.
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = urlsplit(request.headers.get('Origin', ''))
            if origin.scheme != 'https' or origin.netloc != request.host:
                abort(403)

    @app.after_request
    def private_web_response(response):
        response.headers['Cache-Control'] = 'no-store, private'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "frame-ancestors 'self'; form-action 'self'; base-uri 'self'"
        return response

    @app.route('/giris', methods=['GET', 'POST'])
    def web_login():
        error = None
        if request.method == 'POST':
            # Reject explicit cross-site form submissions. Login does not rely
            # on a pre-login session cookie, which some privacy modes discard.
            source = urlsplit(request.headers.get('Origin') or request.headers.get('Referer') or '')
            if source.netloc and (source.scheme != 'https' or source.netloc != request.host):
                abort(403)
            supplied_user = request.form.get('username', '').encode()
            supplied_password = request.form.get('password', '').encode()
            user_ok = hmac.compare_digest(hashlib.sha256(supplied_user).digest(), hashlib.sha256(username.encode()).digest())
            password_ok = hmac.compare_digest(hashlib.sha256(supplied_password).digest(), hashlib.sha256(password.encode()).digest())
            if user_ok and password_ok:
                session.clear()
                session.permanent = True
                session['web_auth'] = fingerprint
                return redirect(url_for('dashboard'))
            if db is not None and not user_ok:
                from web_users import authenticate
                user = authenticate(db, request.form.get('username', '').strip(),
                                    request.form.get('password', ''))
                if user:
                    session.clear()
                    session.permanent = True
                    session['web_user_id'] = user['id']
                    session['web_user_token'] = user['session_token']
                    return redirect(url_for('dashboard'))
            error = 'Kullanıcı adı veya şifre hatalı. Çok sayıda hatalı denemeden sonra 10 dakika bekleyin.'
        return render_template('web_login.html', error=error), 401 if error else 200

    @app.post('/cikis')
    def web_logout():
        session.clear()
        return redirect(url_for('web_login'))

    if db is not None:
        from web_users import install_user_management
        install_user_management(app, db, username)
