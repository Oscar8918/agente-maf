"""
Contexto compartido de ejecución para correlación de requests y tools.
"""
import contextvars

# Correlaciona todas las acciones del request actual.
current_thread_id = contextvars.ContextVar("current_thread_id", default="default")
current_user_id = contextvars.ContextVar("current_user_id", default=None)
