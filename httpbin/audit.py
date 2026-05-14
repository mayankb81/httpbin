# -*- coding: utf-8 -*-
"""MySQL-backed audit logging for httpbin.

Writes one row per handled request capturing client IP, User-Agent, method,
path, query string, status, response time, and bytes sent. All writes are
batched on a background thread so request latency is unaffected, and a
retention thread prunes old rows.

Disabled (no-op) when AUDIT_DB_URL is unset.
"""

from __future__ import absolute_import

import logging
import os
import queue
import threading
import time

from flask import g, request

log = logging.getLogger("httpbin.audit")

_QUEUE_MAXSIZE = 10_000
_BATCH_MAX = 100
_BATCH_INTERVAL_S = 1.0
_RETRY_SLEEP_S = 5.0
_RETENTION_INTERVAL_S = 3600.0

_engine = None
_table = None
_queue = None
_enabled = False


def _build_db_url():
    """Assemble a SQLAlchemy URL from AUDIT_DB_* env vars.

    Returns the URL object, or None if audit should stay disabled.
    """
    host = os.environ.get("AUDIT_DB_HOST")
    if not host:
        log.info("AUDIT_DB_HOST not set; audit logging disabled")
        return None

    user = os.environ.get("AUDIT_DB_USER")
    password = os.environ.get("AUDIT_DB_PASSWORD")
    if not user or password is None:
        log.warning(
            "AUDIT_DB_HOST is set but AUDIT_DB_USER / AUDIT_DB_PASSWORD are missing; "
            "audit logging disabled"
        )
        return None

    try:
        port = int(os.environ.get("AUDIT_DB_PORT", "3306"))
    except ValueError:
        port = 3306
    database = os.environ.get("AUDIT_DB_NAME", "httpbin_audit")

    from sqlalchemy.engine import URL

    return URL.create(
        drivername="mysql+pymysql",
        username=user,
        password=password,
        host=host,
        port=port,
        database=database,
    )


def init(app):
    """Wire audit logging into a Flask app. Safe to call once at startup."""
    global _engine, _table, _queue, _enabled

    try:
        from sqlalchemy import (
            BigInteger,
            Column,
            DateTime,
            Index,
            Integer,
            MetaData,
            SmallInteger,
            String,
            Table,
            Text,
            create_engine,
            func,
        )
    except ImportError:
        log.warning("sqlalchemy not installed; audit logging disabled")
        return

    db_url = _build_db_url()
    if db_url is None:
        return

    try:
        retention_days = int(os.environ.get("AUDIT_RETENTION_DAYS", "30"))
    except ValueError:
        retention_days = 30

    metadata = MetaData()
    table = Table(
        "audit_logs",
        metadata,
        Column("id", BigInteger, primary_key=True, autoincrement=True),
        Column("ts", DateTime(timezone=False), server_default=func.now(), nullable=False),
        Column("client_ip", String(45)),
        Column("method", String(10)),
        Column("path", String(2048)),
        Column("query_string", Text),
        Column("status", SmallInteger),
        Column("user_agent", String(512)),
        Column("response_time_ms", Integer),
        Column("bytes_sent", Integer),
        Index("idx_audit_logs_ts", "ts"),
    )

    try:
        engine = create_engine(db_url, pool_pre_ping=True, pool_recycle=3600)
        metadata.create_all(engine)
    except Exception as exc:
        log.warning("audit DB init failed (%s); audit logging disabled", exc)
        return

    _engine = engine
    _table = table
    _queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
    _enabled = True

    threading.Thread(
        target=_flusher_loop, name="audit-flusher", daemon=True
    ).start()
    threading.Thread(
        target=_retention_loop,
        name="audit-retention",
        args=(retention_days,),
        daemon=True,
    ).start()

    app.before_request(_on_before_request)
    app.after_request(_on_after_request)

    log.info(
        "audit logging enabled (retention=%dd, queue=%d)",
        retention_days,
        _QUEUE_MAXSIZE,
    )


def is_enabled():
    return _enabled


