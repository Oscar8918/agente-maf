# Agente MAF - Asistente Inteligente

🤖 Agente de IA construido con **Microsoft Agent Framework** y **OpenAI API**, desplegado en **EasyPanel**.

## Características

- ✅ Servidor HTTP REST listo para producción
- ✅ Múltiples herramientas (clima, búsqueda, calculadora, hora)
- ✅ Conversaciones con memoria (threads)
- ✅ Docker optimizado para EasyPanel
- ✅ OpenAI API (gpt-4o-mini, gpt-4o, etc.)
- ✅ Health checks integrados

## Requisitos Previos

1. **OpenAI API Key**
   - Ve a [OpenAI Platform](https://platform.openai.com/api-keys)
   - Crea una nueva API Key
   - Guárdala para usarla en EasyPanel

## Despliegue en EasyPanel

### 1. Conectar Repositorio

1. En EasyPanel, crea un nuevo **App**
2. Selecciona **GitHub** como fuente
3. Conecta tu cuenta de GitHub y selecciona este repositorio
4. Configura:
   - **Branch**: `main`
   - **Build**: Auto-detect (usará Dockerfile)

### 2. Configurar Variables de Entorno

En la sección **Environment** de tu app en EasyPanel, agrega:

```
OPENAI_API_KEY=sk-tu-api-key-aqui
MODEL_ID=gpt-4o-mini
PORT=8000
```

### 3. Configurar Puerto

- **Port**: `8000`
- Habilita **HTTPS** si tienes dominio

### 4. Deploy

¡Haz click en **Deploy** y listo! 🚀

## API Endpoints

### GET /
Health check básico
```bash
curl https://tu-dominio.com/
```

### GET /health
Health check detallado
```bash
curl https://tu-dominio.com/health
```

### POST /chat
Enviar mensaje al agente
```bash
curl -X POST https://tu-dominio.com/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "¿Cuál es el clima en Madrid?"}'
```

**Con thread (conversación continua):**
```bash
curl -X POST https://tu-dominio.com/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "¿Y en Barcelona?", "thread_id": "mi-thread-123"}'
```

### DELETE /threads/{thread_id}
Eliminar un thread de conversación
```bash
curl -X DELETE https://tu-dominio.com/threads/mi-thread-123
```

## Desarrollo Local

```bash
# Clonar repositorio
git clone https://github.com/tu-usuario/agente-maf.git
cd agente-maf

# Crear entorno virtual
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/Mac

# Instalar dependencias
pip install -r requirements.txt

# Configurar variables
cp .env.example .env
# Editar .env con tu OPENAI_API_KEY

# Ejecutar
python main.py
```

El servidor estará en `http://localhost:8000`

## Modelos Disponibles

| Modelo | ID | Costo aprox |
|--------|-----|-------------|
| GPT-4o Mini | `gpt-4o-mini` | $0.15/1M tokens |
| GPT-4o | `gpt-4o` | $2.50/1M tokens |
| GPT-4 Turbo | `gpt-4-turbo` | $10/1M tokens |
| GPT-3.5 Turbo | `gpt-3.5-turbo` | $0.50/1M tokens |

Cambia `MODEL_ID` en las variables de entorno para usar otro modelo.

## Estructura del Proyecto

```
agente-maf/
├── main.py           # Servidor HTTP FastAPI
├── agent.py          # Configuración del agente y herramientas
├── requirements.txt  # Dependencias Python
├── Dockerfile        # Imagen Docker para EasyPanel
├── .env.example      # Variables de entorno de ejemplo
├── .gitignore        # Archivos ignorados por Git
└── README.md         # Esta documentación
```

## Flujo CI/CD

```
Push a GitHub → EasyPanel detecta → Build Docker → Deploy automático
```

## Agregar Nuevas Herramientas

Edita [agent.py](agent.py) y agrega tu función:

```python
def mi_herramienta(
    parametro: Annotated[str, "Descripción del parámetro"],
) -> str:
    """Descripción de lo que hace la herramienta."""
    return "Resultado"

# Agregar a la lista de herramientas
TOOLS = [
    # ... herramientas existentes
    mi_herramienta,
]
```

## Licencia

MIT

---

**Nota**: Este proyecto usa versiones fijadas de `agent-framework-core==1.0.0b260107` para evitar cambios de API durante el preview.
