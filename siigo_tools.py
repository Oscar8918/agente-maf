"""
Herramientas SIIGO para el Agente MAF.
Conecta con las Azure Functions de SIIGO CRUD para gestionar el ERP Siigo Nube.

Base URL: https://siigocrud.azurewebsites.net/api
Auth: Function Key como query param "code".
Patrón HTTP:
  GET:    ?operacion={op}&{params}&code={key}
  POST:   Body JSON con datos + ?operacion=crear&code={key}
  PUT:    Body JSON con datos + ?operacion=editar&id={id}&code={key}
  DELETE:  ?operacion=eliminar&id={id}&code={key}

DEPENDENCIAS CRÍTICAS entre módulos:
- Para crear Producto → necesitas categorias_inventario (account_group.id)
- Para crear Factura Venta → necesitas tipos_comprobante(tipo=FV), usuarios(seller), formas_pago(payments.id), clientes(customer)
- Para crear Factura Compra → necesitas tipos_comprobante(tipo=FC), formas_pago, clientes(supplier)
- Para crear Nota Crédito → necesitas tipos_comprobante(tipo=NC) y factura de venta existente
- Para crear Cotización → necesitas tipos_comprobante(tipo=C), usuarios(seller)
- Para crear Recibo Caja → necesitas tipos_comprobante(tipo=RC), formas_pago, factura de venta(due)
- Para crear Recibo Pago → necesitas tipos_comprobante(tipo=RP), formas_pago, factura de compra(due)
- Para crear Comprobante Contable → necesitas tipos_comprobante(tipo=CC), cuentas_contables

LÍMITES: Max 100 resultados/página. Observaciones max 4000 chars. Descripción producto max 500 chars.
"""
import os
import json
import time
import requests
from typing import Annotated
import db
from runtime_context import current_thread_id, current_user_id


# ==================== CONFIGURACIÓN ====================

SIIGO_BASE_URL = os.getenv("SIIGO_AZURE_FUNCTIONS_URL", "https://siigocrud.azurewebsites.net/api")
SIIGO_FUNCTION_KEY = os.getenv("SIIGO_FUNCTION_KEY", "")
SIIGO_USERNAME = os.getenv("SIIGO_USERNAME", "")
SIIGO_ACCESS_KEY = os.getenv("SIIGO_ACCESS_KEY", "")
SIIGO_DEBUG = os.getenv("SIIGO_DEBUG", "false").lower() == "true"

# Alias para compatibilidad entre versiones de prompts/tools y operaciones
# reales implementadas en la Azure Function.
OPERATION_ALIASES = {
    "facturas_venta": {
        "tipos_factura_venta": "tipos_factura",
    },
    "facturas_compra": {
        "tipos_factura_compra": "tipos_facturas",
    },
    "cuentas_por_pagar": {
        "consultar_por_proveedor": "por_proveedor",
        "consultar_por_fecha": "por_fecha",
    },
}


def _safe_log_tool_event(**kwargs):
    """Loguea eventos de tools sin interrumpir el flujo principal."""
    try:
        if db.is_ready():
            db.log_tool_event(**kwargs)
    except Exception:
        # No romper la operación de negocio por fallas de observabilidad.
        pass


def _mask_secret(value: str, visible: int = 4) -> str:
    """Enmascara secretos en logs de depuración."""
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return f"{value[:visible]}{'*' * (len(value) - visible)}"


# ==================== HELPER ====================

def _call_siigo(endpoint: str, operacion: str, method: str = "GET",
                query_params: dict = None, body: dict = None) -> dict:
    """
    Llama a una Azure Function de SIIGO CRUD.
    Retorna la respuesta como dict/list (no string) para post-procesamiento.
    """
    url = f"{SIIGO_BASE_URL}/{endpoint}"
    
    params = {
        "code": SIIGO_FUNCTION_KEY,
        "operacion": operacion,
    }

    # Compatibilidad con versiones de Azure Function que exigen credenciales SIIGO
    # por query string en cada request.
    if SIIGO_USERNAME:
        params["username"] = SIIGO_USERNAME
    if SIIGO_ACCESS_KEY:
        params["access_key"] = SIIGO_ACCESS_KEY
    
    if query_params:
        params.update(query_params)
    
    started_at = time.perf_counter()
    thread_id = current_thread_id.get()
    user_id = current_user_id.get()

    try:
        headers = {"Content-Type": "application/json"} if body else {}

        if SIIGO_DEBUG:
            debug_params = dict(params)
            if "code" in debug_params:
                debug_params["code"] = _mask_secret(debug_params["code"])
            if "access_key" in debug_params:
                debug_params["access_key"] = _mask_secret(debug_params["access_key"])
            print(f"[SIIGO_DEBUG] -> {method} {url} params={debug_params}")
        
        if method == "GET":
            resp = requests.get(url, params=params, timeout=30)
        elif method == "POST":
            resp = requests.post(url, params=params, json=body, headers=headers, timeout=30)
        elif method == "PUT":
            resp = requests.put(url, params=params, json=body, headers=headers, timeout=30)
        elif method == "DELETE":
            resp = requests.delete(url, params=params, timeout=30)
        else:
            err = {"error": f"Método HTTP no soportado: {method}"}
            _safe_log_tool_event(
                thread_id=thread_id,
                user_id=user_id,
                tool_name=f"siigo_{endpoint}",
                endpoint=endpoint,
                operation=operacion,
                request_payload={"query": query_params or {}, "body": body or {}},
                response_status=None,
                success=False,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                error_text=err["error"],
            )
            return err
        
        try:
            data = resp.json()
        except Exception:
            data = {"raw_response": resp.text, "status_code": resp.status_code}

        if SIIGO_DEBUG:
            body_len = len(resp.text or "")
            print(f"[SIIGO_DEBUG] <- status={resp.status_code} body_len={body_len}")

        # Señaliza de forma explícita respuestas vacías para evitar ambigüedad.
        if (resp.text or "").strip() == "":
            if resp.status_code >= 400:
                data = {
                    "error": f"HTTP {resp.status_code} desde Azure Function (respuesta vacía)",
                    "status_code": resp.status_code,
                }
            else:
                data = {
                    "error": "Azure Function respondió vacío",
                    "status_code": resp.status_code,
                }

        # En operaciones listar, un objeto {} no es una respuesta válida para este conector
        # (se espera lista o estructura con `results`). Lo tratamos como error explícito para
        # evitar falsos positivos de éxito.
        if (
            method == "GET"
            and operacion == "listar"
            and isinstance(data, dict)
            and not data
            and resp.ok
        ):
            data = {
                "error": "Azure Function devolvió objeto vacío en operación listar",
                "status_code": resp.status_code,
                "endpoint": endpoint,
                "operacion": operacion,
                "sugerencia": "Revisar backend SIIGO/credenciales o intentar consulta puntual por ID/código.",
            }

        success = resp.ok and not (isinstance(data, dict) and "error" in data)
        _safe_log_tool_event(
            thread_id=thread_id,
            user_id=user_id,
            tool_name=f"siigo_{endpoint}",
            endpoint=endpoint,
            operation=operacion,
            request_payload={"query": query_params or {}, "body": body or {}},
            response_status=resp.status_code,
            success=success,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            error_text=(data.get("error") if isinstance(data, dict) else None),
        )
        return data
        
    except requests.exceptions.Timeout:
        err = {"error": "Timeout: La solicitud tardó demasiado"}
        _safe_log_tool_event(
            thread_id=thread_id,
            user_id=user_id,
            tool_name=f"siigo_{endpoint}",
            endpoint=endpoint,
            operation=operacion,
            request_payload={"query": query_params or {}, "body": body or {}},
            response_status=None,
            success=False,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            error_text=err["error"],
        )
        return err
    except requests.exceptions.ConnectionError:
        err = {"error": "Error de conexión con el servidor de SIIGO"}
        _safe_log_tool_event(
            thread_id=thread_id,
            user_id=user_id,
            tool_name=f"siigo_{endpoint}",
            endpoint=endpoint,
            operation=operacion,
            request_payload={"query": query_params or {}, "body": body or {}},
            response_status=None,
            success=False,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            error_text=err["error"],
        )
        return err
    except Exception as e:
        err = {"error": f"Error inesperado: {str(e)}"}
        _safe_log_tool_event(
            thread_id=thread_id,
            user_id=user_id,
            tool_name=f"siigo_{endpoint}",
            endpoint=endpoint,
            operation=operacion,
            request_payload={"query": query_params or {}, "body": body or {}},
            response_status=None,
            success=False,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            error_text=err["error"],
        )
        return err


