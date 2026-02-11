"""
Servidor HTTP principal del Agente MAF.
Listo para producción en EasyPanel/Docker.
Con persistencia PostgreSQL para historial de conversaciones.
"""
import os
import asyncio
import time
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import uvicorn

# Cargar variables de entorno (override=True para producción)
load_dotenv(override=True)

from agent import create_agent
from siigo_agent import reset_siigo_agent, reset_siigo_thread
from runtime_context import current_thread_id, current_user_id
import db

# Variables globales
agent = None
threads = {}
APP_VERSION = "1.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa el agente y la BD al arrancar, limpia al cerrar."""
    global agent
    try:
        db.init_db()
        print("[OK] Conexion a PostgreSQL establecida")
    except Exception as e:
        print(f"[WARN] Error al conectar con PostgreSQL: {e}")
        print("El servidor funcionará SIN persistencia de historial")
    try:
        agent = await create_agent()
        print("[OK] Agente principal inicializado correctamente")
        print("[OK] Sub-agente SIIGO listo (se inicializa en primera consulta)")
    except Exception as e:
        print(f"[WARN] Error al inicializar agente: {e}")
        print("El servidor seguirá funcionando, pero /chat dará error hasta configurar OPENAI_API_KEY")
    yield
    # Cleanup
    agent = None
    threads.clear()
    reset_siigo_agent()
    db.close_db()


# FastAPI app
app = FastAPI(
    title="Agente MAF API",
    description="API del Agente Inteligente con Microsoft Agent Framework",
    version=APP_VERSION,
    lifespan=lifespan,
)

# CORS para permitir requests desde cualquier origen
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Modelos de datos
class ChatRequest(BaseModel):
    message: str
    thread_id: Optional[str] = None
    user_id: Optional[str] = None

class ChatResponse(BaseModel):
    response: str
    thread_id: str

class HealthResponse(BaseModel):
    status: str
    version: str
    db_connected: bool


@app.get("/", response_model=HealthResponse)
async def root():
    """Endpoint de salud del servicio."""
    return HealthResponse(
        status="healthy",
        version=APP_VERSION,
        db_connected=db.is_ready(),
    )


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check para EasyPanel/Docker."""
    return HealthResponse(
        status="healthy" if agent else "degraded",
        version=APP_VERSION,
        db_connected=db.is_ready(),
    )


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Endpoint principal para chatear con el agente."""
    global agent, threads
    thread_id = request.thread_id or str(id(request))
    user_id = request.user_id
    started_at = time.perf_counter()
    
    if agent is None:
        raise HTTPException(
            status_code=503,
            detail="Agente no disponible. Verifica que OPENAI_API_KEY esté configurado."
        )
    
    try:
        # Obtener o crear thread
        if thread_id not in threads:
            threads[thread_id] = agent.get_new_thread()
            
            # Si existe historial en BD, inyectar contexto previo
            try:
                if db._pool and db.conversation_exists(thread_id):
                    history_ctx = db.build_history_context(thread_id, limit=20)
                    if history_ctx:
                        # Enviar historial como primer mensaje para reconstruir contexto
                        async for _ in agent.run_stream(history_ctx, thread=threads[thread_id]):
                            pass  # Solo inyectamos el contexto, descartamos la respuesta
            except Exception as e:
                print(f"[WARN] No se pudo cargar historial de BD para {thread_id}: {e}")
        
        thread = threads[thread_id]
        
        # Setear thread_id en contextvars para que las tools lo usen
        current_thread_id.set(thread_id)
        current_user_id.set(user_id)
        
        # Ejecutar el agente con streaming
        response_text = ""
        async for chunk in agent.run_stream(request.message, thread=thread):
            if chunk.text:
                response_text += chunk.text
        
        # Persistir mensajes en PostgreSQL
        try:
            if db.is_ready():
                db.save_message(thread_id, "user", request.message, user_id=user_id)
                db.save_message(thread_id, "assistant", response_text, user_id=user_id)
        except Exception as e:
            print(f"[WARN] Error al guardar mensajes en BD: {e}")

        # Trazabilidad de ejecución del agente
        try:
            if db.is_ready():
                db.log_agent_run(
                    thread_id=thread_id,
                    user_id=user_id,
                    model_id=os.getenv("MODEL_ID", "gpt-4o-mini"),
                    input_message=request.message,
                    output_message=response_text,
                    success=True,
                    duration_ms=int((time.perf_counter() - started_at) * 1000),
                )
        except Exception as e:
            print(f"[WARN] Error al guardar run del agente: {e}")
        
        return ChatResponse(
            response=response_text,
            thread_id=thread_id
        )
        
    except Exception as e:
        try:
            if db.is_ready():
                db.log_agent_run(
                    thread_id=thread_id,
                    user_id=user_id,
                    model_id=os.getenv("MODEL_ID", "gpt-4o-mini"),
                    input_message=request.message,
                    output_message=None,
                    success=False,
                    duration_ms=int((time.perf_counter() - started_at) * 1000),
                    error_text=str(e),
                )
        except Exception as log_error:
            print(f"[WARN] Error al guardar run fallido: {log_error}")
        raise HTTPException(
            status_code=500,
            detail=f"Error al procesar mensaje: {str(e)}"
        )


@app.get("/metrics")
async def metrics():
    """Métricas operativas básicas del agente."""
    return {
        "status": "ok",
        "version": APP_VERSION,
        **db.get_metrics(),
    }


@app.delete("/threads/{thread_id}")
async def delete_thread(thread_id: str):
    """Elimina un thread de conversación (memoria + BD)."""
    global threads
    deleted_mem = False
    deleted_db = False

    if thread_id in threads:
        del threads[thread_id]
        deleted_mem = True

    # Eliminar thread del sub-agente SIIGO
    reset_siigo_thread(thread_id)

    # Eliminar de la BD
    try:
        if db._pool:
            deleted_db = db.delete_conversation(thread_id)
    except Exception as e:
        print(f"[WARN] Error al eliminar conversación de BD: {e}")

    if deleted_mem or deleted_db:
        return {"message": f"Thread {thread_id} eliminado"}
    raise HTTPException(status_code=404, detail="Thread no encontrado")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    
    print(f"[INFO] Iniciando servidor en {host}:{port}")
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=os.getenv("DEBUG", "false").lower() == "true"
    )
