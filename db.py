"""
Módulo de persistencia PostgreSQL para el Agente MAF.
Gestiona el historial de conversaciones y mensajes.
"""
import os
import psycopg2
from psycopg2 import pool
from typing import Optional

# Pool de conexiones global
_pool: Optional[pool.SimpleConnectionPool] = None


def init_db():
    """Inicializa el pool de conexiones a PostgreSQL."""
    global _pool
    if _pool is not None:
        return

    _pool = pool.SimpleConnectionPool(
        minconn=1,
        maxconn=5,
        dbname=os.getenv("DB_NAME", "elestablo"),
        user=os.getenv("DB_USER", "guillermojuan"),
        password=os.getenv("DB_PASSWORD", ""),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
    )
    print("✅ Pool de conexiones PostgreSQL inicializado")


def close_db():
    """Cierra el pool de conexiones."""
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
        print("🔒 Pool de conexiones PostgreSQL cerrado")


def _get_conn():
    """Obtiene una conexión del pool."""
    if _pool is None:
        raise RuntimeError("Pool de conexiones no inicializado. Llama init_db() primero.")
    return _pool.getconn()


def _put_conn(conn):
    """Devuelve una conexión al pool."""
    if _pool:
        _pool.putconn(conn)


# ─── Conversaciones ──────────────────────────────────────────────

def create_conversation(thread_id: str):
    """Crea un registro de conversación en la BD."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO agentes.conversaciones (thread_id)
                   VALUES (%s)
                   ON CONFLICT (thread_id) DO NOTHING""",
                (thread_id,),
            )
        conn.commit()
    finally:
        _put_conn(conn)


def conversation_exists(thread_id: str) -> bool:
    """Verifica si una conversación existe en la BD."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM agentes.conversaciones WHERE thread_id = %s",
                (thread_id,),
            )
            return cur.fetchone() is not None
    finally:
        _put_conn(conn)


def update_last_activity(thread_id: str):
    """Actualiza la última actividad de una conversación."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE agentes.conversaciones
                   SET last_activity = CURRENT_TIMESTAMP
                   WHERE thread_id = %s""",
                (thread_id,),
            )
        conn.commit()
    finally:
        _put_conn(conn)


def delete_conversation(thread_id: str) -> bool:
    """Elimina una conversación y todos sus mensajes (CASCADE)."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agentes.conversaciones WHERE thread_id = %s",
                (thread_id,),
            )
            deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    finally:
        _put_conn(conn)


# ─── Mensajes ────────────────────────────────────────────────────

def save_message(thread_id: str, role: str, content: str):
    """Guarda un mensaje en la BD. Crea la conversación si no existe."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            # Asegurar que existe la conversación
            cur.execute(
                """INSERT INTO agentes.conversaciones (thread_id)
                   VALUES (%s)
                   ON CONFLICT (thread_id)
                   DO UPDATE SET last_activity = CURRENT_TIMESTAMP""",
                (thread_id,),
            )
            # Insertar mensaje
            cur.execute(
                """INSERT INTO agentes.mensajes (thread_id, role, content)
                   VALUES (%s, %s, %s)""",
                (thread_id, role, content),
            )
        conn.commit()
    finally:
        _put_conn(conn)


def get_messages(thread_id: str, limit: int = 50) -> list[dict]:
    """
    Obtiene los últimos N mensajes de una conversación.
    Retorna lista de dicts: [{"role": "user"|"assistant", "content": "..."}]
    Ordenados cronológicamente (más antiguo primero).
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT role, content FROM agentes.mensajes
                   WHERE thread_id = %s
                   ORDER BY created_at DESC
                   LIMIT %s""",
                (thread_id, limit),
            )
            rows = cur.fetchall()
        # Invertir para orden cronológico
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    finally:
        _put_conn(conn)


def get_message_count(thread_id: str) -> int:
    """Retorna la cantidad de mensajes en una conversación."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM agentes.mensajes WHERE thread_id = %s",
                (thread_id,),
            )
            return cur.fetchone()[0]
    finally:
        _put_conn(conn)


def build_history_context(thread_id: str, limit: int = 20) -> str:
    """
    Construye un resumen del historial de conversación para inyectar
    como contexto cuando se reconstruye un thread perdido (e.g. tras restart).
    Retorna string vacío si no hay historial.
    """
    messages = get_messages(thread_id, limit=limit)
    if not messages:
        return ""

    lines = ["[Contexto: Historial de conversación anterior]"]
    for msg in messages:
        role_label = "Usuario" if msg["role"] == "user" else "Asistente"
        # Truncar mensajes largos en el contexto
        content = msg["content"]
        if len(content) > 500:
            content = content[:500] + "..."
        lines.append(f"{role_label}: {content}")
    lines.append("[Fin del contexto histórico]\n")

    return "\n".join(lines)
