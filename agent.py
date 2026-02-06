"""
Agente principal usando Microsoft Agent Framework con OpenAI API.
Delega operaciones de Siigo Nube al sub-agente SIIGO especializado.
"""
import os
import asyncio
import contextvars
from typing import Annotated
from agent_framework import ChatAgent
from agent_framework.openai import OpenAIChatClient
from siigo_agent import run_siigo_agent

# ContextVar para pasar el thread_id actual a las tools del agente
current_thread_id = contextvars.ContextVar("current_thread_id", default="default")


# ==================== HERRAMIENTAS GENERALES ====================

def get_weather(
    location: Annotated[str, "La ubicación para obtener el clima."],
) -> str:
    """Obtiene el clima para una ubicación dada."""
    import random
    conditions = ["soleado", "nublado", "lluvioso", "tormentoso"]
    return f"El clima en {location} es {conditions[random.randint(0, 3)]} con una temperatura máxima de {random.randint(15, 35)}°C."


def search_web(
    query: Annotated[str, "La consulta de búsqueda."],
) -> str:
    """Busca información en la web (simulado)."""
    return f"Resultados de búsqueda para '{query}': Se encontraron varios artículos relevantes sobre el tema."


def calculate(
    expression: Annotated[str, "La expresión matemática a calcular."],
) -> str:
    """Calcula una expresión matemática."""
    try:
        # Evaluación segura de expresiones matemáticas
        allowed_chars = set("0123456789+-*/().% ")
        if all(c in allowed_chars for c in expression):
            result = eval(expression)
            return f"El resultado de {expression} es: {result}"
        else:
            return "Error: Expresión no válida. Solo se permiten operaciones matemáticas básicas."
    except Exception as e:
        return f"Error al calcular: {str(e)}"


def get_current_time() -> str:
    """Obtiene la fecha y hora actual."""
    from datetime import datetime
    now = datetime.now()
    return f"La fecha y hora actual es: {now.strftime('%d/%m/%Y %H:%M:%S')}"


# ==================== TOOL DEL SUB-AGENTE SIIGO ====================

async def consultar_siigo_erp(
    consulta: Annotated[str, "Descripción detallada de lo que se necesita hacer en Siigo Nube. Incluir toda la información relevante: módulo (clientes, productos, facturas, etc.), operación (listar, crear, editar, consultar, eliminar), y datos necesarios (IDs, nombres, filtros, campos del registro, etc.)."],
) -> str:
    """Delega al sub-agente especializado en Siigo Nube ERP. Usa esta herramienta para CUALQUIER operación con Siigo: gestionar clientes, productos, facturas de venta/compra, notas crédito, cotizaciones, recibos de caja/pago, comprobantes contables, cuentas por pagar, categorías de inventario, y consultar catálogos (impuestos, bodegas, formas de pago, etc.)."""
    tid = current_thread_id.get()
    return await run_siigo_agent(consulta, thread_id=tid)


# ==================== TODAS LAS HERRAMIENTAS ====================

GENERAL_TOOLS = [
    get_weather,
    search_web,
    calculate,
    get_current_time,
]

ALL_TOOLS = GENERAL_TOOLS + [consultar_siigo_erp]


async def create_agent() -> ChatAgent:
    """Crea y retorna el agente configurado."""
    
    # Configuración de OpenAI API
    openai_api_key = os.getenv("OPENAI_API_KEY")
    model_id = os.getenv("MODEL_ID", "gpt-4o-mini")
    
    if not openai_api_key:
        raise ValueError("OPENAI_API_KEY no está configurado. Necesitas tu API Key de OpenAI.")
    
    # Cliente OpenAI usando Agent Framework
    client = OpenAIChatClient(
        api_key=openai_api_key,
        model_id=model_id,
    )
    
    # Crear el agente con ChatAgent
    agent = ChatAgent(
        name="AsistenteMAF",
        instructions="""Eres un asistente inteligente y experto en gestión empresarial, desplegado en producción.
Eres el asistente principal del sistema MAF.

## Capacidades Generales
- Consultar el clima de cualquier ubicación
- Buscar información en la web
- Realizar cálculos matemáticos
- Dar la fecha y hora actual

## Sub-Agente SIIGO Nube (ERP)
Tienes un sub-agente especializado en el ERP Siigo Nube. Cuando el usuario necesite cualquier operación con Siigo, 
usa la herramienta `consultar_siigo_erp` y describe claramente lo que se necesita. El sub-agente maneja:

- Catálogos (impuestos, bodegas, formas de pago, cuentas contables, etc.)
- Clientes/Terceros (crear, consultar, editar)
- Productos y servicios (CRUD completo)
- Facturas de venta (CRUD + facturación electrónica DIAN)
- Facturas de compra
- Notas crédito
- Cotizaciones
- Recibos de caja y de pago
- Comprobantes contables
- Cuentas por pagar (reportes)
- Categorías de inventario

## Instrucciones Importantes
- Responde siempre en español de manera clara y profesional.
- Para operaciones de Siigo, pasa toda la información relevante al sub-agente en la consulta.
- Para crear registros, primero pregunta los datos necesarios si no los proporciona el usuario.
- Confirma las operaciones destructivas (eliminar, anular) antes de delegarlas.
- Si el sub-agente reporta un error, explícalo al usuario y sugiere cómo solucionarlo.""",
        chat_client=client,
        tools=ALL_TOOLS,
    )
    
    return agent
