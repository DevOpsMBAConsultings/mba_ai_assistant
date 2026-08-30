/** @odoo-module **/
import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";

const STORAGE_PREFIX = "mba_ai_assistant.messages.";
const MAX_STORED_MESSAGES = 30;

const DEFAULT_GREETING = { role: "assistant", content: "¡Hola! Soy tu asistente de IA. ¿En qué te puedo ayudar hoy con el sistema?" };

export class AiAssistantSystray extends Component {
    static template = "mba_ai_assistant.AiAssistantSystray";

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.user = user; // "user" es un objeto reactivo importado, no un servicio inyectable en Odoo 18

        this.state = useState({
            isOpen: false,
            minimized: false,
            inputPrompt: "",
            messages: [DEFAULT_GREETING],
            loading: false,
            hasAccess: false
        });

        // Ocultar el ícono por completo si el usuario no tiene el grupo de acceso asignado
        // (Ajustes -> Usuarios -> [usuario] -> Derechos de acceso -> "Usuario del Asistente de IA").
        // onWillStart se espera ANTES del primer render, así que no hay parpadeo del ícono.
        onWillStart(async () => {
            this.state.hasAccess = await user.hasGroup("mba_ai_assistant.group_mba_ai_assistant_user");
        });

        // Restaurar la conversación guardada en este navegador (si existe) para que
        // un refresh de página no borre el hilo de la charla con el usuario.
        const guardadas = this._cargarMensajesGuardados();
        if (guardadas && guardadas.length) {
            this.state.messages = guardadas;
        }
    }

    _storageKey() {
        // Clave por usuario, para que en un navegador compartido cada quien vea su propia
        // conversación (y no la de otro usuario que haya usado el mismo navegador).
        const uid = (this.user && this.user.userId) ? this.user.userId : "anon";
        return `${STORAGE_PREFIX}${uid}`;
    }

    _cargarMensajesGuardados() {
        try {
            const raw = window.localStorage.getItem(this._storageKey());
            if (!raw) return null;
            const parsed = JSON.parse(raw);
            if (Array.isArray(parsed) && parsed.length) {
                return parsed;
            }
        } catch (e) {
            console.warn("No se pudo restaurar la conversación guardada del Asistente de IA", e);
        }
        return null;
    }

    _guardarMensajes() {
        try {
            const aGuardar = this.state.messages.slice(-MAX_STORED_MESSAGES);
            window.localStorage.setItem(this._storageKey(), JSON.stringify(aGuardar));
        } catch (e) {
            // Modo privado del navegador, cuota excedida, etc. No es crítico: simplemente
            // no persiste entre refrescos, pero el chat sigue funcionando con normalidad.
            console.warn("No se pudo guardar la conversación del Asistente de IA", e);
        }
    }

    toggleDropdown() {
        if (this.state.isOpen) {
            this.close();
        } else {
            this.state.isOpen = true;
            this.state.minimized = false;
        }
    }

    minimize() {
        this.state.minimized = true;
    }

    restore() {
        this.state.minimized = false;
    }

    close() {
        this.state.isOpen = false;
        this.state.minimized = false;
    }

    nuevaConversacion() {
        this.state.messages = [DEFAULT_GREETING];
        this._guardarMensajes();
    }

    async sendFeedback(msg, valor) {
        if (!msg.logId || msg.feedback === valor) return;
        try {
            await this.orm.call(
                "mba.ai.interaction.log",
                "registrar_feedback",
                [msg.logId, valor]
            );
            msg.feedback = valor;
            this._guardarMensajes();
        } catch (e) {
            console.warn("No se pudo registrar el feedback", e);
        }
    }

    onKeyDown(ev) {
        if (ev.key === "Enter") {
            ev.preventDefault();
            this.sendMessage();
        }
    }

    async sendMessage() {
        const text = this.state.inputPrompt.trim();
        if (!text || this.state.loading) return;

        // Agregar el mensaje del usuario al historial visual
        this.state.messages.push({ role: "user", content: text });
        this.state.inputPrompt = "";
        this.state.loading = true;
        this._guardarMensajes();

        // Intentar capturar el modelo e ID del registro actual de la pantalla
        let activeModel = null;
        let activeId = null;

        try {
            const mainEl = document.querySelector('.o_content');
            if (mainEl) {
                const formViewEl = mainEl.querySelector('.o_form_view');
                if (formViewEl) {
                    const currentController = this.action.currentController;
                    if (currentController && currentController.props) {
                        activeModel = currentController.props.resModel;
                        activeId = currentController.props.resId;
                    }
                }
            }
        } catch (e) {
            console.warn("No se pudo obtener el contexto activo de la vista actual", e);
        }

        try {
            // Pasar el historial de mensajes de la sesión en los kwargs para dotarlo de memoria activa
            const result = await this.orm.call(
                "mba.ai.assistant",
                "ask_llm",
                [text],
                {
                    active_model: activeModel,
                    active_id: activeId,
                    history: this.state.messages.slice(0, -1) // Enviar todo el historial excluyendo el último mensaje recién agregado
                }
            );
            // ask_llm ahora devuelve {respuesta, log_id} en vez de un string plano, para poder
            // asociar el feedback 👍/👎 a la interacción exacta que quedó registrada en el historial.
            const answer = (result && typeof result === "object") ? result.respuesta : result;
            const logId = (result && typeof result === "object") ? result.log_id : null;
            this.state.messages.push({ role: "assistant", content: answer, logId: logId, feedback: null });
        } catch (error) {
            this.state.messages.push({ role: "assistant", content: "Hubo un error de comunicación con el servidor ERP." });
        } finally {
            this.state.loading = false;
            this._guardarMensajes();
        }
    }
}

export const systrayItem = {
    Component: AiAssistantSystray,
};

registry.category("systray").add("AiAssistantSystray", systrayItem, { sequence: 10 });
