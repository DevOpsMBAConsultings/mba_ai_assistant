# -*- coding: utf-8 -*-
from odoo import models, fields, api


class AIInteractionLog(models.Model):
    _name = 'mba.ai.interaction.log'
    _description = 'Historial de Interacciones del Asistente de IA'
    _order = 'create_date desc'
    _rec_name = 'pregunta'

    user_id = fields.Many2one('res.users', string='Usuario', required=True, index=True, ondelete='cascade')
    proveedor = fields.Selection([
        ('gemini', 'Gemini'),
        ('openai', 'OpenAI'),
        ('anthropic', 'Claude'),
    ], string='Proveedor')
    modelo_activo = fields.Char(string='Vista/Modelo Activo')
    pregunta = fields.Text(string='Pregunta del Usuario')
    respuesta = fields.Text(string='Respuesta de la IA')
    herramientas_usadas = fields.Text(
        string='Detalle Técnico',
        help='Registro de cada llamada a ejecutar_acciones_erp hecha para responder esta pregunta '
             '(operación, modelo, domain y resultado). Sirve para depurar sin tener que entrar al log de Docker.'
    )
    feedback = fields.Selection([
        ('util', '👍 Útil'),
        ('no_util', '👎 No útil'),
    ], string='Feedback', index=True)
    comentario = fields.Text(string='Comentario del Usuario')

    @api.model
    def registrar_feedback(self, log_id, valor, comentario=None):
        """Método RPC llamado desde el widget del systray cuando el usuario da 👍 o 👎
        a una respuesta puntual de la IA."""
        if valor not in ('util', 'no_util'):
            return {"error": "Valor de feedback inválido."}
        if not log_id:
            return {"error": "Falta el identificador de la interacción."}
        registro = self.sudo().browse(int(log_id))
        if not registro.exists():
            return {"error": "No se encontró el registro de interacción."}
        vals = {'feedback': valor}
        if comentario:
            vals['comentario'] = comentario
        registro.write(vals)
        return {"success": True}