def _detect_method(operacion: str) -> str:
    """Detecta el método HTTP según el nombre de la operación."""
    op = operacion.lower()
    if op.startswith("crear") or op in ("enviar_mail",):
        return "POST"
    elif op in ("editar",):
        return "PUT"
    elif op in ("eliminar", "anular"):
        return "DELETE"
    else:
        return "GET"


def _normalize_operation(endpoint: str, operacion: str) -> str:
    """Normaliza alias de operación al nombre real del backend."""
    aliases = OPERATION_ALIASES.get(endpoint, {})
    return aliases.get(operacion, operacion)


def _parse_parametros(parametros_json: str) -> dict:
    """Parsea un string JSON a dict, con manejo de errores."""
    if not parametros_json or parametros_json.strip() in ("", "{}", "null", "none"):
        return {}
    try:
        return json.loads(parametros_json)
    except json.JSONDecodeError:
        return {"_parse_error": "parametros_json_invalido"}


def _normalize_list_filters(endpoint: str, operacion: str, params: dict) -> dict:
    """
    Normaliza alias de filtros de fecha en operaciones listar para evitar
    vacíos por usar claves no esperadas por cada endpoint.
    """
    if operacion != "listar" or not isinstance(params, dict):
        return params

    normalized = dict(params)

    # Endpoints orientados a fecha de documento.
    date_based = {"facturas_venta", "facturas_compra", "comprobantes_contables"}
    if endpoint in date_based:
        if "created_start" in normalized and "date_start" not in normalized:
            normalized["date_start"] = normalized["created_start"]
        if "created_end" in normalized and "date_end" not in normalized:
            normalized["date_end"] = normalized["created_end"]

    # Endpoints orientados a fecha de creación/actualización.
    created_based = {
        "clientes",
        "productos",
        "notas_credito",
        "cotizaciones",
        "recibos_caja",
        "recibos_pago",
    }
    if endpoint in created_based:
        if "date_start" in normalized and "created_start" not in normalized:
            normalized["created_start"] = normalized["date_start"]
        if "date_end" in normalized and "created_end" not in normalized:
            normalized["created_end"] = normalized["date_end"]

    return normalized


def _validate_comprobante_payload(payload: dict) -> dict | None:
    """
    Valida payload mínimo para crear comprobantes contables antes de llamar a SIIGO.
    Retorna dict de error si es inválido, o None si es válido.
    """
    if not isinstance(payload, dict):
        return {
            "error": "Payload inválido para crear comprobante contable",
            "detalle": "Debe enviar un objeto JSON con document, date e items.",
            "status_code": 400,
        }

    missing_fields = []
    document = payload.get("document")
    if not isinstance(document, dict) or not document.get("id"):
        missing_fields.append("document.id")
    if not payload.get("date"):
        missing_fields.append("date")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        missing_fields.append("items[]")

    if missing_fields:
        return {
            "error": "Faltan campos obligatorios para crear comprobante contable",
            "faltantes": missing_fields,
            "status_code": 400,
            "sugerencia": [
                "Consultar tipos_comprobante con tipo=CC para obtener document.id.",
                "Consultar cuentas_contables para validar account.code.",
                "Asegurar items con account.movement (Debit/Credit) y value.",
            ],
        }

    debit_total = 0.0
    credit_total = 0.0
    invalid_items = []

    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            invalid_items.append(f"items[{idx}] no es objeto")
            continue

        account = item.get("account")
        movement = ""
        if isinstance(account, dict):
            movement = str(account.get("movement", "")).strip()
            if not account.get("code"):
                invalid_items.append(f"items[{idx}].account.code faltante")
        else:
            invalid_items.append(f"items[{idx}].account faltante")

        value = item.get("value")
        try:
            value_num = float(value)
        except (TypeError, ValueError):
            invalid_items.append(f"items[{idx}].value inválido")
            continue

        if value_num <= 0:
            invalid_items.append(f"items[{idx}].value debe ser mayor a 0")
            continue

        movement_lc = movement.lower()
        if movement_lc == "debit":
            debit_total += value_num
        elif movement_lc == "credit":
            credit_total += value_num
        else:
            invalid_items.append(f"items[{idx}].account.movement inválido (usar Debit/Credit)")

    if invalid_items:
        return {
            "error": "Items inválidos en comprobante contable",
            "detalle": invalid_items,
            "status_code": 400,
        }

    if abs(debit_total - credit_total) > 0.01:
        return {
            "error": "Partida doble inválida: Débitos y Créditos no cuadran",
            "totales": {"debit": round(debit_total, 2), "credit": round(credit_total, 2)},
            "status_code": 400,
            "sugerencia": "Ajusta items hasta que total Debit == total Credit.",
        }

    return None


