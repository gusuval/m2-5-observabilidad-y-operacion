# Post-mortems del runbook: outbox-backlog-creciente

> *Escenarios simulados para el trabajo del Bloque 5, construidos sobre la arquitectura real del OMS.*
> Evolución del runbook a lo largo de 3 ocurrencias: V1 manual → V2 con comandos → V3 automatizada.
> Formato blameless: se analizan sistemas y procesos, no personas. Los on-call se identifican por rol.

---

## Versión 1: mitigación manual (PM #1)

**Fecha del incidente:** martes 23 de junio de 2026, 10:40 → 15:30 CEST
**Duración del incidente:** 4 h 50 min (primer evento retrasado a las 10:40 → backlog drenado a las 15:30). **Detectado 3 h 15 min tarde.**
**Detectado por:** Atención al cliente, a las 13:55, tras 41 tickets "he pagado y no me ha llegado nada". No existía ninguna alerta sobre el outbox; los SLOs de Orders y Payments estaban en verde todo el rato.
**On-call:** SRE de guardia (semana 26), con un desarrollador del equipo Orders.
**Impacto:**
- ~310 pedidos sin email de confirmación durante entre 40 min y 4 h 30 min (FR-014: < 5 min).
- Los mismos pedidos atascados en `PENDING`: **118 perdieron el corte del transportista de las 15:00** y salieron al día siguiente.
- **212 facturas no emitidas** por una mitigación improvisada (ver abajo). Se reconstruyeron con finanzas en 2 días.
- Error budget de los SLOs: **0 %**. Ese es el problema: el incidente más caro del trimestre no consumió ni un minuto de budget.

### Lo que ocurrió
A las 10:40 el servicio externo de sellado de facturas empezó a responder en ~30 s (su límite de timeout). El relay (ADR-009 tal cual) procesaba cada lote de 100 eventos **en serie y dentro de la transacción del `FOR UPDATE SKIP LOCKED`**. Cada `OrderConfirmed` esperaba 30 s al handler de billing antes de intentar el email, y un lote tardaba ~50 min. Los `PaymentCaptured` de los pedidos nuevos se quedaban detrás. El checkout seguía aceptando pedidos con normalidad.

### Cómo se mitigó (improvisado, sin runbook)
1. 14:05: se forzó un reinicio del relay. Sin efecto: el siguiente lote volvió a chocar con el sellado.
2. 14:20: se escaló el relay a 6 instancias. Empeoró: 6 relays × 100 eventos = el triple de llamadas concurrentes a un servicio que ya no respondía. Nadie revisó la fórmula del pool (330 < 380, por suerte).
3. 14:35: con la presión de 300 clientes sin confirmación, se ejecutó en Cloud SQL Studio `UPDATE outbox.events SET published_at = now() WHERE event_type = 'OrderConfirmed' AND published_at IS NULL` para "desatascar". Los `PaymentCaptured` empezaron a fluir, **pero esos 212 `OrderConfirmed` ya no generaron ni factura ni email**.
4. 15:10: el proveedor de sellado se recuperó y el resto del backlog drenó a las 15:30. Los 212 emails se enviaron con un script manual al día siguiente.

### Aprendizajes y acciones
- **Un SLO de 5xx no ve una cola.** → Alertas `OutboxBacklogGrowing` (edad > 300 s → critical) y `OutboxRelayStalled`. *(Hecho, 25/06)*
- **Marcar eventos como publicados a mano destruye trabajo pendiente.** → Prohibición explícita en el runbook y retirado el permiso `UPDATE` sobre `outbox.events` al rol de lectura que usa Cloud SQL Studio; los cambios solo se hacen con `oms_ops` y los comandos del runbook. *(Hecho, 26/06)*
- Un handler lento bloquea a todos los demás. → Ticket **OMS-1502**: timeout por handler y lease en vez de transacción larga.
- Escalar el relay frente a una dependencia caída multiplica el problema. → Diagnóstico por handler antes de escalar.
- Faltaba un interruptor para el PDF. → Se adopta `billing.pdf-generation-paused` del catálogo de flags.

### Versión 1 del runbook tras este post-mortem

> **Runbook v1 (texto literal):**
> "Si dispara OutboxBacklogGrowing: mirar en Cloud SQL Studio los eventos de `outbox.events` con `published_at` nulo y su `last_error`. Si es el PDF, encender `billing.pdf-generation-paused`. Si el relay va lento, escalarlo. **No marcar nunca eventos como publicados a mano.**"

