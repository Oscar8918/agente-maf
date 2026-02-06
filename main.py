"""
Servidor HTTP principal del Agente MAF.
Listo para producción en EasyPanel/Docker.
"""
import os
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import uvicorn

# Cargar variables de entorno (override=True para producción)
load_dotenv(override=True)

from agent import create_agent

# FastAPI app
app = FastAPI(
    title="Agente MAF API",
    description="API del Agente Inteligente con Microsoft Agent Framework",
    version="1.0.0",
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

class ChatResponse(BaseModel):
    response: str
    thread_id: str

class HealthResponse(BaseModel):
    status: str
    version: str

# Variables globales
agent = None
threads = {}


@app.on_event("startup")
async def startup_event():
    """Inicializa el agente al arrancar el servidor."""
    global agent
    try:
        agent = await create_agent()
        print("✅ Agente inicializado correctamente")
    except Exception as e:
        print(f"⚠️ Error al inicializar agente: {e}")
        print("El servidor seguirá funcionando, pero /chat dará error hasta configurar OPENAI_API_KEY")


@app.get("/", response_model=HealthResponse)
async def root():
    """Endpoint de salud del servicio."""
    return HealthResponse(
        status="healthy",
        version="1.0.0"
    )


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check para EasyPanel/Docker."""
    return HealthResponse(
        status="healthy" if agent else "degraded",
        version="1.0.0"
    )


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Endpoint principal para chatear con el agente."""
    global agent, threads
    
    if agent is None:
        raise HTTPException(
            status_code=503,
            detail="Agente no disponible. Verifica que OPENAI_API_KEY esté configurado."
        )
    
    try:
        # Obtener o crear thread
        thread_id = request.thread_id or str(id(request))
        
        if thread_id not in threads:
            threads[thread_id] = agent.get_new_thread()
        
        thread = threads[thread_id]
        
        # Ejecutar el agente con streaming
        response_text = ""
        async for chunk in agent.run_stream(request.message, thread=thread):
            if chunk.text:
                response_text += chunk.text
        
        return ChatResponse(
            response=response_text,
            thread_id=thread_id
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error al procesar mensaje: {str(e)}"
        )


@app.delete("/threads/{thread_id}")
async def delete_thread(thread_id: str):
    """Elimina un thread de conversación."""
    global threads
    if thread_id in threads:
        del threads[thread_id]
        return {"message": f"Thread {thread_id} eliminado"}
    raise HTTPException(status_code=404, detail="Thread no encontrado")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    
    print(f"🚀 Iniciando servidor en {host}:{port}")
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=os.getenv("DEBUG", "false").lower() == "true"
    )