def _filter_response_fields(data, campos: list) -> any:
    """
    Filtra la respuesta para incluir solo los campos de nivel superior especificados.
    Soporta respuestas con formato:
    - {"results": [...], "pagination": {...}}
    - {"success": true, "data": {"results": [...], "pagination": {...}}}
    - {"success": true, "data": [...]}
    - listas planas
    """
    def _extract_nested_value(record: dict, dotted_path: str):
        value = record
        for part in dotted_path.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return None, False
        return value, True

    def _extract_field_with_aliases(record: dict, field: str):
        # 1) Campo directo
        if field in record:
            return record[field], True

        # 2) Notación punto (ej: metadata.created)
        if "." in field:
            return _extract_nested_value(record, field)

        # 3) Alias comunes para respuestas SIIGO
        metadata = record.get("metadata") if isinstance(record, dict) else None
        if field == "created_at" and isinstance(metadata, dict):
            created = metadata.get("created")
            if created is not None:
                return created, True
        if field in ("updated_at", "last_updated") and isinstance(metadata, dict):
            updated = metadata.get("last_updated")
            if updated is not None:
                return updated, True

        # 4) Alias monetarios: en algunos listados viene en payment.value o items[].value
        if field in ("value", "total"):
            total = record.get("total")
            if isinstance(total, (int, float)):
                return total, True

            payment = record.get("payment")
            if isinstance(payment, dict):
                payment_value = payment.get("value")
                if isinstance(payment_value, (int, float)):
                    return payment_value, True

            items = record.get("items")
            if isinstance(items, list):
                numeric_values = [
                    item.get("value")
                    for item in items
                    if isinstance(item, dict) and isinstance(item.get("value"), (int, float))
                ]
                if numeric_values:
                    return float(sum(numeric_values)), True

        return None, False

    def filter_record(record):
        if not isinstance(record, dict):
            return record
        filtered = {}
        for field in campos:
            value, found = _extract_field_with_aliases(record, field)
            if found:
                filtered[field] = value
        return filtered
    
    if isinstance(data, dict):
        # Formato común del backend SIIGO: {"success": true, "data": ...}
        if "data" in data:
            filtered = dict(data)
            data_payload = data.get("data")

            if isinstance(data_payload, dict) and "results" in data_payload:
                nested = {}
                if "pagination" in data_payload:
                    nested["pagination"] = data_payload["pagination"]
                nested["results"] = [filter_record(r) for r in data_payload.get("results", [])]
                filtered["data"] = nested
                return filtered

            if isinstance(data_payload, list):
                filtered["data"] = [filter_record(r) for r in data_payload]
                return filtered

            if isinstance(data_payload, dict):
                filtered["data"] = filter_record(data_payload)
                return filtered

            return filtered

        if "results" in data:
            filtered = {}
            if "pagination" in data:
                filtered["pagination"] = data["pagination"]
            filtered["results"] = [filter_record(r) for r in data.get("results", [])]
            return filtered
        else:
            return filter_record(data)
    elif isinstance(data, list):
        return [filter_record(r) for r in data]
    return data


def _to_response_str(data, max_chars: int = 15000) -> str:
    """Convierte data a JSON string con truncamiento inteligente."""
    response_str = json.dumps(data, ensure_ascii=False, indent=2)
    if len(response_str) > max_chars:
        # Para listas con resultados, intentar contar registros completos
        if isinstance(data, dict) and "results" in data and isinstance(data["results"], list):
            results = data["results"]
            pagination = data.get("pagination", {})
            total = pagination.get("total_results", len(results))
            # Reducir registros hasta que quepa
            while len(results) > 1:
                test = {"pagination": pagination, "results": results, "_nota": f"Mostrando {len(results)} de {total} registros"}
                test_str = json.dumps(test, ensure_ascii=False, indent=2)
                if len(test_str) <= max_chars:
                    return test_str
                results = results[:-1]
            # Si un solo registro es demasiado grande, truncar
            response_str = json.dumps(
                {"pagination": pagination, "results": results, "_nota": f"Mostrando 1 de {total} registros (use _campos para reducir tamaño o page_size más pequeño)"},
                ensure_ascii=False, indent=2
            )
        if len(response_str) > max_chars:
            response_str = response_str[:max_chars] + "\n... (respuesta truncada)"
    return response_str


def _execute_siigo_tool(endpoint: str, operacion: str, parametros_json: str) -> str:
    """
    Ejecuta una operación SIIGO determinando automáticamente el método HTTP
    y separando parámetros de query vs body.
    
    Soporta parámetros especiales en el JSON:
    - _campos: lista de campos a incluir en la respuesta (ej: ["id","name","identification","phones","contacts"])
    - _todos: true para paginar automáticamente y traer TODOS los registros (solo GET listar)
    """
    operacion = _normalize_operation(endpoint, operacion)
    method = _detect_method(operacion)
    params = _parse_parametros(parametros_json)
    params = _normalize_list_filters(endpoint, operacion, params)

    if "_parse_error" in params:
        return _to_response_str(
            {
                "error": "El JSON de parametros es inválido",
                "detalle": "Asegúrate de enviar un objeto JSON válido en formato string.",
                "status_code": 400,
            }
        )
    
    # Extraer parámetros especiales (no van al API)
    campos = None
    if "_campos" in params:
        campos_raw = params.pop("_campos")
        if isinstance(campos_raw, list):
            campos = campos_raw
        elif isinstance(campos_raw, str):
            campos = [c.strip() for c in campos_raw.split(",")]
    
    paginar_todo = params.pop("_todos", False)

    # El backend de notas crédito no soporta edición (solo GET/POST).
    if endpoint == "notas_credito" and operacion == "editar":
        return _to_response_str(
            {
                "error": "Operación no soportada por backend",
                "detalle": "notas_credito solo permite operaciones GET y POST; no existe editar.",
                "status_code": 405,
                "sugerencia": "Crea una nueva nota crédito o realiza ajustes desde Siigo Web.",
            }
        )

    if endpoint == "comprobantes_contables" and operacion == "crear":
        validation_error = _validate_comprobante_payload(params)
        if validation_error:
            return _to_response_str(validation_error)
    
    if method in ("POST", "PUT"):
        query_params = {}
        body = params.copy()
        for key in ["id", "nombre", "identificacion", "codigo", "nombre_factura"]:
            if key in body:
                query_params[key] = body.pop(key)
        data = _call_siigo(endpoint, operacion, method, query_params=query_params, body=body)
    elif method == "DELETE":
        data = _call_siigo(endpoint, operacion, method, query_params=params)
    else:
        # GET - soportar paginación automática
        if paginar_todo:
            def _extract_results_payload(raw_page):
                """
                Extrae (results, pagination, wrapped_mode) soportando:
                - {"results":[...], "pagination": {...}}
                - {"success": true, "data": {"results":[...], "pagination": {...}}}
                """
                if isinstance(raw_page, dict):
                    if "results" in raw_page:
                        return raw_page.get("results", []), raw_page.get("pagination", {}), False
                    data_payload = raw_page.get("data")
                    if isinstance(data_payload, dict) and "results" in data_payload:
                        return data_payload.get("results", []), data_payload.get("pagination", {}), True
                return None, None, None

            all_results = []
            page = int(params.get("page", 1))
            page_size = int(params.get("page_size", 25))
            params["page_size"] = str(page_size)
            max_pages = 20  # Límite de seguridad
            wrapped_mode = False
            
            for _ in range(max_pages):
                params["page"] = str(page)
                page_data = _call_siigo(endpoint, operacion, method, query_params=params)
                
                if isinstance(page_data, dict) and "error" in page_data:
                    return _to_response_str(page_data)

                results, pagination, page_wrapped_mode = _extract_results_payload(page_data)
                if results is None:
                    # Respuesta sin formato estándar, retornar tal cual
                    if campos:
                        page_data = _filter_response_fields(page_data, campos)
                    return _to_response_str(page_data)

                wrapped_mode = wrapped_mode or bool(page_wrapped_mode)
                all_results.extend(results)
                total = pagination.get("total_results", 0)

                if (total and len(all_results) >= total) or len(results) < page_size:
                    break
                page += 1
            
            consolidated_payload = {
                "pagination": {"total_results": len(all_results), "page": 1, "page_size": len(all_results)},
                "results": all_results,
            }
            data = {"success": True, "data": consolidated_payload} if wrapped_mode else consolidated_payload
        else:
            data = _call_siigo(endpoint, operacion, method, query_params=params)
    
    # Aplicar filtro de campos si se especificó
    if campos and not isinstance(data, str):
        data = _filter_response_fields(data, campos)
    
    return _to_response_str(data)