(Dice qué no hacer, pero no cómo distinguir un handler caído de un poison message ni qué hacer con los eventos agotados. Por eso hay V2.)

---

## Versión 2: diagnóstico y comandos exactos (PM #2)

**Fecha del incidente:** jueves 10 de septiembre de 2026, 02:05 → 04:20 CEST
**Duración del incidente:** 2 h 15 min (primer fallo 02:05 → verificado 04:20). Alerta critical a las 02:16; respuesta a las 02:21.
**Detectado por:** `OutboxBacklogGrowing` (critical) a las 02:16. El warning saltó casi a la vez: sin un solo envío correcto, la edad cruzó 60 s y 300 s en minutos.
**On-call:** SRE de guardia (semana 37); a las 03:50 entró el segundo SRE de la rotación.
**Impacto:** 86 pedidos de madrugada con el email de confirmación entre 25 min y 2 h 10 min tarde. Ningún pedido atascado en `PENDING` (el handler de confirmación funcionaba). Ninguna factura afectada. Budget de los SLOs: 0 %.

### Qué se vio durante esta segunda ocurrencia
A las 02:00 se ejecutó la rotación trimestral de la API key del proveedor de email, que deshabilita la versión anterior del secreto. La revisión del relay tenía el secreto **fijado a la versión `:7`**, así que todos los envíos devolvían 401. El V1 sirvió para no tocar `published_at`, pero:
1. El V1 decía "si va lento, escálalo". El on-call escaló a 4 instancias (02:30) sin efecto: el problema no era throughput.
2. No había una query para agrupar los fallos por handler. El 401 se encontró a las 02:55, buscando a mano en los logs.
3. Con el secreto arreglado (03:20), la edad del más antiguo **no bajaba**: 61 eventos ya habían agotado sus 10 intentos y el relay los ignoraba. El V1 no contemplaba eventos agotados.
4. El segundo SRE los reencoló con un `UPDATE ... SET attempts = 0` a las 04:05, después de comprobar algo que nadie sabía con certeza: ¿se duplicarían emails o facturas? Billing deduplicaba por `order_id`; notifications, **no**. Solo se salvó porque ningún email había llegado a enviarse.

### Qué cambió en el runbook (V1 → V2)
- **Esquema del outbox documentado** en el runbook y **SQL copiable**: pendientes por `event_type`, fallos agrupados por handler, poison messages, leases.
- **Árbol "relay parado / handler caído / poison / throughput"** en el diagnóstico 0, con una acción para cada rama. Escalar pasa a ser la Acción 5, solo para throughput y con la fórmula del pool.
- **Reencolado por handler** (Acción 3b) y **cuarentena en `outbox.dead_letter`** (Acción 1), ambos idempotentes y reversibles.
- **Verificación medible:** edad < 60 s, cola sin crecer, 0 fallos por handler y p99 de `order_confirmation_lag_seconds` < 60 s durante 15 min.
- Tabla de **comunicación a clientes** según el retraso.

### Acciones derivadas
- Deduplicación obligatoria en todos los consumidores: tabla `outbox.processed_events (event_id, handler)`. *(Hecho, 24/09)*
- OMS-1502 desplegado: lease de 60 s (`locked_by`/`locked_until`), timeout por handler, carriles separados (bulkhead) y backoff con `next_attempt_at`. *(Hecho, 01/10)*
- Secretos de Cloud Run siempre a `:latest`; check en el pipeline que rechaza versiones fijadas. *(Hecho)*
- Nueva métrica `outbox_handler_duration_seconds` y alerta `OutboxHandlerFailing`.
- Evaluar mitigación automática del PDF y cuarentena automática.

---

## Versión 3: automatización preventiva (PM #3)

**Fecha:** miércoles 21 de octubre de 2026, 11:05 → 12:08 CEST. **No hubo incidente de cara al cliente ni se paginó a nadie.**

