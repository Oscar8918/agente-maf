"""
Módulo de persistencia PostgreSQL para el Agente MAF.
Incluye historial conversacional, auditoría de tools y métricas operativas.
"""
import json
import os
from typing import Any, Optional

import psycopg2
from psycopg2 import pool
from psycopg2.extras import Json

# Pool de conexiones global
_pool: Optional[pool.SimpleConnectionPool] = None


def _schema_name() -> str:
    """Retorna el schema configurable con un fallback seguro."""
    schema = os.getenv("DB_SCHEMA", "agentes").strip() or "agentes"
    if not schema.replace("_", "").isalnum():
        return "agentes"
    return schema


def _safe_json(value: Any) -> Json:
    """Normaliza cualquier payload a JSON serializable para columnas jsonb."""
    if value is None:
        return Json({})
    try:
        json.dumps(value)
        return Json(value)
    except Exception:
        return Json({"value": str(value)})


def init_db():
    """Inicializa el pool de conexiones y asegura el esquema mínimo."""
    global _pool
    if _pool is not None:
        return

    _pool = pool.SimpleConnectionPool(
        minconn=int(os.getenv("DB_POOL_MIN", "1")),
        maxconn=int(os.getenv("DB_POOL_MAX", "8")),
        dbname=os.getenv("DB_NAME", "elestablo"),
        user=os.getenv("DB_USER", "guillermojuan"),
        password=os.getenv("DB_PASSWORD", ""),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        sslmode=os.getenv("DB_SSLMODE", "prefer"),
    )
    _ensure_schema()
    print("[OK] Pool de conexiones PostgreSQL inicializado")


def close_db():
    """Cierra el pool de conexiones."""
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
        print("[INFO] Pool de conexiones PostgreSQL cerrado")


def is_ready() -> bool:
    """Indica si la capa de BD está disponible."""
    return _pool is not None


def _get_conn():
    """Obtiene una conexión del pool."""
    if _pool is None:
        raise RuntimeError("Pool de conexiones no inicializado. Llama init_db() primero.")
    return _pool.getconn()


def _put_conn(conn):
    """Devuelve una conexión al pool."""
    if _pool:
        _pool.putconn(conn)