# ==================== TOOLS ====================

# 1. CATÁLOGOS
def siigo_catalogos(
    catalogo: Annotated[str, """Catálogo a consultar. Valores válidos:
- 'impuestos': IVA, INC, ReteIVA, ReteICA, etc. Retorna id, name, percentage, active.
- 'listas_precio': Listas de precios. Retorna id, name, default.
- 'bodegas': Bodegas/almacenes. Retorna id, code, name, active.
- 'usuarios': Usuarios/vendedores (necesarios como seller en facturas y cotizaciones). Retorna id, username, first_name, last_name, email.
- 'tipos_comprobante': Tipos de documento. REQUIERE parámetro 'tipo'. Retorna id, code, name, consecutive.
- 'formas_pago': Formas de pago (efectivo, crédito, transferencia). Retorna id, name, type (Cash/Credit), active, due_date. Opcional: 'tipo_documento' para filtrar.
- 'centros_costo': Centros de costo/responsabilidad. Retorna id, code, name, active."""],
    parametros: Annotated[str, """JSON con parámetros opcionales según el catálogo:
- tipos_comprobante REQUIERE: {"tipo": "FV"} donde tipo puede ser: FV (factura venta), FC (factura compra), NC (nota crédito), ND (nota débito), RC (recibo caja), RP (recibo pago), CC (comprobante contable), C (cotización).
- formas_pago OPCIONAL: {"tipo_documento": "FV"} para filtrar por tipo de documento.
- Otros catálogos no necesitan parámetros: usar '{}'.
Ejemplo: Para obtener tipos de factura de venta: catalogo='tipos_comprobante', parametros='{"tipo": "FV"}'"""] = "{}",
) -> str:
    """Consulta catálogos maestros de Siigo Nube (solo lectura). Usa esto PRIMERO para obtener IDs necesarios para crear documentos: document.id (tipos_comprobante), seller (usuarios), payments.id (formas_pago), taxes.id (impuestos), cost_center (centros_costo), warehouse (bodegas)."""
    params = _parse_parametros(parametros)
    params["catalogo"] = catalogo
    data = _call_siigo("catalogos", catalogo, "GET", query_params=params)
    return _to_response_str(data)


# 2. CLIENTES
def siigo_clientes(
    operacion: Annotated[str, """Operación a realizar:
GET:
- 'listar': Lista clientes con paginación. Params: page, page_size (max 100), created_start, created_end.
- 'consultar_por_id': Busca por ID Siigo. Param: id.
- 'consultar_por_identificacion': Busca por nro documento. Param: identificacion.
- 'tipos_documento': Lista tipos de documento disponibles.
- 'responsabilidades_fiscales': Lista responsabilidades fiscales.
- 'usuarios': Lista usuarios asociados.
POST:
- 'crear': Crea un nuevo cliente/tercero. Requiere body JSON completo.
PUT:
- 'editar': Edita un cliente existente. Requiere id + campos a modificar.
⚠️ NO existe DELETE: los clientes NO se pueden eliminar en Siigo."""],
    parametros: Annotated[str, """JSON con parámetros según la operación:

GET listar: {"page": "1", "page_size": "25"}
GET consultar_por_id: {"id": "12345"}
GET consultar_por_identificacion: {"identificacion": "900123456"}

⭐ FILTRO DE CAMPOS (_campos): Para traer solo campos específicos y evitar truncamiento:
{"created_start": "2026-02-02", "created_end": "2026-02-06", "page_size": "100", "_campos": ["id", "identification", "name", "phones", "contacts"]}

⭐ TRAER TODOS (_todos): Para paginar automáticamente y traer todos los registros:
{"created_start": "2026-02-02", "created_end": "2026-02-06", "_todos": true, "_campos": ["id", "identification", "name", "phones", "contacts"]}

POST crear - CAMPOS OBLIGATORIOS:
{
  "type": "Customer",  // Customer, Supplier, Other
  "person_type": "Person",  // Person o Company
  "id_type": "13",  // Código DIAN: 13=CC, 22=CE, 31=NIT, 41=Pasaporte, 42=Doc.extranjero, 43=Sin identificación, 47=PEP, 50=NIT extranjero, 91=NUIP
  "identification": "1234567890",
  "check_digit": "5",  // Solo NIT (id_type=31)
  "name": ["Juan", "", "Pérez", "García"],  // [nombre1, nombre2, apellido1, apellido2] para Person; ["Razón Social"] para Company
  "commercial_name": "Empresa XYZ",  // Opcional
  "vat_responsible": true,  // Responsable de IVA
  "fiscal_responsibilities": [{"code": "O-13"}],  // O-13, O-15, O-23, O-47, R-99-PN
  "address": {
    "address": "Calle 100 #15-20",
    "city": {"country_code": "Co", "state_code": "11", "city_code": "11001"},  // Ciudades código DANE: Bogotá=11001, Medellín=05001, Cali=76001, Barranquilla=08001, Cartagena=13001, Bucaramanga=68001
    "postal_code": "110111"
  },
  "phones": [{"indicative": "57", "number": "3001234567", "extension": ""}],
  "contacts": [{"first_name": "Juan", "last_name": "Pérez", "email": "juan@email.com"}]
}

PUT editar: {"id": "12345", ...campos_a_editar}
⚠️ NO se pueden cambiar: id_type, identification, person_type."""] = "{}",
) -> str:
    """Gestiona clientes/terceros en Siigo Nube. Crear, consultar, listar y editar. Los clientes NO se pueden eliminar. Para crear se necesitan: type, person_type, id_type (código DIAN), identification, name[] y address con códigos DANE de ciudad."""
    return _execute_siigo_tool("clientes", operacion, parametros)


