import unittest

from flask import Flask, g
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

from request_metrics import install_request_metrics


class RequestMetricsTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:')
        self.db = SQLAlchemy(self.app)
        install_request_metrics(self.app, self.db)

        @self.app.get('/page/<role>')
        def page(role):
            g.web_is_owner = role == 'owner'
            self.db.session.execute(text('SELECT 42'))
            self.db.session.execute(text('SELECT 43'))
            return 'ok'

        self.client = self.app.test_client()

    def test_only_opted_in_owner_receives_aggregate_timings(self):
        for path in ('/page/owner', '/page/user?_perf=1'):
            self.assertNotIn('Server-Timing', self.client.get(path).headers)
        response = self.client.get('/page/owner?_perf=1')
        self.assertEqual(response.status_code, 200)
        timing = response.headers['Server-Timing']
        self.assertIn('bos_app;dur=', timing)
        self.assertIn('bos_sql;dur=', timing)
        self.assertIn('bos_queries;desc="2"', timing)
        self.assertNotIn('SELECT', timing)

    def test_counts_do_not_accumulate_between_requests(self):
        for _ in range(2):
            response = self.client.get('/page/owner?_perf=1')
            self.assertIn('bos_queries;desc="2"', response.headers['Server-Timing'])


if __name__ == '__main__':
    unittest.main()
