"""Authentication checks without a database or production credentials."""
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch
from flask import Flask
from web_auth import install_web_auth

BASE = 'https://example.test'
ENV = {'VERCEL': '1', 'BUSINESSOS_WEB_USERNAME': 'test-owner',
       'BUSINESSOS_WEB_PASSWORD': 'test-only-password-long', 'SECRET_KEY': 'test-only-secret-key-at-least-32-chars'}


def make_app(overrides=None, desktop=False):
    env = dict(ENV)
    env.update(overrides or {})
    if desktop:
        env.pop('VERCEL')
    with patch.dict(os.environ, env, clear=True):
        app = Flask(__name__, template_folder=str(Path(__file__).parent / 'templates'))
        app.config.update(TESTING=True, SECRET_KEY=env['SECRET_KEY'], SQLALCHEMY_DATABASE_URI='sqlite:///:memory:')
        install_web_auth(app)
    app.add_url_rule('/', 'dashboard', lambda: 'private')
    app.add_url_rule('/export', 'export', lambda: 'private export')
    app.add_url_rule('/write', 'write', lambda: 'written', methods=['POST'])
    return app


def login(client, password=ENV['BUSINESSOS_WEB_PASSWORD']):
    response = client.get('/giris', base_url=BASE)
    token = re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1]
    return client.post('/giris', base_url=BASE, data={'csrf_token': token, 'username': ENV['BUSINESSOS_WEB_USERNAME'], 'password': password})


class WebAuthTests(unittest.TestCase):
    def test_desktop_unchanged(self):
        app = make_app(desktop=True)
        self.assertFalse(app.config['WEB_AUTH_ENABLED'])
        self.assertEqual(app.test_client().get('/').text, 'private')

    def test_missing_configuration_fails_closed(self):
        for overrides in ({'BUSINESSOS_WEB_PASSWORD': ''}, {'SECRET_KEY': 'short'}, {'BUSINESSOS_WEB_USERNAME': ''}):
            client = make_app(overrides).test_client()
            for path in ('/', '/export', '/giris'):
                self.assertEqual(client.get(path, base_url=BASE).status_code, 503)

    def test_configuration_logs_identify_problem_without_values(self):
        cases = [({'BUSINESSOS_WEB_USERNAME': ''}, 'BUSINESSOS_WEB_USERNAME: missing'),
                 ({'BUSINESSOS_WEB_PASSWORD': ''}, 'BUSINESSOS_WEB_PASSWORD: missing'),
                 ({'BUSINESSOS_WEB_PASSWORD': 'short-test'}, 'BUSINESSOS_WEB_PASSWORD: must contain at least 16'),
                 ({'SECRET_KEY': 'short-secret'}, 'SECRET_KEY: must contain at least 32'),
                 ({'SECRET_KEY': 'development-key-change-in-production'}, 'SECRET_KEY: missing or development default')]
        for overrides, expected in cases:
            app = make_app(overrides)
            with self.assertLogs(app.logger, level='ERROR') as logs:
                response = app.test_client().get('/', base_url=BASE)
            output = '\n'.join(logs.output)
            self.assertIn('BUSINESSOS_AUTH_CONFIG', output)
            self.assertIn(expected, output)
            self.assertEqual(response.status_code, 503)
            self.assertIn(expected, response.text)
            for value in dict(ENV, **overrides).values():
                if len(value) > 2:
                    self.assertNotIn(value, output)

    def test_all_routes_guarded_and_csrf(self):
        client = make_app().test_client()
        for path in ('/', '/export', '/unknown', '/static/style.css'):
            self.assertEqual(client.get(path, base_url=BASE).location, '/giris')
        self.assertEqual(client.post('/write', base_url=BASE).status_code, 401)
        stale = client.post('/giris', base_url=BASE)
        self.assertEqual(stale.status_code, 400)
        self.assertIn('Giriş sayfası yenilendi', stale.text)
        self.assertRegex(stale.text, r'name="csrf_token" value="[^"]+"')
        self.assertEqual(login(client, 'wrong').status_code, 401)
        self.assertEqual(client.get('/', base_url=BASE).status_code, 302)

    def test_login_cookie_write_protection_logout(self):
        app = make_app()
        client = app.test_client()
        response = login(client)
        self.assertEqual(response.status_code, 302)
        cookie = response.headers['Set-Cookie']
        for flag in ('Secure', 'HttpOnly', 'SameSite=Strict', '__Host-businessos'):
            self.assertIn(flag, cookie)
        self.assertEqual(client.get('/', base_url=BASE).text, 'private')
        self.assertEqual(client.post('/write', base_url=BASE).status_code, 403)
        self.assertEqual(client.post('/write', base_url=BASE, headers={'Origin': 'https://attacker.test'}).status_code, 403)
        self.assertEqual(client.post('/write', base_url=BASE, headers={'Origin': BASE}).text, 'written')
        self.assertEqual(client.post('/cikis', base_url=BASE, headers={'Origin': BASE}).status_code, 302)
        self.assertEqual(client.get('/', base_url=BASE).status_code, 302)
        self.assertEqual(app.config['PERMANENT_SESSION_LIFETIME'].total_seconds(), 28800)

    def test_rotated_password_rejects_old_session(self):
        client = make_app().test_client()
        login(client)
        cookie = client.get_cookie('__Host-businessos', domain='example.test')
        other = make_app({'BUSINESSOS_WEB_PASSWORD': 'another-test-password-long'}).test_client()
        other.set_cookie('__Host-businessos', cookie.value, domain='example.test')
        self.assertEqual(other.get('/', base_url=BASE).status_code, 302)


if __name__ == '__main__':
    unittest.main()