# 3. PRODUCTOS
def siigo_productos(
    operacion: Annotated[str, """Operación a realizar:
GET:
- 'listar': Lista productos. Params: page, page_size, created_start, created_end.
- 'consultar_por_id': Busca por ID Siigo. Param: id.
- 'consultar_por_codigo': Busca por código/SKU. Param: codigo.
- 'grupos_inventario': Lista grupos (account_group) disponibles.
- 'impuestos': Lista impuestos aplicables.
- 'bodegas': Lista bodegas disponibles.
POST:
- 'crear': Crea producto/servicio. Requiere body JSON.
PUT:
- 'editar': Edita producto. Requiere id + campos.
DELETE:
- 'eliminar': Elimina producto. Param: id. ⚠️ No se puede eliminar si tiene transacciones (usar active:false)."""],
    parametros: Annotated[str, """JSON con parámetros según la operación:

GET listar: {"page": "1", "page_size": "25"}
GET consultar_por_id: {"id": "12345"}
GET consultar_por_codigo: {"codigo": "PROD001"}

⭐ FILTRO DE CAMPOS: {"page_size": "100", "_campos": ["id", "code", "name", "type", "prices", "active"]}
⭐ TRAER TODOS: {"_todos": true, "_campos": ["id", "code", "name", "type", "prices"]}

POST crear - CAMPOS OBLIGATORIOS:
{
  "code": "PROD001",  // Único, max 20 chars, NO editable después de crear
  "name": "Producto de ejemplo",  // Max 100 chars
  "account_group": {"id": 12345},  // ⚠️ OBTENER PRIMERO de categorias_inventario
  "type": "Product",  // Product, Service, ConsumerGood
  "stock_control": true,  // default true para Product
  "tax_classification": "Taxed",  // Taxed, Exempt, Excluded
  "tax_included": false,
  "unit": "94",  // Código DIAN: 94=Unidad, 24=Docena, KGM=Kilogramo, LTR=Litro, MTR=Metro, GRM=Gramo
  "unit_label": "Unidad",
  "description": "Descripción del producto",  // Max 500 chars
  "prices": [{"price_list": [{"id": 1}], "value": 50000}],
  "taxes": [{"id": 1234}]  // ID del impuesto (obtener de catálogos)
}

PUT editar: {"id": "12345", "name": "Nuevo nombre", ...}
⚠️ NO se puede cambiar: code, type (si tiene transacciones).

DELETE eliminar: {"id": "12345"}
⚠️ Si tiene transacciones, desactivar con editar: {"id":"xxx","active":false}"""] = "{}",
) -> str:
    """Gestiona productos y servicios en Siigo Nube. CRUD completo. Para crear necesitas: code (único, max 20), name, account_group.id (de categorias_inventario), type (Product/Service/ConsumerGood). Códigos unidad DIAN: 94=Unidad, KGM=Kg, LTR=Litro."""
    return _execute_siigo_tool("productos", operacion, parametros)


# 4. FACTURAS DE VENTA
def siigo_facturas_venta(
    operacion: Annotated[str, """Operación a realizar:
GET:
- 'listar': Lista facturas. Params: page, page_size, date_start, date_end, customer_identification, document_id.
- 'consultar_por_id': Param: id.
- 'consultar_por_nombre': Param: nombre (ej: 'FV-003-457').
- 'tipos_factura': Tipos de documento FV disponibles (equivale a tipos_comprobante tipo=FV).
- 'vendedores': Lista vendedores disponibles.
- 'formas_pago': Formas de pago disponibles.
- 'impuestos': Impuestos disponibles.
- 'clientes': Lista/consulta de clientes para facturación.
- 'productos': Lista/consulta de productos para facturación.
- 'pdf': Obtiene PDF de la factura. Param: id.
- 'xml': Obtiene XML DIAN. Param: id.
- 'errores_dian': Errores DIAN. Param: id.
POST:
- 'crear': Crea factura de venta. Requiere body JSON completo.
- 'enviar_mail': Envía factura por email. Params: id + body con emails.
PUT:
- 'editar': Edita factura. Requiere id + campos.
DELETE:
- 'eliminar': Elimina/anula según validaciones del backend. Param: id.
- 'anular': Alias explícito para anulación lógica. Param: id.
⚠️ No editar ni anular si tiene restricciones DIAN o documentos relacionados."""],
    parametros: Annotated[str, """JSON con parámetros según la operación:

GET listar: {"page": "1", "page_size": "25", "date_start": "2024-01-01", "date_end": "2024-12-31"}
GET consultar_por_id: {"id": "abc123"}
GET consultar_por_nombre: {"nombre": "FV-003-457"}
GET pdf/xml/errores_dian: {"id": "abc123"}

⭐ FILTRO DE CAMPOS: {"date_start": "2026-01-01", "date_end": "2026-01-31", "page_size": "100", "_campos": ["id", "name", "date", "total", "customer", "stamp"]}
⭐ TRAER TODOS: {"date_start": "2026-01-01", "date_end": "2026-01-31", "_todos": true, "_campos": ["id", "name", "date", "total", "customer"]}

POST crear - CAMPOS OBLIGATORIOS:
{
  "document": {"id": 12345},  // ⚠️ Obtener de tipos_comprobante con tipo=FV
  "date": "2024-06-15",  // Formato yyyy-MM-dd
  "customer": {
    "identification": "900123456",  // ⚠️ El cliente DEBE existir en Siigo
    "branch_office": 0
  },
  "seller": 123,  // ⚠️ ID del vendedor (obtener de catálogo usuarios)
  "stamp": {"send": true},  // true para enviar a la DIAN (facturación electrónica)
  "mail": {"send": false},  // Enviar copia al email del cliente
  "observations": "Factura de ejemplo",  // Max 500 chars
  "items": [  // ⚠️ OBLIGATORIO: al menos un ítem
    {
      "code": "PROD001",  // Código del producto (debe existir)
      "description": "Producto de ejemplo",
      "quantity": 2,
      "price": 50000,
      "discount": 0,  // Porcentaje 0-100
      "taxes": [{"id": 1234}],  // IDs de impuestos
      "warehouse": 1  // ID de bodega
    }
  ],
  "payments": [  // ⚠️ OBLIGATORIO: al menos una forma de pago
    {
      "id": 5678,  // ⚠️ ID de forma de pago (obtener de catálogo formas_pago)
      "value": 100000,
      "due_date": "2024-07-15"  // Solo para crédito
    }
  ],
  "retentions": [{"id": 9012}],  // Opcional: retenciones
  "cost_center": 1,  // Opcional: centro de costo
  "currency": {"code": "COP", "exchange_rate": 1}  // Opcional: moneda
}

POST enviar_mail: {"id": "abc123", "mail_to": "cliente@email.com"}
PUT editar: {"id": "abc123", ...campos}  ⚠️ No cambiar: document.id, customer.identification, currency.code
Estados DIAN (stamp): Pending, Sending, Accepted, Rejected, Error"""] = "{}",
) -> str:
    """Gestiona facturas de venta electrónicas en Siigo Nube. Para crear necesitas PRIMERO obtener: document.id (catálogo tipos_comprobante tipo=FV), seller (catálogo usuarios), payments.id (catálogo formas_pago), y el cliente debe existir. stamp.send=true envía a la DIAN."""
    return _execute_siigo_tool("facturas_venta", operacion, parametros)


