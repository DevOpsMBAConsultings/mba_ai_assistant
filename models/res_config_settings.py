# -*- coding: utf-8 -*-
from odoo import models, fields, api

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Clave de Anthropic Claude
    mba_anthropic_api_key = fields.Char(
        string="Anthropic Claude API Key",
        config_parameter="mba_ai_assistant.anthropic_api_key",
        help="Clave API oficial para conectar con Anthropic Claude."
    )
    
    # Clave de Google Gemini (Independiente para el asistente)
    mba_gemini_api_key = fields.Char(
        string="Google Gemini API Key",
        config_parameter="mba_ai_assistant.gemini_api_key",
        help="Clave API oficial de Google AI Studio para conectar con Gemini."
    )

    # Clave de OpenAI ChatGPT (Independiente para el asistente)
    mba_openai_api_key = fields.Char(
        string="OpenAI ChatGPT API Key",
        config_parameter="mba_ai_assistant.openai_api_key",
        help="Clave API oficial de OpenAI para conectar con ChatGPT."
    )

    mba_ai_provider_default = fields.Selection([
        ('gemini', 'Google Gemini'),
        ('openai', 'OpenAI ChatGPT'),
        ('anthropic', 'Anthropic Claude')
    ], string="Proveedor de IA por Defecto",
       config_parameter="mba_ai_assistant.provider_default",
       default='gemini',
       help="Selecciona qué modelo utilizar por defecto en el asistente."
    )
