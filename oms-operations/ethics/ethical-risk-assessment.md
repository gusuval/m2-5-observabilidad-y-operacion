# Ethical Risk Assessment: asistente IA de soporte al cliente del OMS

> **Bonus (+10 pts).** Evaluación previa a la puesta en producción de un asistente con IA que ayuda al cliente con dudas sobre su pedido en el chat.
> Sistema ficticio sobre la arquitectura real del OMS.

---

## Sistema bajo evaluación

**Nombre:** OMS Customer Support Assistant
**Descripción:** chatbot LLM que responde al cliente preguntas sobre:
- Estado de su pedido (en preparación, enviado, entregado)
- Reembolsos y cambios (informa de la política y **abre** la solicitud, pero no la aprueba)
- Información del catálogo
- Escalado a un agente humano cuando detecta frustración, o cuando el cliente lo pide

**Modelo:** Claude (familia Sonnet) vía **Vertex AI con endpoint en región UE** (`europe-west`). Así se cumple REG-GDPR-001: ni los prompts ni las respuestas salen de la UE. Antes de contratar hay que verificar la disponibilidad regional del modelo concreto y que el contrato excluye el uso de los datos para entrenamiento.

**Arquitectura:** nuevo módulo `assistant` en el monolito (hexagonal, ADR-002). El LLM **no tiene acceso directo a la BD**: solo puede llamar a 4 herramientas (*tools*) de solo lectura expuestas por los puertos de los módulos existentes, cada una con autorización por sesión:

| Tool | Puerto que usa | Qué devuelve |
|---|---|---|
| `get_my_orders()` | `orders.OrderQueries` | Lista de pedidos **del usuario autenticado** (id, fecha, estado, total) |
| `get_order_status(order_id)` | `orders.OrderQueries` | Estado y tracking, **solo si el pedido es del usuario de la sesión** |
| `search_catalog(query)` | `catalog.CatalogQueries` | Productos públicos |
| `open_refund_request(order_id, reason)` | `orders.RefundRequests` | Crea una **solicitud** en estado `PENDING_REVIEW`, que revisa un humano |

**Datos a los que accede:** pedidos del usuario autenticado (sin dirección ni teléfono completos: la dirección llega truncada a ciudad y CP), catálogo público y FAQ interna (políticas de envío y devolución). **Nunca** ve datos de pago (solo "tarjeta terminada en 4242" si el cliente pregunta).

**Quién toma la decisión final:** el asistente **informa y abre solicitudes**, nunca decide. Reembolsos, cambios y excepciones a la política los aprueba siempre un agente humano. El cliente puede pedir hablar con una persona en cualquier momento (botón permanente "Hablar con una persona").

**Clasificación EU AI Act: riesgo limitado (art. 50, obligaciones de transparencia).**
- No está en el anexo III (alto riesgo): no evalúa solvencia, no decide sobre acceso a servicios esenciales, no hace perfilado con efectos jurídicos ni decide sobre derechos del cliente. La decisión sobre reembolsos es humana.
- Interactúa directamente con personas, así que hay **obligación de informar** de que se habla con una IA ("Soy el asistente automático de AcmeOMS").
- **Condición para mantener la clasificación:** si en el futuro el asistente aprueba reembolsos automáticamente o deniega cambios, hay que **reevaluarla**, porque pasaría a tomar decisiones con efecto sobre el consumidor.

---

## Evaluación de riesgos

Escala: Severidad (1 = molestia, 5 = daño legal o económico grave o pérdida de confianza masiva) × Probabilidad (1 = remota, 5 = casi segura sin control). **Riesgo inherente** = S × P antes de los controles.

