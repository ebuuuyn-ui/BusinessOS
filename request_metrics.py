"""Opt-in, owner-only page timings; never records SQL or business data."""
from time import perf_counter

from flask import g, has_request_context, request
from sqlalchemy import event


def install_request_metrics(app, db):
    @app.before_request
    def start_metrics():
        if request.method == 'GET' and request.args.get('_perf') == '1':
            g.page_metrics = {'started': perf_counter(), 'sql_ms': 0.0, 'queries': 0}

    def before_cursor(conn, cursor, statement, parameters, context, executemany):
        if has_request_context() and getattr(g, 'page_metrics', None) is not None:
            context._bos_query_started = perf_counter()

    def after_cursor(conn, cursor, statement, parameters, context, executemany):
        metrics = getattr(g, 'page_metrics', None) if has_request_context() else None
        started = getattr(context, '_bos_query_started', None)
        if metrics is not None and started is not None:
            metrics['sql_ms'] += (perf_counter() - started) * 1000
            metrics['queries'] += 1

    with app.app_context():
        event.listen(db.engine, 'before_cursor_execute', before_cursor)
        event.listen(db.engine, 'after_cursor_execute', after_cursor)

    @app.after_request
    def finish_metrics(response):
        metrics = getattr(g, 'page_metrics', None)
        if metrics is not None and getattr(g, 'web_is_owner', False):
            total_ms = (perf_counter() - metrics['started']) * 1000
            response.headers.add('Server-Timing', (
                f'bos_app;dur={total_ms:.2f}, bos_sql;dur={metrics["sql_ms"]:.2f}, '
                f'bos_queries;desc="{metrics["queries"]}"'
            ))
        return response