def _ensure_schema():
    """Crea esquema, tablas e índices base de forma idempotente."""
    schema = _schema_name()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {schema}.conversaciones (
                    thread_id TEXT PRIMARY KEY,
                    user_id VARCHAR(100),
                    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_activity TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Migraciones para instalaciones existentes (v1 -> v2)
            cur.execute(f"ALTER TABLE {schema}.conversaciones ADD COLUMN IF NOT EXISTS user_id VARCHAR(100)")
            cur.execute(f"ALTER TABLE {schema}.conversaciones ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb")
            cur.execute(
                f"""ALTER TABLE {schema}.conversaciones
                    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"""
            )
            cur.execute(
                f"""ALTER TABLE {schema}.conversaciones
                    ADD COLUMN IF NOT EXISTS last_activity TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"""
            )

            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {schema}.mensajes (
                    id BIGSERIAL PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES {schema}.conversaciones(thread_id) ON DELETE CASCADE,
                    role VARCHAR(20) NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                f"""ALTER TABLE {schema}.mensajes
                    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"""
            )

            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {schema}.tool_events (
                    id BIGSERIAL PRIMARY KEY,
                    thread_id TEXT,
                    user_id VARCHAR(100),
                    tool_name VARCHAR(120) NOT NULL,
                    endpoint VARCHAR(200),
                    operation VARCHAR(80),
                    request_payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    response_status INTEGER,
                    success BOOLEAN NOT NULL DEFAULT FALSE,
                    duration_ms INTEGER,
                    error_text TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(f"ALTER TABLE {schema}.tool_events ADD COLUMN IF NOT EXISTS thread_id TEXT")
            cur.execute(f"ALTER TABLE {schema}.tool_events ADD COLUMN IF NOT EXISTS user_id VARCHAR(100)")
            cur.execute(f"ALTER TABLE {schema}.tool_events ADD COLUMN IF NOT EXISTS tool_name VARCHAR(120)")
            cur.execute(f"ALTER TABLE {schema}.tool_events ADD COLUMN IF NOT EXISTS endpoint VARCHAR(200)")
            cur.execute(f"ALTER TABLE {schema}.tool_events ADD COLUMN IF NOT EXISTS operation VARCHAR(80)")
            cur.execute(
                f"""ALTER TABLE {schema}.tool_events
                    ADD COLUMN IF NOT EXISTS request_payload JSONB NOT NULL DEFAULT '{{}}'::jsonb"""
            )
            cur.execute(f"ALTER TABLE {schema}.tool_events ADD COLUMN IF NOT EXISTS response_status INTEGER")
            cur.execute(f"ALTER TABLE {schema}.tool_events ADD COLUMN IF NOT EXISTS success BOOLEAN NOT NULL DEFAULT FALSE")
            cur.execute(f"ALTER TABLE {schema}.tool_events ADD COLUMN IF NOT EXISTS duration_ms INTEGER")
            cur.execute(f"ALTER TABLE {schema}.tool_events ADD COLUMN IF NOT EXISTS error_text TEXT")
            cur.execute(
                f"""ALTER TABLE {schema}.tool_events
                    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"""
            )

            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {schema}.agent_runs (
                    id BIGSERIAL PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    user_id VARCHAR(100),
                    model_id VARCHAR(100),
                    input_message TEXT NOT NULL,
                    output_message TEXT,
                    success BOOLEAN NOT NULL DEFAULT FALSE,
                    duration_ms INTEGER,
                    error_text TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(f"ALTER TABLE {schema}.agent_runs ADD COLUMN IF NOT EXISTS thread_id TEXT")
            cur.execute(f"ALTER TABLE {schema}.agent_runs ADD COLUMN IF NOT EXISTS user_id VARCHAR(100)")
            cur.execute(f"ALTER TABLE {schema}.agent_runs ADD COLUMN IF NOT EXISTS model_id VARCHAR(100)")
            cur.execute(f"ALTER TABLE {schema}.agent_runs ADD COLUMN IF NOT EXISTS input_message TEXT")
            cur.execute(f"ALTER TABLE {schema}.agent_runs ADD COLUMN IF NOT EXISTS output_message TEXT")
            cur.execute(f"ALTER TABLE {schema}.agent_runs ADD COLUMN IF NOT EXISTS success BOOLEAN NOT NULL DEFAULT FALSE")
            cur.execute(f"ALTER TABLE {schema}.agent_runs ADD COLUMN IF NOT EXISTS duration_ms INTEGER")
            cur.execute(f"ALTER TABLE {schema}.agent_runs ADD COLUMN IF NOT EXISTS error_text TEXT")
            cur.execute(
                f"""ALTER TABLE {schema}.agent_runs
                    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"""
            )

            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_conv_last_activity ON {schema}.conversaciones(last_activity)")
            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_msg_thread_created ON {schema}.mensajes(thread_id, created_at)")
            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_tool_events_created ON {schema}.tool_events(created_at)")
            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_tool_events_thread ON {schema}.tool_events(thread_id)")
            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON {schema}.agent_runs(created_at)")
            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_agent_runs_thread ON {schema}.agent_runs(thread_id)")
        conn.commit()
    finally:
        _put_conn(conn)


# ─── Conversaciones ──────────────────────────────────────────────

def create_conversation(thread_id: str, user_id: Optional[str] = None, metadata: Optional[dict] = None):
    """Crea un registro de conversación en la BD."""
    schema = _schema_name()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {schema}.conversaciones (thread_id, user_id, metadata)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (thread_id) DO NOTHING""",
                (thread_id, user_id, _safe_json(metadata)),
            )
        conn.commit()
    finally:
        _put_conn(conn)


def conversation_exists(thread_id: str) -> bool:
    """Verifica si una conversación existe en la BD."""
    schema = _schema_name()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT 1 FROM {schema}.conversaciones WHERE thread_id = %s",
                (thread_id,),
            )
            return cur.fetchone() is not None
    finally:
        _put_conn(conn)


def update_last_activity(thread_id: str):
    """Actualiza la última actividad de una conversación."""
    schema = _schema_name()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE {schema}.conversaciones
                   SET last_activity = CURRENT_TIMESTAMP
                   WHERE thread_id = %s""",
                (thread_id,),
            )
        conn.commit()
    finally:
        _put_conn(conn)


def delete_conversation(thread_id: str) -> bool:
    """Elimina una conversación y todos sus mensajes (CASCADE)."""
    schema = _schema_name()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {schema}.conversaciones WHERE thread_id = %s",
                (thread_id,),
            )
            deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    finally:
        _put_conn(conn)


# ─── Mensajes ────────────────────────────────────────────────────

def save_message(thread_id: str, role: str, content: str, user_id: Optional[str] = None):
    """Guarda un mensaje en la BD. Crea la conversación si no existe."""
    schema = _schema_name()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {schema}.conversaciones (thread_id, user_id)
                   VALUES (%s, %s)
                   ON CONFLICT (thread_id)
                   DO UPDATE SET last_activity = CURRENT_TIMESTAMP""",
                (thread_id, user_id),
            )
            cur.execute(
                f"""INSERT INTO {schema}.mensajes (thread_id, role, content)
                   VALUES (%s, %s, %s)""",
                (thread_id, role, content),
            )
        conn.commit()
    finally:
        _put_conn(conn)


def get_messages(thread_id: str, limit: int = 50) -> list[dict]:
    """
    Obtiene los últimos N mensajes de una conversación.
    Ordenados cronológicamente (más antiguo primero).
    """
    schema = _schema_name()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT role, content FROM {schema}.mensajes
                   WHERE thread_id = %s
                   ORDER BY created_at DESC
                   LIMIT %s""",
                (thread_id, limit),
            )
            rows = cur.fetchall()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    finally:
        _put_conn(conn)