| # | Riesgo | Sev | Prob | Inh. | Control técnico | Control organizativo | Responsable | Evidencia para auditoría |
|---|---|---|---|---|---|---|---|---|
| 1 | **Sesgo y trato desigual**: peor calidad de respuesta o menos escalado a humano para clientes que escriben en catalán, gallego, euskera o con faltas de ortografía, o por nombres de origen extranjero | 3 | 3 | 9 | Suite de evaluación en CI con 400 conversaciones pareadas (mismo caso, distinta lengua, registro o nombre). Se bloquea el despliegue si la diferencia en `resolution_rate` o en `human_escalation_offered` es > 5 pp entre grupos. El nombre del cliente **no** se incluye en el prompt. | Revisión trimestral de una muestra de 200 conversaciones estratificada por idioma, hecha por Atención al Cliente | Product Owner del asistente | Informe de la suite en cada release (artefacto de CI) + acta de la revisión trimestral |
| 2 | **Alucinación sobre el estado del pedido** (dice "está enviado" cuando no lo está, o inventa un número de seguimiento o un plazo de reembolso) | 4 | 4 | 16 | El estado y el tracking **solo** pueden salir de la tool `get_order_status`. Un validador post-respuesta compara cada estado, fecha o tracking del texto con el JSON de la tool y, si no cuadra, sustituye la respuesta por la plantilla determinista. Las políticas (plazos de devolución) se citan desde la FAQ con referencia, nunca de memoria. | Botón "¿Te ha sido útil?" + revisión semanal de las respuestas marcadas como incorrectas | Tech Lead del módulo `assistant` | Métrica `assistant_grounding_violations_total`, logs de las sustituciones y tests de regresión con los casos reales detectados |
| 3 | **Filtración de datos personales** (responde sobre el pedido de otro cliente, por IDOR vía `order_id` inventado o por mezcla de contexto entre sesiones) | 5 | 3 | 15 | Las tools validan `pedido.user_id == sesion.user_id` **en el puerto de dominio**, no en el prompt: el LLM no puede saltárselo. Contexto aislado por sesión, sin memoria compartida. Minimización: la dirección llega truncada. Logs sin PII (`user_ref` con hash, ver `instrumentation.md`). | DPIA (art. 35 RGPD) firmada por el DPO antes de producción. Procedimiento de brecha de 72 h (REG-GDPR-005) actualizado con este canal | DPO + Tech Lead | Tests de autorización en CI (no regresión, 100 % de las tools), DPIA archivada y `assistant_authz_denied_total` monitorizada |
| 4 | **Prompt injection** (el cliente o un texto de un producto del catálogo intenta que el asistente ignore sus instrucciones, revele el system prompt, prometa reembolsos o ejecute tools no previstas) | 4 | 4 | 16 | El input del usuario y el contenido del catálogo van delimitados como **datos**, no como instrucciones. Las tools tienen permisos mínimos (ninguna escribe salvo `open_refund_request`, que solo crea una solicitud revisable). Clasificador de input malicioso antes del LLM. **Una promesa del asistente no es vinculante**: el texto legal del chat lo dice y el flujo de reembolso siempre pasa por un humano. | Red teaming trimestral (equipo de seguridad) + canal para reportar vulnerabilidades | Equipo de Seguridad | Informe de red teaming, suite adversarial en CI (≥ 150 casos) y `assistant_injection_detected_total` |
| 5 | **Coste descontrolado** (bucles de turnos, conversaciones muy largas, abuso automatizado del chat como proxy gratuito del LLM) | 3 | 3 | 9 | `MAX_TOKENS_PER_SESSION = 50.000` (al superarlo, escalado a humano), máximo de 30 mensajes por cliente y hora, máximo 5 llamadas a tools por turno, chat disponible **solo para usuarios autenticados** y presupuesto diario con corte automático vía feature flag `assistant.enabled` | Revisión mensual del coste por conversación resuelta frente al coste de un agente humano | Engineering Manager OMS | Panel de coste diario, alerta `AssistantDailyBudgetExceeded` e histórico de cortes |
| 6 | **Escalado a humano que no llega** (el asistente retiene al cliente frustrado en lugar de derivarlo; el cliente no sabe cómo salir del bot) | 3 | 3 | 9 | El botón "Hablar con una persona" está siempre visible y no depende del LLM. El detector de frustración (heurística + modelo) fuerza la oferta de escalado. Un tercer "no me has entendido" deriva a humano automáticamente. | SLA de respuesta humana < 2 min en horario de atención (fuera de horario se dice con claridad y se ofrece una devolución de llamada) | Responsable de Atención al Cliente | `assistant_human_escalation_rate`, tiempo hasta el agente humano y encuesta post-chat |

**Riesgo residual tras los controles (estimado):** todos los riesgos quedan en ≤ 6 (S × P residual), excepto el 2 y el 4, que quedan en 8 (S 4 × P 2). Por eso tienen la monitorización más estricta y su propia condición de retirada.

---

## Quality attributes derivados (van a la spec del sistema)

