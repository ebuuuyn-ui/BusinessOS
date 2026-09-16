"""Hosted startup must not connect, migrate or touch local backup folders."""
import os
import tempfile
import unittest
from unittest.mock import patch
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

_data=tempfile.TemporaryDirectory(prefix='businessos-startup-tests-')
os.environ.update(BUSINESSOS_DATA_DIR=_data.name,BUSINESSOS_BACKUP_DIR=_data.name,DATABASE_URL='sqlite:///:memory:')
import app as m

ENV={'VERCEL':'1','BUSINESSOS_WEB_USERNAME':'test-owner','BUSINESSOS_WEB_PASSWORD':'test-only-password-long','SECRET_KEY':'test-only-secret-key-at-least-32-chars'}

class WebStartupTests(unittest.TestCase):
    def test_web_startup_makes_no_database_connection_or_setup_calls(self):
        for uri in ['sqlite:///:memory:','postgresql+psycopg://unused:unused@127.0.0.1:1/unused']:
            with self.subTest(uri=uri), patch.dict(os.environ,{**ENV,'VERCEL':'1' if uri.startswith('sqlite') else ''}), patch.object(Engine,'connect',side_effect=AssertionError('Startup connected')):
                with patch.object(m.db,'create_all') as create, patch.object(m,'create_database_backup') as backup, patch.object(m,'import_personal_finance_data') as legacy:
                    app=m.create_app({'TESTING':True,'SQLALCHEMY_DATABASE_URI':uri})
                    self.assertTrue(app.config['WEB_AUTH_ENABLED'])
                    create.assert_not_called();backup.assert_not_called();legacy.assert_not_called()
    def test_web_requests_skip_local_backups_and_cli_is_explicit(self):
        with patch.dict(os.environ,ENV):app=m.create_app({'TESTING':True,'SQLALCHEMY_DATABASE_URI':'sqlite:///:memory:'})
        with app.app_context():self.assertEqual(inspect(m.db.engine).get_table_names(),[])
        with patch.object(m,'ensure_scheduled_backups') as backups:
            client=app.test_client()
            r=client.post('/giris',base_url='https://example.test',headers={'Origin':'https://example.test'},data={'username':ENV['BUSINESSOS_WEB_USERNAME'],'password':ENV['BUSINESSOS_WEB_PASSWORD']})
            self.assertEqual(r.status_code,302)
            with client.get('/static/collection-tracking.js',base_url='https://example.test') as response:
                self.assertEqual(response.status_code,200)
            backups.assert_not_called()
        runner=app.test_cli_runner()
        self.assertEqual(runner.invoke(args=['init-db']).exit_code,0)
        with app.app_context():
            m.db.session.add(m.Customer(name='Preserved'));m.db.session.commit()
        self.assertEqual(runner.invoke(args=['init-db']).exit_code,0)
        with app.app_context():self.assertEqual(m.Customer.query.one().name,'Preserved')
    def test_desktop_keeps_setup_and_scheduled_backup(self):
        with patch.dict(os.environ,{'VERCEL':''}), patch.object(m,'create_database_backup',wraps=m.create_database_backup) as backup, patch.object(m,'import_personal_finance_data',wraps=m.import_personal_finance_data) as legacy:
            app=m.create_app({'TESTING':True,'SQLALCHEMY_DATABASE_URI':'sqlite:///:memory:'})
            self.assertFalse(app.config['WEB_AUTH_ENABLED']);backup.assert_called_once_with(app,'startup');legacy.assert_called_once_with(app)
        with app.app_context():self.assertIn('customer',inspect(m.db.engine).get_table_names())
        with patch.object(m,'ensure_scheduled_backups') as backups:
            self.assertEqual(app.test_client().get('/').status_code,200);backups.assert_called_once_with(app)
if __name__=='__main__':unittest.main()