def get_message_count(thread_id: str) -> int:
    """Retorna la cantidad de mensajes en una conversación."""
    schema = _schema_name()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM {schema}.mensajes WHERE thread_id = %s",
                (thread_id,),
            )
            return cur.fetchone()[0]
    finally:
        _put_conn(conn)


def build_history_context(thread_id: str, limit: int = 20) -> str:
    """
    Construye un resumen del historial para reconstruir contexto tras reinicio.
    """
    messages = get_messages(thread_id, limit=limit)
    if not messages:
        return ""

    lines = ["[Contexto: Historial de conversación anterior]"]
    for msg in messages:
        role_label = "Usuario" if msg["role"] == "user" else "Asistente"
        content = msg["content"]
        if len(content) > 500:
            content = content[:500] + "..."
        lines.append(f"{role_label}: {content}")
    lines.append("[Fin del contexto histórico]\n")

    return "\n".join(lines)


# ─── Observabilidad ──────────────────────────────────────────────

def log_tool_event(
    thread_id: Optional[str],
    user_id: Optional[str],
    tool_name: str,
    endpoint: Optional[str],
    operation: Optional[str],
    request_payload: Optional[dict],
    response_status: Optional[int],
    success: bool,
    duration_ms: Optional[int],
    error_text: Optional[str] = None,
):
    """Registra una ejecución de tool sin interrumpir el flujo principal."""
    if _pool is None:
        return
    schema = _schema_name()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {schema}.tool_events
                    (thread_id, user_id, tool_name, endpoint, operation, request_payload,
                     response_status, success, duration_ms, error_text)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    thread_id,
                    user_id,
                    tool_name,
                    endpoint,
                    operation,
                    _safe_json(request_payload),
                    response_status,
                    success,
                    duration_ms,
                    error_text,
                ),
            )
        conn.commit()
    finally:
        _put_conn(conn)


def log_agent_run(
    thread_id: str,
    user_id: Optional[str],
    model_id: Optional[str],
    input_message: str,
    output_message: Optional[str],
    success: bool,
    duration_ms: Optional[int],
    error_text: Optional[str] = None,
):
    """Registra cada corrida del agente principal."""
    if _pool is None:
        return
    schema = _schema_name()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {schema}.agent_runs
                    (thread_id, user_id, model_id, input_message, output_message, success, duration_ms, error_text)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    thread_id,
                    user_id,
                    model_id,
                    input_message,
                    output_message,
                    success,
                    duration_ms,
                    error_text,
                ),
            )
        conn.commit()
    finally:
        _put_conn(conn)


def get_metrics() -> dict:
    """Retorna métricas operativas básicas para endpoint `/metrics`."""
    if _pool is None:
        return {
            "db_connected": False,
            "total_conversations": 0,
            "total_messages": 0,
            "runs_last_24h": 0,
            "failed_runs_last_24h": 0,
            "tool_calls_last_24h": 0,
            "failed_tool_calls_last_24h": 0,
        }

    schema = _schema_name()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {schema}.conversaciones")
            total_conversations = cur.fetchone()[0]

            cur.execute(f"SELECT COUNT(*) FROM {schema}.mensajes")
            total_messages = cur.fetchone()[0]

            cur.execute(
                f"""SELECT COUNT(*),
                           COUNT(*) FILTER (WHERE success = FALSE)
                    FROM {schema}.agent_runs
                    WHERE created_at >= NOW() - INTERVAL '24 hours'"""
            )
            runs_last_24h, failed_runs_last_24h = cur.fetchone()

            cur.execute(
                f"""SELECT COUNT(*),
                           COUNT(*) FILTER (WHERE success = FALSE)
                    FROM {schema}.tool_events
                    WHERE created_at >= NOW() - INTERVAL '24 hours'"""
            )
            tool_calls_last_24h, failed_tool_calls_last_24h = cur.fetchone()

        return {
            "db_connected": True,
            "total_conversations": total_conversations,
            "total_messages": total_messages,
            "runs_last_24h": runs_last_24h,
            "failed_runs_last_24h": failed_runs_last_24h,
            "tool_calls_last_24h": tool_calls_last_24h,
            "failed_tool_calls_last_24h": failed_tool_calls_last_24h,
        }
    finally:
        _put_conn(conn)