1. **AI-NFR-001 · Grounding verificable:** el 100 % de los estados, fechas y trackings de pedido que aparecen en una respuesta proceden de una tool del mismo turno. `assistant_grounding_violations_total` se corrige antes de enviar al cliente, y la tasa de sustitución tiene que ser < 1 % de las respuestas.
2. **AI-NFR-002 · Autorización en dominio:** antes de exponer un pedido, el puerto verifica `pedido.user_id == sesion.user_id`. Tests de no regresión en CI que cubren el 100 % de las tools. `assistant_authz_denied_total` se alerta si > 0,5 % de las llamadas, porque indica intentos de IDOR.
3. **AI-NFR-003 · Presupuesto por sesión:** `MAX_TOKENS_PER_SESSION = 50.000`, 30 mensajes por cliente y hora, 5 tool calls por turno. Al superarlos, escalado a humano, nunca un error silencioso.
4. **AI-NFR-004 · Aislamiento del prompt:** el system prompt no puede ser sobrescrito por input del usuario ni por contenido del catálogo. La suite adversarial en CI (≥ 150 casos) bloquea la release si la tasa de éxito de la inyección es > 2 %.
5. **AI-NFR-005 · Trazabilidad por turno:** cada respuesta queda registrada con `trace_id`, versión del modelo, versión del prompt, tools llamadas y valoración del cliente, en el `audit_log` (ADR-010) con retención de 3 años. `audit_log_completeness ≥ 99,9 %`.
6. **AI-NFR-006 · Transparencia (AI Act art. 50):** el primer mensaje de cada conversación identifica al asistente como IA. Test E2E en CI.
7. **AI-NFR-007 · Equidad lingüística:** diferencia de `resolution_rate` entre castellano, catalán, gallego y euskera ≤ 5 pp en la suite de evaluación.

---

## Cómo se monitoriza en producción

Se integra en el [plan de instrumentación](../observability/instrumentation.md) como un módulo más:

| Quality attribute | Métrica | SLI | Alerta |
|---|---|---|---|
| Utilidad para el cliente | `assistant_conversations_total{outcome="resolved\|escalated\|abandoned"}` | resueltas / (total − escaladas a petición del cliente) | `AssistantResolutionDrop` (−15 pp frente a la media de 7 d, warning) |
| AI-NFR-001 | `assistant_grounding_violations_total` / `assistant_responses_total` | — | `AssistantGroundingViolations` (> 1 % en 1 h, critical) |
| AI-NFR-002 | `assistant_authz_denied_total` | — | `AssistantAuthzAnomaly` (> 0,5 % de las tool calls en 15 min, va a seguridad) |
| Riesgo 6 | `assistant_human_escalation_rate` | — | > 30 % durante 1 h = problema **sistémico** (modelo, prompt o tool rota), no del cliente → warning |
| AI-NFR-003 | `assistant_tokens_per_session` (histograma), `assistant_cost_eur_total` | — | `AssistantDailyBudgetExceeded` (> 120 % del presupuesto diario → corte automático por flag) |
| Disponibilidad del proveedor | `llm_provider_requests_total{outcome}` | — | Si el LLM no responde, el chat ofrece directamente un agente humano (degradación elegante, OPS-007) |

**Runbook:** `runbooks/13-assistant-degraded` (pendiente; se escribe antes del lanzamiento con el formato estándar). Su primera acción es siempre `flag_off assistant.enabled`: el chat pasa a "agente humano o formulario" sin redeploy. Es la aplicación directa de la regla 7 (mitigación como feature flag).

---

## Revisión y gobierno

- **Antes de producción:** DPIA firmada por el DPO, suite de evaluación y suite adversarial en verde, y este documento aprobado por Ingeniería, Legal y Atención al Cliente.
- **Lanzamiento gradual:** 5 % de los usuarios autenticados durante 2 semanas (flag con rollout porcentual), luego 25 % y luego 100 %, siempre que los indicadores de los riesgos 2, 3 y 4 sigan en verde.
- **Frecuencia de revisión:** trimestral, conjunta entre Ingeniería, Legal y DPO, y Atención al Cliente. También extraordinaria con cada cambio de modelo o de system prompt, que son releases con su propia suite.
- **Decisión de retirada:** si se materializa un riesgo de severidad 5 (filtración de datos de otro cliente) **o** `AssistantGroundingViolations` dispara 3 veces en una semana, el asistente se desactiva con el flag `assistant.enabled` hasta tener la causa raíz y el fix verificado en la suite. Cualquier on-call puede apagarlo sin pedir permiso; encenderlo de nuevo requiere al Product Owner y al DPO.
- **Owner del documento:** Product Owner del asistente, con el DPO como revisor obligatorio.

---

## Cierre

Lo más útil del ejercicio ha sido ver que casi todos los controles serios **no están en el prompt sino en la arquitectura**: la autorización vive en el puerto de dominio, el grounding se verifica después de la respuesta y el apagado es un feature flag. Un system prompt bien escrito reduce la probabilidad de un riesgo; solo un control técnico fuera del modelo reduce su severidad.
