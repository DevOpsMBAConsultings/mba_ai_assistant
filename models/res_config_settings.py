# -*- coding: utf-8 -*-
import json
import logging
import requests
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # --- Proveedor por Defecto ---
    mba_ai_provider_default = fields.Selection([
        ('gemini', 'Google Gemini'),
        ('anthropic', 'Anthropic Claude'),
        ('openai', 'OpenAI ChatGPT'),
        ('deepseek', 'DeepSeek'),
    ], string="Proveedor de IA por Defecto",
       config_parameter="mba_ai_assistant.provider_default",
       default='deepseek',
       help="Selecciona qué modelo utilizar por defecto en todo el sistema.")

    mba_ai_use_per_function_provider = fields.Boolean(
        string="Permitir que cada función elija su propio proveedor",
        config_parameter="mba_ai_assistant.use_per_function_provider",
        default=False,
        help="Si lo activa, cada función de IA que lo soporte podrá tener su propio selector de proveedor."
    )

    # ==========================================
    # 1. DEEPSEEK
    # ==========================================
    mba_deepseek_api_key = fields.Char(
        string="DeepSeek API Key",
        config_parameter="mba_ai_assistant.deepseek_api_key",
        help="Clave API oficial de DeepSeek (platform.deepseek.com)."
    )
    mba_deepseek_model = fields.Selection(
        selection='_get_deepseek_model_selection',
        string="Modelo DeepSeek",
        config_parameter="mba_ai_assistant.deepseek_model",
        default='deepseek-flash',
        help="Modelo DeepSeek activo para llamadas de IA y visión."
    )

    @api.model
    def _get_deepseek_model_selection(self):
        cache = self.env['ir.config_parameter'].sudo().get_param('mba_ai_assistant.deepseek_models_cache')
        if cache:
            try:
                models_data = json.loads(cache)
                options = [(m['id'], m.get('name') or m['id']) for m in models_data]
                if options:
                    return options
            except Exception:
                pass
        return [
            ('deepseek-flash', 'DeepSeek-V4.1-Flash (Vision & Text)'),
            ('deepseek-v4-pro', 'DeepSeek-V4-Pro (Text)'),
            ('deepseek-chat', 'DeepSeek-Chat'),
            ('deepseek-reasoner', 'DeepSeek-Reasoner (R1)'),
        ]

    def action_refresh_deepseek_models(self):
        """Consulta GET https://api.deepseek.com/models con la API Key configurada."""
        self.ensure_one()
        api_key = (self.mba_deepseek_api_key or '').strip()
        if not api_key:
            raise UserError(_("Ingrese primero la DeepSeek API Key antes de actualizar."))
        try:
            res = requests.get(
                'https://api.deepseek.com/models',
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=20,
            )
            res.raise_for_status()
            data = res.json()
            models_list = [
                {'id': m['id'], 'name': m.get('name') or m['id']}
                for m in data.get('data', [])
            ]
            if not models_list:
                models_list = [
                    {'id': 'deepseek-flash', 'name': 'DeepSeek-V4.1-Flash'},
                    {'id': 'deepseek-v4-pro', 'name': 'DeepSeek-V4-Pro'},
                ]
            self.env['ir.config_parameter'].sudo().set_param(
                'mba_ai_assistant.deepseek_models_cache', json.dumps(models_list)
            )
            if self.mba_deepseek_model not in [m['id'] for m in models_list]:
                self.mba_deepseek_model = models_list[0]['id']
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Modelos DeepSeek actualizados'),
                    'message': _('Se detectaron %s modelo(s) disponibles.') % len(models_list),
                    'type': 'success',
                    'sticky': False,
                    'next': {'type': 'ir.actions.client', 'tag': 'reload'},
                }
            }
        except Exception as e:
            raise UserError(_("Error al conectar con DeepSeek: %s") % str(e))

    # ==========================================
    # 2. ANTHROPIC CLAUDE
    # ==========================================
    mba_anthropic_api_key = fields.Char(
        string="Anthropic Claude API Key",
        config_parameter="mba_ai_assistant.anthropic_api_key",
        help="Clave API oficial para conectar con Anthropic Claude."
    )
    mba_anthropic_model = fields.Selection(
        selection='_get_anthropic_model_selection',
        string="Modelo Claude",
        config_parameter="mba_ai_assistant.anthropic_model",
        default='claude-sonnet-4-5-20250929',
        help="Modelo Claude seleccionado."
    )

    @api.model
    def _get_anthropic_model_selection(self):
        cache = self.env['ir.config_parameter'].sudo().get_param('mba_ai_assistant.anthropic_models_cache')
        if cache:
            try:
                models_data = json.loads(cache)
                options = [(m['id'], m.get('display_name') or m['id']) for m in models_data]
                if options:
                    return options
            except Exception:
                pass
        return [
            ('claude-sonnet-4-5-20250929', 'Claude 3.5 Sonnet (Recomendado)'),
            ('claude-3-5-haiku-20241022', 'Claude 3.5 Haiku'),
            ('claude-3-opus-20240229', 'Claude 3 Opus'),
        ]

    def action_refresh_anthropic_models(self):
        """Consulta GET https://api.anthropic.com/v1/models con la API Key configurada."""
        self.ensure_one()
        api_key = (self.mba_anthropic_api_key or '').strip()
        if not api_key:
            raise UserError(_("Ingrese primero la Anthropic API Key antes de actualizar."))
        try:
            res = requests.get(
                'https://api.anthropic.com/v1/models',
                headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01'},
                timeout=20,
            )
            res.raise_for_status()
            data = res.json()
            models_list = [
                {'id': m['id'], 'display_name': m.get('display_name') or m['id']}
                for m in data.get('data', [])
            ]
            self.env['ir.config_parameter'].sudo().set_param(
                'mba_ai_assistant.anthropic_models_cache', json.dumps(models_list)
            )
            if self.mba_anthropic_model not in [m['id'] for m in models_list]:
                self.mba_anthropic_model = models_list[0]['id'] if models_list else 'claude-sonnet-4-5-20250929'
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Modelos Anthropic actualizados'),
                    'message': _('Se detectaron %s modelo(s) disponibles.') % len(models_list),
                    'type': 'success',
                    'sticky': False,
                    'next': {'type': 'ir.actions.client', 'tag': 'reload'},
                }
            }
        except Exception as e:
            raise UserError(_("Error al conectar con Anthropic: %s") % str(e))

    # ==========================================
    # 3. GOOGLE GEMINI
    # ==========================================
    mba_gemini_api_key = fields.Char(
        string="Google Gemini API Key",
        config_parameter="mba_ai_assistant.gemini_api_key",
        help="Clave API oficial de Google AI Studio para conectar con Gemini."
    )
    mba_gemini_model = fields.Selection(
        selection='_get_gemini_model_selection',
        string="Modelo Gemini",
        config_parameter="mba_ai_assistant.gemini_model",
        default='gemini-2.5-flash',
        help="Modelo Gemini seleccionado."
    )

    @api.model
    def _get_gemini_model_selection(self):
        cache = self.env['ir.config_parameter'].sudo().get_param('mba_ai_assistant.gemini_models_cache')
        if cache:
            try:
                models_data = json.loads(cache)
                options = [(m['id'], m.get('display_name') or m['id']) for m in models_data]
                if options:
                    return options
            except Exception:
                pass
        return [
            ('gemini-2.5-flash', 'Gemini 2.5 Flash'),
            ('gemini-2.5-pro', 'Gemini 2.5 Pro'),
        ]

    def action_refresh_gemini_models(self):
        """Consulta GET https://generativelanguage.googleapis.com/v1beta/models."""
        self.ensure_one()
        api_key = (self.mba_gemini_api_key or '').strip()
        if not api_key:
            raise UserError(_("Ingrese primero la Google Gemini API Key antes de actualizar."))
        try:
            res = requests.get(
                f'https://generativelanguage.googleapis.com/v1beta/models?key={api_key}',
                timeout=20,
            )
            res.raise_for_status()
            data = res.json()
            models_list = []
            for m in data.get('models', []):
                name = m.get('name', '').replace('models/', '')
                if 'generateContent' in m.get('supportedGenerationMethods', []):
                    models_list.append({
                        'id': name,
                        'display_name': m.get('displayName') or name
                    })
            self.env['ir.config_parameter'].sudo().set_param(
                'mba_ai_assistant.gemini_models_cache', json.dumps(models_list)
            )
            if self.mba_gemini_model not in [m['id'] for m in models_list]:
                self.mba_gemini_model = models_list[0]['id'] if models_list else 'gemini-2.5-flash'
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Modelos Gemini actualizados'),
                    'message': _('Se detectaron %s modelo(s) disponibles.') % len(models_list),
                    'type': 'success',
                    'sticky': False,
                    'next': {'type': 'ir.actions.client', 'tag': 'reload'},
                }
            }
        except Exception as e:
            raise UserError(_("Error al conectar con Google Gemini: %s") % str(e))

    # ==========================================
    # 4. OPENAI CHATGPT
    # ==========================================
    mba_openai_api_key = fields.Char(
        string="OpenAI ChatGPT API Key",
        config_parameter="mba_ai_assistant.openai_api_key",
        help="Clave API oficial de OpenAI para conectar con ChatGPT."
    )
    mba_openai_model = fields.Selection(
        selection='_get_openai_model_selection',
        string="Modelo OpenAI",
        config_parameter="mba_ai_assistant.openai_model",
        default='gpt-4o',
        help="Modelo OpenAI seleccionado."
    )

    @api.model
    def _get_openai_model_selection(self):
        cache = self.env['ir.config_parameter'].sudo().get_param('mba_ai_assistant.openai_models_cache')
        if cache:
            try:
                models_data = json.loads(cache)
                options = [(m['id'], m['id']) for m in models_data]
                if options:
                    return options
            except Exception:
                pass
        return [
            ('gpt-4o', 'GPT-4o'),
            ('gpt-4o-mini', 'GPT-4o Mini'),
            ('o1', 'OpenAI o1'),
            ('o3-mini', 'OpenAI o3-mini'),
        ]

    def action_refresh_openai_models(self):
        """Consulta GET https://api.openai.com/v1/models."""
        self.ensure_one()
        api_key = (self.mba_openai_api_key or '').strip()
        if not api_key:
            raise UserError(_("Ingrese primero la OpenAI API Key antes de actualizar."))
        try:
            res = requests.get(
                'https://api.openai.com/v1/models',
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=20,
            )
            res.raise_for_status()
            data = res.json()
            models_list = [
                {'id': m['id']}
                for m in data.get('data', [])
                if any(k in m['id'] for k in ('gpt-4', 'gpt-3.5', 'o1', 'o3'))
            ]
            self.env['ir.config_parameter'].sudo().set_param(
                'mba_ai_assistant.openai_models_cache', json.dumps(models_list)
            )
            if self.mba_openai_model not in [m['id'] for m in models_list]:
                self.mba_openai_model = models_list[0]['id'] if models_list else 'gpt-4o'
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Modelos OpenAI actualizados'),
                    'message': _('Se detectaron %s modelo(s) disponibles.') % len(models_list),
                    'type': 'success',
                    'sticky': False,
                    'next': {'type': 'ir.actions.client', 'tag': 'reload'},
                }
            }
        except Exception as e:
            raise UserError(_("Error al conectar con OpenAI: %s") % str(e))