# 5. FACTURAS DE COMPRA
def siigo_facturas_compra(
    operacion: Annotated[str, """Operación a realizar:
GET:
- 'listar': Lista facturas compra. Params: page, page_size, date_start, date_end, supplier_identification, document_id, name.
- 'consultar_por_id': Param: id.
- 'consultar_por_nombre': Param: nombre (ej: 'FC-1-22').
- 'tipos_facturas': Tipos de documento FC disponibles.
- 'formas_pago': Formas de pago disponibles.
- 'impuestos': Impuestos disponibles.
POST:
- 'crear': Crea factura de compra/gasto. Requiere body JSON.
PUT:
- 'editar': Edita factura de compra. Requiere id + campos.
DELETE:
- 'eliminar': Elimina factura de compra. Param: id.
⚠️ No eliminar si tiene pagos (recibos_pago) o notas crédito asociadas."""],
    parametros: Annotated[str, """JSON con parámetros según la operación:

GET listar: {"page": "1", "page_size": "25", "date_start": "2024-01-01", "date_end": "2024-12-31"}
GET consultar_por_id: {"id": "abc123"}
GET consultar_por_nombre: {"nombre": "FC-1-22"}

⭐ FILTRO DE CAMPOS: {"date_start": "2026-01-01", "date_end": "2026-01-31", "page_size": "100", "_campos": ["id", "name", "date", "total", "supplier"]}
⭐ TRAER TODOS: {"date_start": "2026-01-01", "date_end": "2026-01-31", "_todos": true, "_campos": ["id", "name", "date", "total", "supplier"]}

POST crear - CAMPOS OBLIGATORIOS:
{
  "document": {"id": 12345},  // ⚠️ Obtener de tipos_comprobante con tipo=FC
  "date": "2024-06-15",
  "supplier": {  // ⚠️ Usa 'supplier' NO 'customer' (diferencia con FV)
    "identification": "900123456",
    "branch_office": 0
  },
  "observations": "Compra de insumos",  // Max 4000 chars
  "items": [
    {
      "code": "PROD001",
      "description": "Insumo comprado",
      "quantity": 10,
      "price": 25000,
      "discount": 0,
      "taxes": [{"id": 1234}],
      "warehouse": 1
    }
  ],
  "payments": [
    {
      "id": 5678,
      "value": 250000,
      "due_date": "2024-07-15"
    }
  ],
  "cost_center": 1,
  "currency": {"code": "COP", "exchange_rate": 1}
}

PUT editar: {"id": "abc123", ...campos}  ⚠️ No cambiar: document.id, supplier.identification, currency.code, number
DELETE eliminar: {"id": "abc123"}  ⚠️ No eliminar si tiene pagos o NC"""] = "{}",
) -> str:
    """Gestiona facturas de compra/gasto en Siigo Nube. CRUD completo. ⚠️ Usa 'supplier' (no 'customer'). Para crear necesitas: document.id (tipos_comprobante tipo=FC), supplier existente, items[], payments[]. No eliminar si tiene pagos o notas crédito."""
    return _execute_siigo_tool("facturas_compra", operacion, parametros)


# 6. NOTAS CRÉDITO
def siigo_notas_credito(
    operacion: Annotated[str, """Operación a realizar:
GET:
- 'listar': Lista notas crédito. Params: page, page_size, date_start, date_end, customer_identification.
- 'consultar_por_id': Param: id.
- 'consultar_por_nombre': Param: nombre (ej: 'NC-001-123').
- 'tipos_nota_credito': Tipos de documento NC disponibles.
- 'vendedores': Lista vendedores.
- 'formas_pago': Formas de pago.
- 'impuestos': Impuestos disponibles.
- 'facturas': Lista facturas disponibles para relacionar.
- 'buscar_factura': Busca factura por nombre. Param: nombre_factura (ej: 'FV-003-457').
- 'pdf': Obtiene PDF. Param: id.
POST:
- 'crear': Crea nota crédito. Requiere body JSON.
⚠️ NO se puede eliminar/DELETE notas crédito.
⚠️ El backend no soporta editar notas crédito por API (solo GET/POST)."""],
    parametros: Annotated[str, """JSON con parámetros según la operación:

GET listar: {"page": "1", "page_size": "25"}
GET buscar_factura: {"nombre_factura": "FV-003-457"}

POST crear - Caso 1: NC sobre factura existente en Siigo:
{
  "document": {"id": 12345},  // ⚠️ Obtener de tipos_comprobante con tipo=NC
  "date": "2024-06-15",
  "invoice": "abc-def-123",  // ⚠️ ID de la factura de venta en Siigo
  "reason": 1,  // ⚠️ Motivo DIAN OBLIGATORIO para facturación electrónica:
                 // 1=Devolución parcial, 2=Anulación de factura, 3=Rebaja o bonificación,
                 // 4=Ajuste de precio, 5=Otros, 6=Cambio de fecha, 7=Descuento pronto pago
  "stamp": {"send": true},
  "mail": {"send": false},
  "items": [
    {
      "code": "PROD001",
      "description": "Devolución producto",
      "quantity": 1,
      "price": 50000,
      "discount": 0,
      "taxes": [{"id": 1234}]
    }
  ],
  "payments": [{"id": 5678, "value": 50000}]
}

POST crear - Caso 2: NC sobre factura EXTERNA (no en Siigo):
{
  "document": {"id": 12345},
  "date": "2024-06-15",
  "customer": {"identification": "900123456", "branch_office": 0},
  "seller": 123,
  "invoice_data": {
    "date": "2024-05-01",
    "prefix": "FV",
    "number": "12345",
    "cufe": "abc123..."  // CUFE de la factura original
  },
  "reason": 2,
  "stamp": {"send": true},
  "items": [...],
  "payments": [...]
}

⚠️ El monto de la NC NO puede exceder el saldo de la factura original."""] = "{}",
) -> str:
    """Gestiona notas crédito en Siigo Nube. Solo crear y consultar (NO editar ni eliminar si fue enviada a DIAN). Motivos DIAN obligatorios: 1=Devolución, 2=Anulación, 3=Rebaja, 4=Ajuste precio, 5=Otros, 6=Cambio fecha, 7=Desc. pronto pago. Puede ser sobre factura Siigo (invoice) o externa (invoice_data+customer+seller)."""
    return _execute_siigo_tool("notas_credito", operacion, parametros)


# 7. COTIZACIONES
def siigo_cotizaciones(
    operacion: Annotated[str, """Operación a realizar:
GET:
- 'listar': Lista cotizaciones. Params: page, page_size, created_start, created_end, customer_identification, name.
- 'consultar_por_id': Param: id.
- 'consultar_por_nombre': Param: nombre (ej: 'C-003-457').
- 'tipos_cotizacion': Tipos de documento C disponibles.
- 'vendedores': Lista vendedores.
- 'impuestos': Impuestos disponibles.
POST:
- 'crear': Crea cotización/oferta comercial. Requiere body JSON.
PUT:
- 'editar': Edita cotización. Requiere id + campos.
DELETE:
- 'eliminar': Elimina cotización. Param: id.
⚠️ No eliminar si fue convertida a factura de venta."""],
    parametros: Annotated[str, """JSON con parámetros según la operación:

GET listar: {"page": "1", "page_size": "25"}
GET consultar_por_id: {"id": "abc123"}
GET consultar_por_nombre: {"nombre": "C-003-457"}

POST crear - CAMPOS OBLIGATORIOS:
{
  "document": {"id": 12345},  // ⚠️ Obtener de tipos_comprobante con tipo=C
  "date": "2024-06-15",
  "customer": {
    "identification": "900123456",
    "branch_office": 0
  },
  "seller": 123,  // ⚠️ ID del vendedor
  "cost_center": 1,  // Opcional
  "currency": {"code": "COP", "exchange_rate": 1},  // Opcional
  "items": [  // ⚠️ OBLIGATORIO
    {
      "code": "PROD001",
      "description": "Servicio de consultoría",
      "quantity": 1,
      "price": 500000,
      "discount": 10,  // Porcentaje
      "taxes": [{"id": 1234}]
    }
  ]
}
⚠️ Las cotizaciones NO tienen payments (a diferencia de facturas).
⚠️ No van a la DIAN, no afectan inventario ni cartera.
⚠️ La conversión a factura de venta es manual desde Siigo Web.

PUT editar: {"id": "abc123", ...campos}
DELETE eliminar: {"id": "abc123"}"""] = "{}",
) -> str:
    """Gestiona cotizaciones/ofertas comerciales en Siigo Nube. CRUD completo. No afectan inventario, cartera ni DIAN. No llevan payments. Para crear necesitas: document.id (tipos_comprobante tipo=C), customer, seller, items[]. No eliminar si fue convertida a factura."""
    return _execute_siigo_tool("cotizaciones", operacion, parametros)


