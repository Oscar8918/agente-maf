"""
Agente principal usando Microsoft Agent Framework con OpenAI API.
"""
import os
from typing import Annotated
from agent_framework import ChatAgent
from agent_framework.openai import OpenAIChatClient


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


# Lista de herramientas disponibles
TOOLS = [
    get_weather,
    search_web,
    calculate,
    get_current_time,
]


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
        instructions="""Eres un asistente inteligente y amigable desplegado en producción.
        
Tus capacidades:
- Consultar el clima de cualquier ubicación
- Buscar información en la web
- Realizar cálculos matemáticos
- Dar la fecha y hora actual

Responde siempre en español de manera clara y útil.
Si no puedes ayudar con algo, explícalo amablemente.""",
        chat_client=client,
        tools=TOOLS,
    )
    
    return agent
