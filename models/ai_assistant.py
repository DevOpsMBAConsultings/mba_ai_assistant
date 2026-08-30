# -*- coding: utf-8 -*-
from odoo import models, fields, api
import requests
import json
import ast
import logging

_logger = logging.getLogger(__name__)

# Cache simple en memoria para conservar el historial completo de la API de Gemini (incluidas las llamadas a funciones)
SESSION_HISTORY_CACHE = {}


def _loose_json_parse(value):
    """
    Convierte un string a estructura Python (lista/diccionario) aceptando tanto JSON
    estricto (comillas dobles, ej. [["qty_available", ">", 0]]) como el formato estilo
    Python con comillas simples que a veces devuelven los LLM (ej. [['qty_available', '>', 0]]).
    Antes, un domain/vals con comillas simples tumbaba json.loads() con JSONDecodeError
    y la operación fallaba con un error genérico ('problema con el formato de la solicitud'),
    aunque el domain fuera perfectamente válido en intención. Con esto se acepta cualquiera
    de los dos estilos en vez de exigirle al modelo un formato exacto.
    """
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return ast.literal_eval(value)

class AIAssistant(models.AbstractModel):
    _name = 'mba.ai.assistant'
    _description = 'Controlador Backend del Asistente de IA'

    @api.model
    def ask_llm(self, prompt, active_model=None, active_id=None, history=None):
        """
        Punto de entrada RPC. Consulta al proveedor de IA por defecto, inyectando el historial si está disponible.
        """
        params = self.env['ir.config_parameter'].sudo()
        provider = params.get_param('mba_ai_assistant.provider_default', 'gemini')
        user_id = self.env.user.id
        
        # 1. Recolectar contexto de la vista si se envió modelo y record ID
        context_info = ""
        if active_model and active_id:
            try:
                record = self.env[active_model].browse(int(active_id))
                if record.exists():
                    ruido = ('write_', 'create_', 'message_', 'activity_', 'website_', 'rating_', 'access_')
                    read_fields = [
                        f for f in record._fields
                        if record._fields[f].type in ['char', 'text', 'selection', 'integer', 'float', 'monetary', 'boolean', 'date', 'datetime']
                        and not f.startswith(ruido)
                        and f not in ('id', '__last_update')
                    ]
                    data = record.read(read_fields[:30])[0]
                    context_info = f"\n[Contexto del ERP - Vista actual: {active_model} (ID: {active_id})]\n"
                    context_info += json.dumps(data, indent=2, default=str)
            except Exception as e:
                _logger.warning(f"No se pudo extraer contexto del registro actual: {str(e)}")

        # 2. Generar el mapa dinámico de modelos instalados en Odoo para darle contexto a la IA
        mapa_modelos = self._obtener_mapa_modelos()

        # Fecha real del servidor: sin esto, el modelo tiene que ADIVINAR el año cuando el
        # usuario dice "agosto", "este mes", "el mes pasado", etc. y puede acertar mal
        # (ej. usar un año equivocado y devolver 0 resultados en vez del total real).
        fecha_hoy = fields.Date.context_today(self)
        dias_semana = {0: 'lunes', 1: 'martes', 2: 'miércoles', 3: 'jueves', 4: 'viernes', 5: 'sábado', 6: 'domingo'}

        system_instruction = (
            f"HOY es {fecha_hoy.isoformat()} ({dias_semana[fecha_hoy.weekday()]}). Usa siempre esta fecha como referencia "
            "para resolver expresiones de tiempo relativas o incompletas: 'este mes', 'el mes pasado', 'esta semana', "
            "'este año', 'los últimos 30 días', o un mes sin año explícito (ej. si el usuario dice solo 'agosto', "
            "asume el agosto del año actual según la fecha de hoy, NO un año de tu propio conocimiento interno). "
            "Nunca asumas ni inventes la fecha actual por tu cuenta.\n\n"
            "Eres un asistente de Inteligencia Artificial integrado en el ERP Odoo 18 de MBA Consultings. "
            "Tu objetivo es asistir al usuario consultando o realizando acciones en la base de datos usando la herramienta 'ejecutar_acciones_erp'.\n\n"
            "A continuación tienes una lista de los modelos de negocio principales disponibles en el ERP:\n"
            f"{json.dumps(mapa_modelos, indent=2, ensure_ascii=False)}\n\n"
            "INSTRUCCIONES CLAVE:\n"
            "1. Tienes permisos para leer, crear y escribir registros en la base de datos de Odoo usando la herramienta 'ejecutar_acciones_erp'.\n"
            "2. Si el usuario te pide crear un registro (ej. crear un producto, cliente o lead), "
            "identifica el modelo y los campos correspondientes. Llama a la herramienta con operation='create' y pasa los valores en el parámetro 'vals'.\n"
            "3. Si el usuario te pide modificar o actualizar un registro existente, busca primero su ID (o pídelo) y llama a la herramienta con operation='write', pasando el parámetro 'record_id' y los nuevos valores en 'vals'.\n"
            "4. Si el usuario pide datos (ej. cuántos productos hay, cuáles son, etc.), usa de inmediato operation='search_read' o operation='count'. Mapea adecuadamente la consulta al modelo correcto.\n"
            "5. Si la consulta del usuario requiere una consulta lógica compleja o relacional (ej. 'productos que NO tienen ventas generadas'):\n"
            "   - Paso A: Identifica qué datos intermedios necesitas. Por ejemplo, para saber qué productos no se han vendido, primero debes consultar las líneas de pedido de ventas ('sale.order.line') para obtener los IDs de los productos que sí se han vendido.\n"
            "   - Paso B: Llama a la herramienta para obtener esos registros intermedios (ej. operation='search_read' en 'sale.order.line' leyendo solo el campo 'product_id').\n"
            "   - Paso C: Una vez obtengas los IDs de los productos vendidos, realiza una segunda llamada a la herramienta sobre 'product.product' utilizando el operador 'not in' en el dominio (ej. '[[\"id\", \"not in\", [ID1, ID2, ID3]]]').\n"
            "   - Ejecuta estas llamadas secuencialmente en el mismo turno hasta obtener los resultados y luego redacta la respuesta.\n"
            "6. Redacta la respuesta final en un lenguaje natural amigable y profesional basándote en lo que devuelva la herramienta.\n"
            "7. IMPORTANTE: Nunca concluyas que un campo 'no existe' o 'no está disponible' solo porque no aparece en la lista de campos del mapa de modelos de arriba (ese mapa viene resumido/truncado y puede omitir campos reales, especialmente en modelos muy personalizados como account.move en instalaciones con múltiples módulos de contabilidad). Si necesitas un campo específico (ej. amount_total, invoice_date, qty_available) y no aparece en el resumen, llama primero a la herramienta con operation='list_fields' sobre ese modelo para obtener su esquema COMPLETO y real, y luego usa el nombre de campo correcto para tu consulta. Solo informa al usuario que un dato no está disponible después de haber verificado con list_fields.\n"
            "8. IMPORTANTE: Para preguntas de totales/sumas (ej. '¿cuánto se ha facturado?', '¿cuál es el valor de mi inventario?', promedios, etc.) NUNCA uses operation='search_read' para traer registros y sumarlos tú mismo: search_read solo trae un número limitado de registros (por defecto 10) y el total que calcules será incorrecto por subestimación. En su lugar usa siempre operation='sum', indicando en fields_list el campo numérico a sumar (ej. fields_list=['amount_total']) y el domain para filtrar; la suma se calcula sobre TODOS los registros que cumplen el filtro, no solo los primeros. Para '¿cuánto se ha facturado en <mes>?' usa el modelo 'account.move' con domain que incluya move_type en ['out_invoice','out_refund'], state='posted', y el campo 'invoice_date' dentro del rango de fechas del mes solicitado; ten en cuenta que las notas de crédito ('out_refund') restan del total de facturación neta. Si el usuario NO especifica 'bruta' o 'neta' (ej. simplemente '¿cuánto se ha facturado?'), asume que quiere la NETA por defecto (es el número que normalmente se reporta): calcula la suma de 'out_invoice' y la suma de 'out_refund' por separado y réstalas. Solo devuelve la bruta (solo 'out_invoice', sin restar) si el usuario usa explícitamente la palabra 'bruta'. Para '¿cuál es el valor de mi inventario físico?' usa el modelo 'product.product', domain=[['qty_available','>',0]] (y opcionalmente ['type','=','product'] si el campo existe), y operation='sum' con fields_list=['qty_available','standard_price'] para que se calcule cantidad*costo sumado sobre TODOS los productos con stock, en vez de pedir productos uno por uno con search_read y multiplicar manualmente (eso solo trae unos pocos productos por el límite de resultados y da un total muy por debajo del real). 9. IMPORTANTE: Para preguntas sobre COBROS ESPERADOS, VENCIMIENTOS, o 'ventas a X días que se cobran en <mes>' (ej. 'cuánto se vendió a 30 días en agosto que se cobra en septiembre', 'qué facturas vencen este mes', 'cuánto tengo por cobrar el próximo mes'), NO intentes averiguar el nombre o ID del término de pago ('payment_term_id') para calcular manualmente la fecha de vencimiento — Odoo ya la calcula automáticamente en el campo 'invoice_date_due' de 'account.move' (funciona sin importar si el plazo es 15, 30, 45 días, etc.). Usa domain con 'invoice_date' en el rango del mes de la venta/factura Y 'invoice_date_due' en el rango del mes de cobro esperado, sobre move_type='out_invoice' y state='posted', y operation='sum' con fields_list=['amount_total']. Solo pregunta por el término de pago si el usuario pide explícitamente filtrar por un plazo específico y necesitas identificar cuál 'account.payment.term' corresponde a ese plazo."
        )

        if context_info:
            system_instruction += f"\nUsa además el siguiente contexto del registro abierto en pantalla si es relevante:\n{context_info}"

        if not history and user_id in SESSION_HISTORY_CACHE:
            SESSION_HISTORY_CACHE[user_id] = []

        # Traza de las llamadas a ejecutar_acciones_erp hechas en este turno, para el
        # historial de auditoría (mba.ai.interaction.log) y el feedback 👍/👎 del widget.
        tool_trace = []

        if provider == 'anthropic':
            respuesta = self._call_claude(prompt, system_instruction, params, history, tool_trace)
        elif provider == 'openai':
            respuesta = self._call_openai(prompt, system_instruction, params, history, tool_trace)
        else:
            respuesta = self._call_gemini(prompt, system_instruction, params, user_id, tool_trace)

        try:
            log = self.env['mba.ai.interaction.log'].create({
                'user_id': self.env.uid,
                'proveedor': provider,
                'modelo_activo': active_model,
                'pregunta': prompt,
                'respuesta': respuesta,
                'herramientas_usadas': json.dumps(tool_trace, indent=2, ensure_ascii=False, default=str),
            })
            log_id = log.id
        except Exception:
            _logger.exception("No se pudo registrar el historial de interacción de la IA")
            log_id = False

        return {'respuesta': respuesta, 'log_id': log_id}

    def _obtener_mapa_modelos(self):
        """
        Extrae un mapa dinámico y ligero de los modelos instalados de Odoo excluyendo modelos internos.
        """
        try:
            domain = [
                ('transient', '=', False),
                ('model', 'in', [
                    'product.product', 'res.partner', 'sale.order', 'sale.order.line', 'purchase.order', 
                    'account.move', 'account.account', 'account.journal', 'account.tax', 'crm.lead', 'stock.picking', 'project.project', 'project.task'
                ])
            ]
            modelos = self.env['ir.model'].sudo().search(domain)
            res = []
            for m in modelos:
                obj = self.env.get(m.model)
                fields_info = []
                if obj is not None:
                    ruido = ('write_', 'create_', 'message_', 'activity_', 'website_', 'rating_', 'access_')
                    fields_info = [
                        {"field": name, "type": f.type, "string": f.string}
                        for name, f in obj._fields.items()
                        if f.type in ['char', 'selection', 'integer', 'float', 'monetary', 'date', 'datetime', 'boolean', 'one2many', 'many2one']
                        and not name.startswith(ruido)
                        and name not in ('id', '__last_update')
                    ][:30]
                res.append({
                    "model": m.model,
                    "name": m.name,
                    "fields": fields_info
                })
            return res
        except Exception as e:
            _logger.error(f"Error al generar mapa de modelos: {str(e)}")
            return []

    @api.model
    def ejecutar_acciones_erp(self, model, operation, domain=None, record_id=None, vals=None, fields_list=None, limit=10):
        """
        Herramienta expuesta al LLM. Ejecuta de forma segura operaciones ORM de lectura, creación y edición
        respetando las reglas de acceso nativas de Odoo.
        """
        if model not in self.env:
            return {"error": f"El modelo '{model}' no existe en este ERP."}
        
        try:
            required_perm = 'write' if operation == 'write' else ('create' if operation == 'create' else 'read')
            self.env[model].check_access_rights(required_perm)
            if operation == 'write' and record_id:
                record = self.env[model].browse(int(record_id))
                record.check_access_rule('write')
        except Exception:
            return {"error": f"Acceso denegado: No tienes permisos suficientes para realizar la acción '{operation}' en '{model}'."}

        model_obj = self.env[model]

        try:
            if operation == 'create':
                if not vals:
                    return {"error": "No se enviaron valores ('vals') para crear el registro."}
                
                if isinstance(vals, str):
                    vals = _loose_json_parse(vals)
                
                clean_vals = {}
                for k, v in vals.items():
                    if k in model_obj._fields:
                        if model_obj._fields[k].type in ['one2many', 'many2many'] and isinstance(v, str):
                            try:
                                clean_vals[k] = _loose_json_parse(v)
                            except Exception:
                                clean_vals[k] = v
                        else:
                            clean_vals[k] = v
                            
                new_record = model_obj.create(clean_vals)
                return {
                    "result_type": "create",
                    "success": True,
                    "id": new_record.id,
                    "display_name": new_record.display_name or new_record.name
                }

            elif operation == 'write':
                if not record_id:
                    return {"error": "Falta el ID del registro ('record_id') para realizar la actualización."}
                if not vals:
                    return {"error": "No se enviaron valores ('vals') para actualizar el registro."}
                
                if isinstance(vals, str):
                    vals = _loose_json_parse(vals)
                
                clean_vals = {}
                for k, v in vals.items():
                    if k in model_obj._fields:
                        if model_obj._fields[k].type in ['one2many', 'many2many'] and isinstance(v, str):
                            try:
                                clean_vals[k] = _loose_json_parse(v)
                            except Exception:
                                clean_vals[k] = v
                        else:
                            clean_vals[k] = v

                record = model_obj.browse(int(record_id))
                if not record.exists():
                    return {"error": f"El registro con ID {record_id} en '{model}' no existe."}
                
                record.write(clean_vals)
                return {
                    "result_type": "write",
                    "success": True,
                    "id": record.id,
                    "display_name": record.display_name
                }

            elif operation == 'count':
                safe_domain = []
                if domain:
                    if isinstance(domain, str):
                        safe_domain = _loose_json_parse(domain)
                    elif isinstance(domain, list):
                        safe_domain = domain
                count = model_obj.search_count(safe_domain)
                return {"result_type": "count", "value": count}

            elif operation == 'list_fields':
                # Esquema REAL y completo del modelo, sin truncar. Se usa cuando un campo que
                # se necesita (ej. amount_total, qty_available) no aparece en el mapa de modelos
                # resumido que se le da a la IA al inicio de la conversación.
                ruido = ('write_', 'create_', 'message_', 'activity_', 'website_', 'rating_', 'access_')
                campos = [
                    {"field": name, "type": f.type, "string": f.string, "required": bool(f.required)}
                    for name, f in model_obj._fields.items()
                    if not name.startswith(ruido) and name not in ('id', '__last_update')
                ]
                return {"result_type": "fields", "model": model, "count": len(campos), "fields": campos}

            elif operation == 'sum':
                # Suma real sobre TODOS los registros que cumplen el filtro (no solo los primeros N).
                # Evita el error de sumar manualmente un search_read limitado (subestima el total).
                # Si fields_list trae DOS campos numéricos (ej. ['qty_available', 'standard_price']),
                # se multiplica campo1*campo2 por registro y se suma ese producto sobre todos los
                # registros (esto es lo que se necesita para "valor de inventario" = cantidad * costo).
                if not fields_list:
                    return {"error": "Debes indicar en 'fields_list' el/los campo(s) numérico(s) a sumar, ej. ['amount_total'] o ['qty_available','standard_price'] para valor = cantidad*costo."}

                campos_numericos = fields_list[:2]
                for campo in campos_numericos:
                    if campo not in model_obj._fields or model_obj._fields[campo].type not in ('integer', 'float', 'monetary'):
                        return {"error": f"El campo '{campo}' no existe en '{model}' o no es numérico. Usa operation='list_fields' para verificar los campos disponibles."}

                safe_domain = []
                if domain:
                    if isinstance(domain, str):
                        safe_domain = _loose_json_parse(domain)
                    elif isinstance(domain, list):
                        safe_domain = domain

                registros = model_obj.search(safe_domain)

                if len(campos_numericos) == 2:
                    campo_a, campo_b = campos_numericos
                    total = sum(r[campo_a] * r[campo_b] for r in registros)
                    return {"result_type": "sum", "model": model, "field": f"{campo_a}*{campo_b}", "sum": total, "count_records": len(registros)}
                else:
                    campo = campos_numericos[0]
                    total = sum(registros.mapped(campo))
                    return {"result_type": "sum", "model": model, "field": campo, "sum": total, "count_records": len(registros)}

            else:
                safe_domain = []
                if domain:
                    if isinstance(domain, str):
                        safe_domain = _loose_json_parse(domain)
                    elif isinstance(domain, list):
                        safe_domain = domain
                
                valid_fields = []
                if fields_list:
                    valid_fields = [f for f in fields_list if f in model_obj._fields]
                if not valid_fields:
                    valid_fields = [f for f in ['name', 'display_name', 'amount_total', 'qty_available', 'state', 'stage_id', 'date'] if f in model_obj._fields]
                
                records = model_obj.search_read(safe_domain, valid_fields, limit=limit)
                return {"result_type": "records", "data": records, "count": len(records)}

        except Exception as e:
            _logger.exception(f"Error al ejecutar acción ORM '{operation}' en el modelo {model}")
            return {"error": f"Error al ejecutar la acción: {str(e)}"}

    @api.model
    def list_available_models(self, provider, api_key):
        if not api_key:
            return []
        
        if provider == 'gemini':
            url = f"https://generativelanguage.googleapis.com/v1/models?key={api_key}"
            try:
                res = requests.get(url, timeout=10)
                if res.status_code == 200:
                    models_data = res.json().get('models', [])
                    return [
                        m['name'].replace('models/', '') 
                        for m in models_data 
                        if 'generateContent' in m.get('supportedGenerationMethods', [])
                    ]
                _logger.warning(f"Error al listar modelos de Gemini ({res.status_code}): {res.text}")
            except Exception as e:
                _logger.exception("Error de conexión al listar modelos de Gemini")
        
        elif provider == 'openai':
            url = "https://api.openai.com/v1/models"
            headers = {"Authorization": f"Bearer {api_key}"}
            try:
                res = requests.get(url, headers=headers, timeout=10)
                if res.status_code == 200:
                    models_data = res.json().get('data', [])
                    return [m['id'] for m in models_data if 'gpt' in m['id']]
                _logger.warning(f"Error al listar modelos de OpenAI ({res.status_code}): {res.text}")
            except Exception as e:
                _logger.exception("Error de conexión al listar modelos de OpenAI")

        elif provider == 'anthropic':
            return [
                "claude-3-5-sonnet-20241022",
                "claude-3-5-haiku-20241022",
                "claude-3-opus-20240229"
            ]

        return []

    def _get_tools_schema(self):
        return [
            {
                "name": "ejecutar_acciones_erp",
                "description": "Permite consultar, contar, crear y actualizar registros en la base de datos de Odoo de forma segura.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "model": {
                            "type": "STRING",
                            "description": "El nombre técnico del modelo de Odoo a procesar (ej. 'product.product', 'res.partner', 'sale.order')."
                        },
                        "operation": {
                            "type": "STRING",
                            "description": "La acción ORM a ejecutar: 'search_read' (para buscar/leer), 'count' (para contar), 'create' (para crear nuevo registro) o 'write' (para actualizar registro existente) o 'list_fields' (para consultar TODOS los campos reales de un modelo, sin resumir, cuando un campo que necesitas no aparece en el mapa de modelos inicial) o 'sum' (para sumar sobre TODOS los registros que cumplan el domain: con fields_list=[campo] suma un solo campo, ej. total facturado; con fields_list=[campo_cantidad, campo_costo] multiplica ambos campos por registro y suma ese producto, ej. valor de inventario = qty_available * standard_price).",
                            "enum": ["search_read", "count", "create", "write", "list_fields", "sum"]
                        },
                        "domain": {
                            "type": "STRING",
                            "description": "Filtro de búsqueda en formato de dominio de Odoo serializado en JSON (ej. '[[\"qty_available\", \">\", 0]]'). Solo aplica para 'search_read' y 'count'."
                        },
                        "record_id": {
                            "type": "INTEGER",
                            "description": "El ID numérico del registro de Odoo a modificar. Requerido únicamente si la operación es 'write'."
                        },
                        "vals": {
                            "type": "STRING",
                            "description": "Diccionario JSON con los nombres de campos y valores a insertar o actualizar (ej. '{\"name\": \"Casa\", \"list_price\": 12.0}'). Requerido si la operación es 'create' o 'write'."
                        },
                        "fields_list": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"},
                            "description": "Lista de campos específicos a leer (default id, name/display_name)."
                        },
                        "limit": {
                            "type": "INTEGER",
                            "description": "Límite máximo de registros a retornar (default 10). Solo aplica para 'search_read'."
                        }
                    },
                    "required": ["model", "operation"]
                }
            }
        ]

    def _call_gemini(self, prompt, system_instruction, params, user_id, tool_trace=None):
        api_key = params.get_param('mba_ai_assistant.gemini_api_key') or params.get_param('google.gemini_api_key')
        if not api_key:
            return "Error: No se ha configurado la API Key para Google Gemini en Ajustes."
        
        available = self.list_available_models('gemini', api_key)
        model = "gemini-1.5-flash"
        if available and "gemini-1.5-flash" not in available:
            model = available[0]
            
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        headers = {'Content-Type': 'application/json'}
        
        if user_id not in SESSION_HISTORY_CACHE:
            SESSION_HISTORY_CACHE[user_id] = []
            
        history_contents = SESSION_HISTORY_CACHE[user_id]
        initial_instruction = f"[INSTRUCCIÓN DE SISTEMA]\n{system_instruction}\n\n"
        
        current_prompt = prompt
        if not history_contents:
            current_prompt = initial_instruction + current_prompt
            
        history_contents.append({
            "role": "user",
            "parts": [{"text": current_prompt}]
        })
        
        max_loops = 8
        loop_count = 0
        
        try:
            while loop_count < max_loops:
                payload = {
                    "contents": history_contents,
                    "tools": [{"functionDeclarations": self._get_tools_schema()}],
                    "generationConfig": {"temperature": 0.1}
                }
                
                res = requests.post(url, json=payload, headers=headers, timeout=60)
                if res.status_code != 200:
                    if loop_count == 0:
                        history_contents.pop()
                    return f"Error en Gemini API ({res.status_code}): {res.text}"
                    
                result = res.json()
                model_response = result['candidates'][0]['content']
                part = model_response['parts'][0]
                
                if 'functionCall' in part:
                    func_call = part['functionCall']
                    func_name = func_call['name']
                    func_args = func_call.get('args', {})
                    
                    if func_name == 'ejecutar_acciones_erp':
                        action_result = self.ejecutar_acciones_erp(
                            model=func_args.get('model'),
                            operation=func_args.get('operation'),
                            domain=func_args.get('domain'),
                            record_id=func_args.get('record_id'),
                            vals=func_args.get('vals'),
                            fields_list=func_args.get('fields_list'),
                            limit=func_args.get('limit', 10)
                        )
                        if tool_trace is not None:
                            tool_trace.append({
                                "operation": func_args.get('operation'),
                                "model": func_args.get('model'),
                                "domain": func_args.get('domain'),
                                "fields_list": func_args.get('fields_list'),
                                "resultado": action_result if isinstance(action_result, (dict, list)) else str(action_result)[:300],
                            })
                        
                        history_contents.append(model_response)
                        history_contents.append({
                            "role": "model",
                            "parts": [{
                                "functionResponse": {
                                    "name": "ejecutar_acciones_erp",
                                    "response": {"output": action_result}
                                }
                            }]
                        })
                        
                        loop_count += 1
                        continue
                
                history_contents.append(model_response)
                break
            else:
                # Se agotaron los loops de function-calling sin que el modelo cerrara con texto.
                # Forzamos una última llamada SIN herramientas para obligarlo a resumir en lenguaje natural
                # lo que alcanzó a hacer, en vez de devolver un mensaje vacío/genérico al usuario.
                history_contents.append({
                    "role": "user",
                    "parts": [{"text": "Resume en una respuesta breve y en lenguaje natural lo que lograste hacer hasta ahora. No uses más herramientas."}]
                })
                res = requests.post(url, json={
                    "contents": history_contents,
                    "generationConfig": {"temperature": 0.1}
                }, headers=headers, timeout=60)
                if res.status_code == 200:
                    result = res.json()
                    model_response = result['candidates'][0]['content']
                    part = model_response['parts'][0]
                    history_contents.append(model_response)
                
            if len(history_contents) > 30:
                history_contents = history_contents[-30:]
            SESSION_HISTORY_CACHE[user_id] = history_contents
            
            return part.get('text', 'No se generó texto de respuesta. Es posible que la tarea haya quedado parcialmente completada — revisa los registros creados.')
            
        except Exception as e:
            if loop_count == 0 and history_contents:
                history_contents.pop()
            return f"Excepción al conectar con Gemini: {str(e)}"

    def _call_claude(self, prompt, system_instruction, params, history=None, tool_trace=None):
        api_key = params.get_param('mba_ai_assistant.anthropic_api_key')
        if not api_key:
            return "Error: No se ha configurado la API Key para Anthropic Claude en Ajustes."
        
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json'
        }
        
        claude_tools = [
            {
                "name": "ejecutar_acciones_erp",
                "description": "Permite consultar, contar, crear y actualizar registros en la base de datos de Odoo de forma segura.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "model": {
                            "type": "string",
                            "description": "El nombre técnico del modelo de Odoo a procesar (ej. 'product.product', 'res.partner')."
                        },
                        "operation": {
                            "type": "string",
                            "description": "La acción ORM a ejecutar: 'search_read', 'count', 'create', 'write', 'list_fields' (esquema completo de campos) o 'sum' (suma sobre TODOS los registros del domain: fields_list=[campo] suma un campo; fields_list=[campo_cantidad, campo_costo] multiplica ambos por registro y suma el producto, ej. valor de inventario).",
                            "enum": ["search_read", "count", "create", "write", "list_fields", "sum"]
                        },
                        "domain": {
                            "type": "string",
                            "description": "Filtro de búsqueda serializado en JSON (solo para search_read y count)."
                        },
                        "record_id": {
                            "type": "integer",
                            "description": "ID del registro a modificar (solo para write)."
                        },
                        "vals": {
                            "type": "string",
                            "description": "Diccionario JSON con valores a insertar o actualizar (solo para create y write)."
                        },
                        "fields_list": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Lista de campos específicos a leer."
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Límite máximo de registros a retornar."
                        }
                    },
                    "required": ["model", "operation"]
                }
            }
        ]
        
        messages = []
        if history:
            for msg in history:
                role = "user" if msg.get('role') == 'user' else "assistant"
                messages.append({
                    "role": role,
                    "content": msg.get('content', '')
                })
        
        messages.append({"role": "user", "content": prompt})
        
        max_loops = 8
        loop_count = 0
        
        try:
            while loop_count < max_loops:
                payload = {
                    "model": "claude-3-5-sonnet-20241022",
                    "max_tokens": 1024,
                    "system": system_instruction,
                    "messages": messages,
                    "tools": claude_tools,
                    "temperature": 0.1
                }
                
                res = requests.post(url, json=payload, headers=headers, timeout=60)
                if res.status_code != 200:
                    return f"Error en Claude API ({res.status_code}): {res.text}"
                    
                result = res.json()
                tool_calls = [content for content in result.get('content', []) if content.get('type') == 'tool_use']
                
                if tool_calls:
                    tool_use = tool_calls[0]
                    tool_id = tool_use['id']
                    tool_name = tool_use['name']
                    tool_input = tool_use['input']
                    
                    if tool_name == 'ejecutar_acciones_erp':
                        action_result = self.ejecutar_acciones_erp(
                            model=tool_input.get('model'),
                            operation=tool_input.get('operation'),
                            domain=tool_input.get('domain'),
                            record_id=tool_input.get('record_id'),
                            vals=tool_input.get('vals'),
                            fields_list=tool_input.get('fields_list'),
                            limit=tool_input.get('limit', 10)
                        )
                        if tool_trace is not None:
                            tool_trace.append({
                                "operation": tool_input.get('operation'),
                                "model": tool_input.get('model'),
                                "domain": tool_input.get('domain'),
                                "fields_list": tool_input.get('fields_list'),
                                "resultado": action_result if isinstance(action_result, (dict, list)) else str(action_result)[:300],
                            })
                        
                        messages.append({"role": "assistant", "content": result['content']})
                        messages.append({
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_id,
                                    "content": json.dumps(action_result)
                                }
                            ]
                        })
                        loop_count += 1
                        continue
                
                break
            else:
                # Se agotaron los loops de tool-use sin que el modelo cerrara con texto.
                # Forzamos una última llamada SIN herramientas para obligarlo a resumir lo que alcanzó a hacer.
                messages.append({"role": "user", "content": "Resume en una respuesta breve y en lenguaje natural lo que lograste hacer hasta ahora. No uses más herramientas."})
                res = requests.post(url, json={
                    "model": "claude-3-5-sonnet-20241022",
                    "max_tokens": 1024,
                    "system": system_instruction,
                    "messages": messages,
                    "temperature": 0.1
                }, headers=headers, timeout=60)
                if res.status_code == 200:
                    result = res.json()
                
            text_contents = [content for content in result.get('content', []) if content.get('type') == 'text']
            return text_contents[0]['text'] if text_contents else "No se obtuvo respuesta. Es posible que la tarea haya quedado parcialmente completada — revisa los registros creados."
            
        except Exception as e:
            return f"Excepción al conectar con Claude: {str(e)}"

    def _call_openai(self, prompt, system_instruction, params, history=None, tool_trace=None):
        api_key = params.get_param('mba_ai_assistant.openai_api_key') or params.get_param('mail.openai_api_key')
        if not api_key:
            return "Error: No se ha configurado la API Key para OpenAI en Ajustes."
        
        available = self.list_available_models('openai', api_key)
        model = "gpt-4o-mini"
        if available and "gpt-4o-mini" not in available:
            model = available[0]
            
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
        
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": "ejecutar_acciones_erp",
                    "description": "Permite consultar, contar, crear y actualizar registros en la base de datos de Odoo de forma segura.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "El nombre técnico del modelo de Odoo a procesar (ej. 'product.product', 'res.partner')."
                            },
                            "operation": {
                                "type": "string",
                                "description": "La acción ORM a ejecutar: 'search_read', 'count', 'create', 'write', 'list_fields' (esquema completo de campos) o 'sum' (suma sobre TODOS los registros del domain: fields_list=[campo] suma un campo; fields_list=[campo_cantidad, campo_costo] multiplica ambos por registro y suma el producto, ej. valor de inventario).",
                                "enum": ["search_read", "count", "create", "write", "list_fields", "sum"]
                            },
                            "domain": {
                                "type": "string",
                                "description": "Filtro de búsqueda serializado en JSON (solo para search_read y count)."
                            },
                            "record_id": {
                                "type": "integer",
                                "description": "ID del registro a modificar (solo para write)."
                            },
                            "vals": {
                                "type": "string",
                                "description": "Diccionario JSON con valores a insertar o actualizar (solo para create y write)."
                            },
                            "fields_list": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Lista de campos específicos a leer."
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Límite máximo de registros a retornar."
                            }
                        },
                        "required": ["model", "operation"]
                    }
                }
            }
        ]
        
        messages = [{"role": "system", "content": system_instruction}]
        if history:
            for msg in history:
                role = "user" if msg.get('role') == 'user' else "assistant"
                messages.append({
                    "role": role,
                    "content": msg.get('content', '')
                })
                
        messages.append({"role": "user", "content": prompt})
        
        max_loops = 8
        loop_count = 0
        
        try:
            while loop_count < max_loops:
                payload = {
                    "model": model,
                    "messages": messages,
                    "tools": openai_tools,
                    "temperature": 0.1
                }
                
                res = requests.post(url, json=payload, headers=headers, timeout=60)
                if res.status_code != 200:
                    return f"Error en OpenAI API ({res.status_code}): {res.text}"
                    
                result = res.json()
                message = result['choices'][0]['message']
                
                if message.get('tool_calls'):
                    tool_call = message['tool_calls'][0]
                    tool_call_id = tool_call['id']
                    func_name = tool_call['function']['name']
                    func_args = json.loads(tool_call['function']['arguments'])
                    
                    if func_name == 'ejecutar_acciones_erp':
                        query_result = self.ejecutar_acciones_erp(
                            model=func_args.get('model'),
                            operation=func_args.get('operation'),
                            domain=func_args.get('domain'),
                            record_id=func_args.get('record_id'),
                            vals=func_args.get('vals'),
                            fields_list=func_args.get('fields_list'),
                            limit=func_args.get('limit', 10)
                        )
                        if tool_trace is not None:
                            tool_trace.append({
                                "operation": func_args.get('operation'),
                                "model": func_args.get('model'),
                                "domain": func_args.get('domain'),
                                "fields_list": func_args.get('fields_list'),
                                "resultado": query_result if isinstance(query_result, (dict, list)) else str(query_result)[:300],
                            })
                        
                        messages.append(message)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "name": "ejecutar_acciones_erp",
                            "content": json.dumps(query_result)
                        })
                        loop_count += 1
                        continue
                
                break
            else:
                # Se agotaron los loops de tool-calling sin que el modelo cerrara con texto.
                # Forzamos una última llamada SIN herramientas para obligarlo a resumir lo que alcanzó a hacer.
                messages.append({"role": "user", "content": "Resume en una respuesta breve y en lenguaje natural lo que lograste hacer hasta ahora. No uses más herramientas."})
                res = requests.post(url, json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.1
                }, headers=headers, timeout=60)
                if res.status_code == 200:
                    result = res.json()
                    message = result['choices'][0]['message']
                
            return message.get('content', '') or "No se generó una respuesta final. Es posible que la tarea haya quedado parcialmente completada — revisa los registros creados."
            
        except Exception as e:
            return f"Excepción al conectar con OpenAI: {str(e)}"