# 8. RECIBOS DE CAJA
def siigo_recibos_caja(
    operacion: Annotated[str, """Operación a realizar:
GET:
- 'listar': Lista recibos de caja. Params: page, page_size, created_start, created_end, name.
- 'consultar_por_id': Param: id.
- 'consultar_por_nombre': Param: nombre (ej: 'RC-1-22').
- 'tipos_recibos': Tipos de documento RC disponibles.
- 'formas_pago': Formas de pago disponibles.
POST:
- 'crear': Crea recibo de caja. Requiere body JSON con type.
- 'crear_anticipo': Variante explícita para anticipos.
- 'crear_abono_deuda': Variante explícita para abonos a deuda.
- 'crear_avanzado': Variante avanzada para payload detallado.
⚠️ NO se puede editar (PUT) ni eliminar (DELETE) por API. Solo anular manualmente en Siigo Web."""],
    parametros: Annotated[str, """JSON con parámetros según la operación:

GET listar: {"page": "1", "page_size": "25"}
GET consultar_por_id: {"id": "abc123"}
GET consultar_por_nombre: {"nombre": "RC-1-22"}

POST crear - 3 TIPOS de recibo de caja:

Tipo 1 - DebtPayment (abono a factura de venta):
{
  "document": {"id": 12345},  // ⚠️ Obtener de tipos_comprobante con tipo=RC
  "date": "2024-06-15",
  "type": "DebtPayment",
  "customer": {"identification": "900123456", "branch_office": 0},
  "observations": "Pago parcial factura",
  "items": [
    {
      "due": {
        "prefix": "FV",  // Prefijo de la factura de venta
        "consecutive": 457,  // Número consecutivo de la factura
        "quote": 0
      },
      "value": 50000  // Monto del abono
    }
  ],
  "payment": {"id": 5678, "value": 50000}  // Forma de pago
}

Tipo 2 - AdvancePayment (anticipo de cliente):
{
  "document": {"id": 12345},
  "date": "2024-06-15",
  "type": "AdvancePayment",
  "customer": {"identification": "900123456", "branch_office": 0},
  "advance_value": 100000,  // Monto del anticipo (en lugar de items con due)
  "payment": {"id": 5678, "value": 100000}
}

Tipo 3 - Detailed (registro contable detallado):
{
  "document": {"id": 12345},
  "date": "2024-06-15",
  "type": "Detailed",
  "customer": {"identification": "900123456", "branch_office": 0},
  "items": [
    {
      "account": {"code": "11050501"},  // Cuenta contable
      "value": 50000,
      "description": "Ingreso por concepto X"
    }
  ],
  "payment": {"id": 5678, "value": 50000}
}

⚠️ Recibos de caja afectan Cuentas por Cobrar (CxC).
⚠️ Para DebtPayment necesitas el prefijo y consecutivo de la factura de venta."""] = "{}",
) -> str:
    """Gestiona recibos de caja (ingresos) en Siigo Nube. Solo crear y consultar. 3 tipos: DebtPayment (abono a FV con due.prefix/due.consecutive), AdvancePayment (anticipo con advance_value), Detailed (contable con account.code). ⚠️ NO se pueden editar ni eliminar por API."""
    return _execute_siigo_tool("recibos_caja", operacion, parametros)


# 9. RECIBOS DE PAGO
def siigo_recibos_pago(
    operacion: Annotated[str, """Operación a realizar:
GET:
- 'listar': Lista recibos de pago. Params: page, page_size, created_start, created_end, name.
- 'consultar_por_id': Param: id.
- 'consultar_por_nombre': Param: nombre (ej: 'RP-1-22').
- 'tipos_recibos': Tipos de documento RP disponibles.
- 'formas_pago': Formas de pago disponibles.
POST:
- 'crear': Crea recibo de pago. Requiere body JSON.
DELETE:
- 'eliminar': Elimina recibo de pago. Param: id. ✅ SÍ se puede eliminar (a diferencia de RC).
⚠️ NO se puede editar (PUT)."""],
    parametros: Annotated[str, """JSON con parámetros según la operación:

GET listar: {"page": "1", "page_size": "25"}
GET consultar_por_id: {"id": "abc123"}
GET consultar_por_nombre: {"nombre": "RP-1-22"}

POST crear - 3 TIPOS (igual que RC pero para egresos/pagos a proveedores):

Tipo 1 - DebtPayment (pago a factura de compra):
{
  "document": {"id": 12345},  // ⚠️ Obtener de tipos_comprobante con tipo=RP
  "date": "2024-06-15",
  "type": "DebtPayment",
  "supplier": {  // ⚠️ Usa 'supplier' NO 'customer' (diferencia con RC)
    "identification": "900123456",
    "branch_office": 0
  },
  "observations": "Pago factura compra",
  "items": [
    {
      "due": {
        "prefix": "FC",  // Prefijo de factura de compra
        "consecutive": 22,
        "quote": 0
      },
      "value": 250000
    }
  ],
  "payment": {"id": 5678, "value": 250000}
}

Tipo 2 - AdvancePayment (anticipo a proveedor):
  Igual que DebtPayment pero type="AdvancePayment" y advance_value en vez de items con due.

Tipo 3 - Detailed (registro contable detallado):
  Igual que RC Detailed pero con supplier en vez de customer.

⚠️ RP afecta Cuentas por Pagar (CxP). RC afecta CxC.
⚠️ Eliminar RP restaura el saldo de la factura de compra.

DELETE eliminar: {"id": "abc123"}"""] = "{}",
) -> str:
    """Gestiona recibos de pago (egresos a proveedores) en Siigo Nube. Crear, consultar y eliminar. Usa 'supplier' (no 'customer'). SÍ se puede eliminar (restaura saldo FC). Mismos 3 tipos que RC: DebtPayment, AdvancePayment, Detailed. Afecta Cuentas por Pagar."""
    return _execute_siigo_tool("recibos_pago", operacion, parametros)