### Qué pasó (o, mejor, qué NO pasó)
El servicio de sellado volvió a degradarse (mismo patrón que PM #1). Con el bulkhead, solo se llenó el carril de `billing.generate-invoice`. A las 11:19 saltó `OutboxHandlerFailing{handler="billing.generate-invoice"}`; Alertmanager la envió por webhook a `oms-ops-flag-automator`, que encendió `billing.pdf-generation-paused` y lo anunció en `#oms-alerts`. Emails y confirmaciones siguieron saliendo con un p99 de 4 s. 143 facturas se quedaron aparcadas (`outbox_deferred_events`), sin disparar la alerta de edad. A las 12:02 la sonda del automatizador vio el sellado sano 3 veces seguidas, apagó el flag y las 143 facturas se generaron en 6 min. Esa misma madrugada, la cuarentena automática había apartado 2 `OrderConfirmed` con un tipo de IVA canario no contemplado; `OutboxDeadLetterNotEmpty` avisó a las 09:00 y se reencolaron tras el fix.

### Qué cambió en el runbook (V2 → V3)
- **La Acción 2 está automatizada:** `OutboxHandlerFailing{handler="billing.generate-invoice"}` → webhook → `flag_on billing.pdf-generation-paused`. El apagado también es automático, con sonda de salud y aviso en Slack.
- **Cuarentena automática:** al décimo intento, el relay mueve el evento a `outbox.dead_letter` (Acción 1 en código) y publica `outbox_dead_letter_events`. Un poison message ya no despierta a nadie: avisa en horario laboral.
- **La alerta critical se mantiene** (edad > 300 s), porque un relay parado o un fallo del email siguen necesitando a una persona. La warning es ahora la señal habitual.
- **La automatización se vigila:** la alerta compartida `FlagAutomatorStale` (en `observability/ops-automation-alerts.yaml`) ya cubría al automatizador. Se añade al self-test un caso de este runbook para que el latido también falle si la regla del outbox está rota.

### El runbook como automatización

- **Servicio:** Cloud Run `oms-ops-flag-automator` (sin ingress público; solo lo invoca Alertmanager con OIDC). Reglas declarativas `alerta + etiquetas → flag`, con apagado por sonda.
- **Disparador:** webhook de Alertmanager (ruta `automation`) para `OutboxHandlerFailing{handler="billing.generate-invoice"}`.
- **Cuarentena:** código del relay (`OUTBOX_MAX_ATTEMPTS=10` → `outbox.dead_letter`).
- **Owner:** equipo SRE (automatizador) y equipo Orders (handlers y revisión de la cuarentena).
- **Salud:** `ops_job_last_success_timestamp_seconds{job="flag-automator"}` (self-test cada 5 min), vigilada por `FlagAutomatorStale`.

### Métrica del runbook a lo largo de la evolución

| | V1 (manual) | V2 (comandos) | V3 (automatizado) |
|---|---|---|---|
| Detección | Clientes, 3 h 15 min tarde | Alerta propia, 11 min | Alerta warning + mitigación automática en 14 min |
| Tiempo hasta mitigar | 4 h 50 min | 2 h 15 min | 14 min (sin humanos; emails nunca afectados) |
| Pedidos con email tardío (> 5 min) | ~310 | 86 | 0 |
| Pedidos que perdieron el envío del día | 118 | 0 | 0 |
| Facturas perdidas / retrasadas | 212 perdidas | 0 | 0 perdidas, 143 retrasadas 1 h |
| Error budget de SLOs consumido | 0 % (invisible) | 0 % | 0 % |
| Toil del on-call | Alto (+ 2 días de finanzas) | Medio (SQL guiado) | Cero (revisión de cuarentena en horario laboral) |

---

## Conclusión

Este runbook enseña lo contrario que el 01: aquí el problema no era reaccionar tarde, sino **no ver nada**. Los tres SLOs estaban en verde mientras 300 clientes pensaban que su pago se había perdido. En V1 la lección fue doble: medir la edad de la cola (lo que vive el cliente) y no destruir nunca un evento para "desatascar". En V2 el runbook distinguió por fin cuatro causas que se parecen desde fuera (relay parado, handler caído, poison message, throughput), y descubrimos que reprocesar solo es seguro si **todos** los consumidores son idempotentes, no solo el que pensábamos. En V3 el diseño hace el trabajo: bulkhead para que un handler lento no frene a los demás, cuarentena para que un evento roto no bloquee la alerta y un flag automático para que el PDF espere sin que nadie se despierte. Lo pendiente es estructural (ADR-009, re-evaluación): si el volumen crece ×10, el polling cada 500 ms se sustituirá por `LISTEN/NOTIFY` o por una cola externa, y este runbook tendrá que cambiar con él.
