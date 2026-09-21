"""Transactional PostgreSQL change history, private to the web owner.

Triggers capture committed row changes, including bulk SQL and cascading deletes.
There is no business-history UI and no automatic schema work during startup.
"""
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from flask import abort, g, has_request_context, jsonify, render_template, request, session
from sqlalchemy import event, inspect, text

AUDIT_TABLE = "business_audit_event"
EXCLUDED = {"password", "password_hash", "session_token", "secret", "api_key", "token",
            "content", "payload", "failed_attempts", "locked_until"}
WRITE_SQL = re.compile(r"^\s*(INSERT|UPDATE|DELETE|WITH|TRUNCATE)\b", re.I)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS public.business_audit_event (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    actor_id TEXT NOT NULL,
    actor_name TEXT NOT NULL,
    request_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    operation TEXT NOT NULL,
    table_name TEXT NOT NULL,
    record_key JSONB NOT NULL,
    before_data JSONB,
    after_data JSONB,
    changed_fields JSONB NOT NULL,
    transaction_id BIGINT NOT NULL DEFAULT txid_current()
);
CREATE INDEX IF NOT EXISTS ix_bos_audit_time ON public.business_audit_event (occurred_at, id);
CREATE INDEX IF NOT EXISTS ix_bos_audit_actor ON public.business_audit_event (actor_name, id);
CREATE INDEX IF NOT EXISTS ix_bos_audit_record ON public.business_audit_event (table_name, id);
CREATE INDEX IF NOT EXISTS ix_bos_audit_key ON public.business_audit_event USING GIN (record_key);
CREATE INDEX IF NOT EXISTS ix_bos_audit_request ON public.business_audit_event (request_id);
CREATE OR REPLACE FUNCTION public.bos_audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Business audit history is append-only';
END;
$$;
DROP TRIGGER IF EXISTS bos_audit_immutable ON public.business_audit_event;
CREATE TRIGGER bos_audit_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
ON public.business_audit_event FOR EACH STATEMENT EXECUTE FUNCTION public.bos_audit_immutable();
"""


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def trigger_sql(table_name, columns, primary_keys, schema="public", function_schema="public"):
    """Build projections from trusted model metadata; never convert file bytes/secrets to JSON."""
    columns = [name for name in columns if name not in EXCLUDED]
    def projection(prefix, names):
        return "jsonb_build_object(" + ", ".join(
            literal(name) + ", " + prefix + "." + identifier(name) for name in names) + ")"
    fn = identifier(function_schema) + "." + identifier("bos_audit_" + table_name)
    target = identifier(schema) + "." + identifier(table_name)
    password_flag = ""
    if table_name == "web_user":
        password_flag = """
        IF TG_OP = 'UPDATE' AND OLD.password_hash IS DISTINCT FROM NEW.password_hash THEN
            old_row := old_row || jsonb_build_object('password_changed', false);
            new_row := new_row || jsonb_build_object('password_changed', true);
        END IF;
"""
    return f"""
CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE old_row JSONB; new_row JSONB; key_row JSONB; changed JSONB;
BEGIN
    IF TG_OP <> 'INSERT' THEN old_row := {projection('OLD', columns)}; END IF;
    IF TG_OP <> 'DELETE' THEN new_row := {projection('NEW', columns)}; END IF;
    {password_flag}
    IF TG_OP = 'UPDATE' AND old_row IS NOT DISTINCT FROM new_row THEN RETURN NEW; END IF;
    IF TG_OP = 'DELETE' THEN key_row := {projection('OLD', primary_keys)};
    ELSE key_row := {projection('NEW', primary_keys)}; END IF;
    SELECT COALESCE(jsonb_agg(k ORDER BY k), '[]'::jsonb) INTO changed
    FROM (SELECT jsonb_object_keys(COALESCE(old_row, '{{}}'::jsonb) || COALESCE(new_row, '{{}}'::jsonb))) AS keys(k)
    WHERE old_row->k IS DISTINCT FROM new_row->k;
    INSERT INTO public.business_audit_event
        (actor_id, actor_name, request_id, endpoint, operation, table_name, record_key,
         before_data, after_data, changed_fields)
    VALUES (
        COALESCE(NULLIF(current_setting('businessos.actor_id', true), ''), 'system'),
        COALESCE(NULLIF(current_setting('businessos.actor_name', true), ''), 'Sistem / uygulama dışı'),
        COALESCE(current_setting('businessos.request_id', true), ''),
        COALESCE(current_setting('businessos.endpoint', true), ''),
        TG_OP, TG_TABLE_NAME, key_row, old_row, new_row, changed
    );
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END;
$$;
DROP TRIGGER IF EXISTS bos_audit_change ON {target};
CREATE TRIGGER bos_audit_change AFTER INSERT OR UPDATE OR DELETE ON {target}
FOR EACH ROW EXECUTE FUNCTION {fn}();
"""


def bind_actor(engine):
    @event.listens_for(engine, "begin")
    def reset_context(connection):
        connection.info.pop("bos_audit_context", None)

    @event.listens_for(engine, "rollback_savepoint")
    def reset_savepoint_context(connection, name, context):
        connection.info.pop("bos_audit_context", None)

    @event.listens_for(engine, "before_cursor_execute")
    def attach_context(connection, cursor, statement, parameters, context, executemany):
        if not (context.isinsert or context.isupdate or context.isdelete or WRITE_SQL.match(statement)):
            return
        if has_request_context():
            if not getattr(g, "audit_request_id", None):
                g.audit_request_id = uuid4().hex
            owner = getattr(g, "web_is_owner", False)
            name = getattr(g, "web_username", None)
            actor_id = "owner" if owner else ("user:" + str(session.get("web_user_id"))) if name else "system"
            values = (actor_id, name or "Sistem / uygulama dışı", g.audit_request_id, request.endpoint or "")
        else:
            values = ("system", "Sistem / uygulama dışı", "", "")
        if connection.info.get("bos_audit_context") == values:
            return
        # Transaction-local settings cannot leak to another pooled connection user.
        cursor.execute(
            "SELECT set_config('businessos.actor_id', %s, true), "
            "set_config('businessos.actor_name', %s, true), "
            "set_config('businessos.request_id', %s, true), "
            "set_config('businessos.endpoint', %s, true)", values)
        connection.info["bos_audit_context"] = values


def prepare_audit(engine, metadata):
    from web_users import users
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text("SET LOCAL statement_timeout = '20s'"))
        connection.execute(text("SELECT pg_advisory_xact_lock(72109341)"))
        users.create(connection, checkfirst=True)
        connection.exec_driver_sql(SCHEMA_SQL)
        tables = list(metadata.sorted_tables) + [users]
        for table in tables:
            connection.exec_driver_sql(trigger_sql(table.name, [c.name for c in table.columns],
                                                  [c.name for c in table.primary_key]))
        verify_triggers(connection)
        connection.execute(text("""
            INSERT INTO public.business_audit_event
                (actor_id, actor_name, request_id, endpoint, operation, table_name,
                 record_key, changed_fields)
            VALUES (:actor_id, :actor_name, :request_id, 'audit_setup', 'ENABLED', '', '{}', '[]')
        """), {"actor_id": "owner", "actor_name": g.web_username,
               "request_id": uuid4().hex})
    return len(tables)


def verify_triggers(connection):
    """Exercise real PostgreSQL triggers inside a rolled-back savepoint only."""
    savepoint = connection.begin_nested()
    try:
        connection.exec_driver_sql("CREATE TEMP TABLE bos_audit_probe (id INTEGER PRIMARY KEY, amount INTEGER) ON COMMIT DROP")
        connection.exec_driver_sql(trigger_sql("bos_audit_probe", ["id", "amount"], ["id"],
                                              schema="pg_temp", function_schema="pg_temp"))
        connection.execute(text("INSERT INTO bos_audit_probe VALUES (1, 11)"))
        connection.execute(text("UPDATE bos_audit_probe SET amount = 12 WHERE id = 1"))
        connection.execute(text("DELETE FROM bos_audit_probe WHERE id = 1"))
        rows = connection.execute(text(
            "SELECT operation, actor_name, before_data, after_data FROM public.business_audit_event "
            "WHERE table_name = 'bos_audit_probe' AND transaction_id = txid_current() ORDER BY id"
        )).mappings().all()
        if ([r["operation"] for r in rows] != ["INSERT", "UPDATE", "DELETE"] or
                any(r["actor_name"] != g.web_username for r in rows) or
                rows[1]["before_data"]["amount"] != 11 or rows[1]["after_data"]["amount"] != 12):
            raise RuntimeError("İşlem kaydı doğrulaması başarısız; kurulum geri alındı.")
    finally:
        savepoint.rollback()


def audit_status(connection, metadata):
    from web_users import users
    if not inspect(connection).has_table(AUDIT_TABLE):
        return {"enabled": False, "covered_tables": 0}
    expected = {table.name for table in metadata.sorted_tables} | {users.name}
    covered = set(connection.execute(text("""
        SELECT c.relname FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE t.tgname = 'bos_audit_change' AND t.tgenabled = 'O' AND n.nspname = 'public'
    """)).scalars())
    return {"enabled": expected <= covered, "covered_tables": len(covered),
            "missing_tables": sorted(expected - covered)}


def install_audit(app, db):
    if not app.config.get("WEB_AUTH_ENABLED"):
        return
    with app.app_context():
        if db.engine.dialect.name == "postgresql":
            bind_actor(db.engine)

    def owner():
        if not getattr(g, "web_is_owner", False):
            abort(403)
        if db.engine.dialect.name != "postgresql":
            abort(404)

    @app.route("/yonetim/islem-kaydi/kurulum", methods=["GET", "POST"])
    def audit_setup():
        owner()
        if request.method == "POST":
            if request.form.get("operation") != "enable-audit":
                abort(400)
            prepare_audit(db.engine, db.metadata)
        with db.engine.connect() as connection:
            status = audit_status(connection, db.metadata)
        return render_template("audit_setup.html", status=status)

    @app.get("/yonetim/islem-kaydi/kayitlar")
    def audit_records():
        owner()
        after = request.args.get("after", 0, type=int)
        limit = min(max(request.args.get("limit", 100, type=int), 1), 500)
        clauses = ["id > :after"]
        params = {"after": after, "limit": limit}
        for field, column in (("user", "actor_name"), ("actor_id", "actor_id"), ("table", "table_name"),
                              ("request_id", "request_id"), ("operation", "operation")):
            value = request.args.get(field)
            if value:
                clauses.append(column + " = :" + field)
                params[field] = value
        if request.args.get("record"):
            clauses.append("record_key @> CAST(:record AS jsonb)")
            try:
                record = json.loads(request.args["record"])
                if not isinstance(record, dict):
                    abort(400)
            except (ValueError, TypeError):
                abort(400)
            params["record"] = json.dumps(record)
        for field, operator in (("from", ">="), ("to", "<")):
            value = request.args.get(field)
            if value:
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if not parsed.tzinfo:
                        abort(400, "Saat dilimi belirtilmelidir.")
                except ValueError:
                    abort(400)
                key = "time_" + field
                clauses.append("occurred_at " + operator + " :" + key)
                params[key] = parsed
        with db.engine.connect() as connection:
            if not inspect(connection).has_table(AUDIT_TABLE):
                return jsonify(enabled=False, records=[])
            rows = connection.execute(text("SELECT * FROM public.business_audit_event WHERE " +
                " AND ".join(clauses) + " ORDER BY id LIMIT :limit"), params).mappings().all()
        records = [dict(row, occurred_at=row["occurred_at"].astimezone(timezone.utc).isoformat()) for row in rows]
        return jsonify(records=records, next_after=records[-1]["id"] if records else after,
                       limit=limit, time_zone="UTC")