# 10. COMPROBANTES CONTABLES
def siigo_comprobantes_contables(
    operacion: Annotated[str, """Operación a realizar:
GET:
- 'listar': Lista comprobantes. Params: page, page_size, date_start, date_end, document_id, name.
- 'consultar_por_id': Param: id.
- 'consultar_por_nombre': Param: nombre (ej: 'CC-1-22').
- 'tipos_comprobantes': Tipos de documento CC disponibles.
- 'cuentas_contables': Lista cuentas contables del PUC.
- 'activos_fijos': Lista activos fijos disponibles.
- 'impuestos': Impuestos disponibles.
- 'centros_costo': Centros de costo.
POST:
- 'crear': Crea comprobante contable. Requiere body JSON.
⚠️ NO se puede editar ni eliminar. Para corregir: anular manualmente en Siigo Web y crear uno nuevo."""],
    parametros: Annotated[str, """JSON con parámetros según la operación:

GET listar: {"page": "1", "page_size": "25", "date_start": "2024-01-01", "date_end": "2024-12-31"}
GET consultar_por_id: {"id": "abc123"}
GET consultar_por_nombre: {"nombre": "CC-1-22"}

FLUJO RECOMENDADO ANTES DE CREAR:
1) siigo_catalogos(catalogo='tipos_comprobante', parametros='{"tipo":"CC"}') -> tomar document.id
2) siigo_comprobantes_contables(operacion='cuentas_contables') -> validar account.code
3) (Opcional) centros_costo / impuestos / activos_fijos si el asiento los usa
4) Validar partida doble (debitos = creditos) antes de enviar POST

POST crear - REGLA FUNDAMENTAL: Total Débitos DEBE ser igual a Total Créditos (partida doble)
{
  "document": {"id": 12345},  // ⚠️ Obtener de tipos_comprobante con tipo=CC
  "date": "2024-06-15",
  "observations": "Ajuste contable",  // Max 4000 chars
  "currency": {"code": "COP", "exchange_rate": 1},  // Opcional
  "items": [  // ⚠️ Movimientos contables - DEBEN cuadrar débitos con créditos
    {
      "account": {
        "code": "11050501",  // ⚠️ Código cuenta contable del PUC
        "movement": "Debit"  // "Debit" o "Credit"
      },
      "customer": {
        "identification": "900123456",  // Tercero asociado
        "branch_office": 0
      },
      "description": "Débito por ajuste",
      "value": 100000,
      "cost_center": 1,  // Opcional
      "due": {  // Opcional: para cuentas CxC/CxP
        "prefix": "FV",
        "quote": 0,
        "date": "2024-07-15"
      },
      "tax": {"id": 1234},  // Opcional: solo para cuentas de impuestos
      "fixed_assets": {"id": 1},  // Opcional: para cuentas de activos fijos
      "product": {"id": 1}  // Opcional: para cuentas de inventario
    },
    {
      "account": {"code": "23352501", "movement": "Credit"},
      "customer": {"identification": "900123456", "branch_office": 0},
      "description": "Crédito compensatorio",
      "value": 100000
    }
  ]
}

⚠️ Si débitos ≠ créditos, Siigo rechazará el comprobante."""] = "{}",
) -> str:
    """Gestiona comprobantes contables manuales en Siigo Nube. Solo crear y consultar. ⚠️ REGLA: Total Débitos = Total Créditos (partida doble). Items con account.code (PUC), account.movement (Debit/Credit), customer, value. No se puede editar ni eliminar."""
    return _execute_siigo_tool("comprobantes_contables", operacion, parametros)


# 11. CUENTAS POR PAGAR
def siigo_cuentas_por_pagar(
    operacion: Annotated[str, """Operación (SOLO CONSULTA, no se puede crear/editar/eliminar):
- 'listar': Lista CxP con paginación. Params: page, page_size, due_date_start, due_date_end, provider_identification, provider_branch_office.
- 'por_proveedor': CxP de un proveedor específico. Params: identificacion, sucursal (default '0').
- 'por_fecha': CxP en rango de fechas. Params: fecha_inicio, fecha_fin.
- 'vencidas': CxP vencidas a una fecha de corte. Param: fecha_corte (default: hoy). Incluye dias_vencido.
- 'resumen': Resumen general de CxP. Sin params. Retorna: total_general, total_vencido, total_por_vencer, cantidad_vencimientos, desglose por proveedores.
⚠️ Para PAGAR cuentas por pagar, usar la herramienta siigo_recibos_pago."""],
    parametros: Annotated[str, """JSON con parámetros según la operación:
- listar: {"page": "1", "page_size": "25", "due_date_start": "2024-01-01", "due_date_end": "2024-12-31"}
- por_proveedor: {"identificacion": "900123456", "sucursal": "0"}
- por_fecha: {"fecha_inicio": "2024-01-01", "fecha_fin": "2024-12-31"}
- vencidas: {"fecha_corte": "2024-06-01"}  // Default: hoy
- resumen: {}

Cada registro CxP incluye: due (prefix, consecutive, quote, date, balance), provider (id, identification, branch_office, name), cost_center, currency."""] = "{}",
) -> str:
    """Consulta cuentas por pagar en Siigo Nube. SOLO LECTURA: listar, por proveedor, por fecha, vencidas (con dias_vencido), resumen (totales generales). Para pagar, usar recibos_pago."""
    return _execute_siigo_tool("cuentas_por_pagar", operacion, parametros)


# 12. CATEGORÍAS DE INVENTARIO
def siigo_categorias_inventario(
    operacion: Annotated[str, """Operación a realizar:
GET:
- 'listar': Lista categorías. Params: page, page_size.
- 'consultar_por_nombre': Búsqueda parcial. Param: nombre.
- 'consultar_por_codigo': Búsqueda exacta. Param: codigo.
POST:
- 'crear': Crea categoría. Requiere body JSON.
PUT:
- 'editar': Edita categoría. Requiere id + campos.
⚠️ NO se pueden eliminar categorías (solo desactivar manualmente en Siigo).
⚠️ El código es asignado automáticamente por Siigo."""],
    parametros: Annotated[str, """JSON con parámetros según la operación:

GET listar: {"page": "1", "page_size": "25"}
GET consultar_por_nombre: {"nombre": "Mercancía"}  // Búsqueda parcial
GET consultar_por_codigo: {"codigo": "1234"}  // Búsqueda exacta

POST crear:
{
  "name": "Productos Electrónicos",  // Max 100 chars, OBLIGATORIO
  "type": "Product",  // OBLIGATORIO: Product, Service, ConsumerGood
  "apply_to_service": false  // Opcional, default false
}

PUT editar: {"id": "12345", "name": "Nuevo nombre"}
⚠️ No cambiar type si tiene productos asociados.

IMPORTANTE: El 'id' retornado se usa como account_group.id al crear productos.
Ejemplo flujo: crear categoría → obtener id → usar como account_group.id en producto."""] = "{}",
) -> str:
    """Gestiona categorías/grupos de inventario en Siigo Nube. Crear, consultar, listar y editar. ⚠️ El ID de la categoría se usa como account_group.id al crear productos. Código asignado automáticamente. No se pueden eliminar."""
    return _execute_siigo_tool("categorias_inventario", operacion, parametros)


# ==================== LISTA DE TOOLS SIIGO ====================

SIIGO_TOOLS = [
    siigo_catalogos,
    siigo_clientes,
    siigo_productos,
    siigo_facturas_venta,
    siigo_facturas_compra,
    siigo_notas_credito,
    siigo_cotizaciones,
    siigo_recibos_caja,
    siigo_recibos_pago,
    siigo_comprobantes_contables,
    siigo_cuentas_por_pagar,
    siigo_categorias_inventario,
]