def fetch_page(limit, offset):
    """Newest-first slice of audit_logs. Returns [] if disabled or on error."""
    if not _enabled:
        return []
    from sqlalchemy import select

    try:
        with _engine.connect() as conn:
            stmt = (
                select(_table)
                .order_by(_table.c.id.desc())
                .limit(limit)
                .offset(offset)
            )
            return [dict(row) for row in conn.execute(stmt).mappings()]
    except Exception:
        log.exception("audit fetch_page failed")
        return []


def fetch_stats():
    """Aggregate stats. Returns zeroed/empty values if disabled or on error."""
    empty = {
        "count_1h": 0,
        "count_24h": 0,
        "status_classes": {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0},
        "top_paths": [],
    }
    if not _enabled:
        return empty

    from sqlalchemy import text

    try:
        with _engine.connect() as conn:
            count_1h = conn.execute(
                text(
                    "SELECT COUNT(*) FROM audit_logs "
                    "WHERE ts > NOW() - INTERVAL 1 HOUR"
                )
            ).scalar() or 0
            count_24h = conn.execute(
                text(
                    "SELECT COUNT(*) FROM audit_logs "
                    "WHERE ts > NOW() - INTERVAL 1 DAY"
                )
            ).scalar() or 0
            class_rows = conn.execute(
                text(
                    "SELECT FLOOR(status/100) AS cls, COUNT(*) AS n "
                    "FROM audit_logs "
                    "WHERE ts > NOW() - INTERVAL 1 DAY "
                    "GROUP BY cls"
                )
            ).all()
            top_paths = conn.execute(
                text(
                    "SELECT path, COUNT(*) AS n FROM audit_logs "
                    "WHERE ts > NOW() - INTERVAL 1 DAY "
                    "GROUP BY path ORDER BY n DESC LIMIT 5"
                )
            ).all()

        classes = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
        for cls, n in class_rows:
            key = "%dxx" % int(cls) if cls else None
            if key in classes:
                classes[key] = int(n)
        return {
            "count_1h": int(count_1h),
            "count_24h": int(count_24h),
            "status_classes": classes,
            "top_paths": [(p, int(n)) for p, n in top_paths],
        }
    except Exception:
        log.exception("audit fetch_stats failed")
        return empty


def _on_before_request():
    g._audit_start = time.monotonic()


def _on_after_request(response):
    if not _enabled:
        return response
    try:
        start = getattr(g, "_audit_start", None)
        elapsed_ms = int((time.monotonic() - start) * 1000) if start else None

        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        if client_ip:
            client_ip = client_ip.split(",")[0].strip()[:45]

        try:
            bytes_sent = response.calculate_content_length()
        except Exception:
            bytes_sent = None

        record = {
            "client_ip": client_ip or None,
            "method": request.method[:10] if request.method else None,
            "path": request.path[:2048] if request.path else None,
            "query_string": request.query_string.decode("utf-8", errors="replace") or None,
            "status": response.status_code,
            "user_agent": (request.headers.get("User-Agent") or "")[:512] or None,
            "response_time_ms": elapsed_ms,
            "bytes_sent": bytes_sent,
        }
        _queue.put_nowait(record)
    except queue.Full:
        log.warning("audit queue full; dropping record")
    except Exception:
        log.exception("audit enqueue failed")
    return response


def _drain_batch():
    batch = []
    deadline = time.monotonic() + _BATCH_INTERVAL_S
    while len(batch) < _BATCH_MAX:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            break
        try:
            batch.append(_queue.get(timeout=timeout))
        except queue.Empty:
            break
    return batch


def _flusher_loop():
    while True:
        try:
            batch = _drain_batch()
            if not batch:
                continue
            with _engine.begin() as conn:
                conn.execute(_table.insert(), batch)
        except Exception:
            log.exception("audit flush failed; dropping batch")
            time.sleep(_RETRY_SLEEP_S)


def _retention_loop(retention_days):
    from sqlalchemy import text

    stmt = text(
        "DELETE FROM audit_logs WHERE ts < (NOW() - INTERVAL :days DAY)"
    )
    while True:
        time.sleep(_RETENTION_INTERVAL_S)
        try:
            with _engine.begin() as conn:
                result = conn.execute(stmt, {"days": retention_days})
                count = getattr(result, "rowcount", None)
                log.info("audit retention pruned %s rows", count)
        except Exception:
            log.exception("audit retention failed")
