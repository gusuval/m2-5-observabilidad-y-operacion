# Runbook: Backlog del outbox creciendo (emails, facturas y confirmaciones que no salen)

> **Degradación silenciosa.** El checkout funciona y los SLOs de Orders y Payments siguen en verde, pero el negocio está roto: no llegan los emails de confirmación (FR-014, < 5 min), no se generan las facturas (FR-022) y los pedidos pagados no pasan de `PENDING` a `CONFIRMED`, así que el almacén no los prepara.
> **Versión:** V3 (ver `post-mortems.md`). **Owner:** equipo SRE (relay) + equipo Orders (handlers de billing y notificaciones). **Última revisión:** 2026-10-23.

---

## Trigger

**Alerta principal:** `OutboxBacklogGrowing` (severity: **critical**, PagerDuty)
**Condición:** el evento pendiente más antiguo (`outbox_oldest_pending_age_seconds`) supera **300 s durante 5 min**. 300 s es el presupuesto entero de FR-014: si un `OrderConfirmed` lleva 5 min sin salir, el email ya llega tarde.

**Alertas asociadas (mismo runbook):**
| Alerta | Severity | Condición | Qué te dice |
|---|---|---|---|
| `OutboxBacklogGrowing` | warning (Slack `#oms-alerts`) | durante 10 min: edad > 60 s, **o** cola creciendo (`deriv(sum(outbox_pending_events)[15m:1m]) > 0`) con > 50 pendientes | El relay no da abasto o un handler falla. Aún no hay incumplimiento de FR-014. |
| `OutboxRelayStalled` | critical | `time() - outbox_relay_last_success_timestamp_seconds > 120` durante 2 min, o la métrica ha desaparecido | El relay no completa ni un ciclo. Nada sale. Ve directo al paso 1. |
| `OutboxHandlerFailing` | warning | > 20 % de invocaciones fallidas de un `handler` en 10 min (mínimo 10 invocaciones) | Una dependencia concreta (email, PDF) está caída. Ve al paso 3. |
| `OutboxDeadLetterNotEmpty` | warning (horario laboral) | `outbox_dead_letter_events > 0` durante 15 min | La automatización V3 ha puesto en cuarentena un poison message. Revisión humana, sin urgencia. |

**Cuándo dispara (patrones conocidos):**
1. **Handler lento o caído** (el más frecuente): el servicio de sellado de la factura PDF responde en 30 s o el proveedor de email devuelve 401/429/5xx. Los eventos se reintentan con backoff y la cola crece (PM #1 y PM #3).
2. **Poison message**: un evento concreto falla siempre (payload que el handler no sabe procesar, p. ej. un tipo de IVA no contemplado). Agota sus 10 intentos y se queda pendiente para siempre: la edad sube, pero el resto fluye (PM #2).
3. **Relay parado**: revisión nueva que no arranca, instancia viva con el bucle colgado, credenciales de BD rotadas. `OutboxRelayStalled` dispara a la vez.
4. **Síntoma de otra cosa**: Postgres caído o el pool saturado. El relay no puede leer ni marcar eventos. Si `PostgresUnavailable` o `DBConnectionPoolSaturated` están activas, empieza por [`09`](../09-postgres-unavailable/runbook.md) o [`01`](../01-db-pool-saturated/runbook.md): este backlog se vaciará solo después.

---

## Contexto

**Qué impacto tiene (en lenguaje de negocio):**
- **Emails de confirmación tardíos** (FR-014): el cliente paga y no recibe nada. Abre tickets de soporte ("¿se ha hecho mi pedido?") y algunos repiten la compra. Es la queja que llega primero.
- **Pedidos atascados en `PENDING`**: el evento `PaymentCaptured` no se procesa, el pedido no pasa a `CONFIRMED` y el backoffice de Madrid y Barcelona no lo prepara. Si dura más allá del corte del transportista (15:00), el pedido **pierde el envío del día**.
- **Facturas retrasadas** (FR-022): no es urgente para el cliente, pero es una obligación legal. Un evento retrasado no es un problema; **un evento perdido sí lo es** (factura nunca emitida).
- **Workflow GDPR** (`UserDataDeletionRequested`, plazo legal de 30 días): un retraso de horas no tiene impacto; de días, sí.

**Por qué es silencioso:** ningún usuario ve un 5xx. El SLO 1 de Orders (no-5xx en `POST /api/orders`) y el de Payments **no consumen budget**. Solo lo ven estas alertas, `order_confirmation_lag_seconds` (p99 < 60 s) y Atención al cliente.

**Qué NO afecta:**
- **Checkout y pagos**: el pedido y su evento se escriben en la misma transacción (ADR-009). Ningún pedido se pierde: el evento está en `outbox.events` esperando.
- **Catálogo** (`oms-catalog`): no usa el outbox.
- **Datos**: un evento pendiente nunca se pierde. **Solo se pierde si alguien lo borra o lo marca como publicado a mano** (eso pasó en PM #1; está prohibido, ver Acciones).

**Cómo funciona el relay (lo necesitas para leer el diagnóstico):**
- `oms-outbox-relay` (Cloud Run, sin ingress, `--no-cpu-throttling`, `min-instances=1`, `max-instances=2`, `DB_POOL_SIZE=5`) hace un *tick* cada 500 ms: reclama hasta 100 eventos con un **lease** (`FOR UPDATE SKIP LOCKED` + `locked_by`/`locked_until = now() + 60 s`), hace commit y los despacha a los handlers en carriles separados (bulkhead: cada handler tiene su concurrencia y su timeout).
- Si todos los handlers del evento terminan bien → `published_at = now()`. Si alguno falla → `attempts + 1`, `last_error = '<handler>: <mensaje>'`, `next_attempt_at = now() + min(2^attempts s, 10 min)`.
- Con `attempts = 10` el relay deja de intentarlo y lo mueve a `outbox.dead_letter` (cuarentena automática, V3).
- Los gauges `outbox_pending_events` y `outbox_oldest_pending_age_seconds` cuentan `published_at IS NULL AND deferred_by IS NULL`: lo aparcado a propósito por un flag se cuenta aparte en `outbox_deferred_events{handler}` y no dispara la alerta. Los publica el propio relay en cada tick; si el relay muere, desaparecen y salta `OutboxRelayStalled`.
- **Entrega at-least-once:** el mismo evento puede llegar dos veces a un handler. Todos los consumidores deduplican por `(event_id, handler)` en `outbox.processed_events`. Por eso **reprocesar es seguro**.

**Esquema (ADR-009 + migraciones V2/V3):**
```sql
outbox.events (
  id uuid PRIMARY KEY, aggregate_type text, aggregate_id text,
  event_type text,              -- OrderCreated, PaymentCaptured, OrderConfirmed, UserDataDeletionRequested…
  payload jsonb, occurred_at timestamptz,
  published_at timestamptz,     -- NULL = pendiente
  attempts int, last_error text,  -- last_error empieza por el nombre del handler: 'billing.generate-invoice: …'
  next_attempt_at timestamptz,  -- backoff exponencial
  locked_by text, locked_until timestamptz,  -- lease del relay (instancia de Cloud Run)
  deferred_by text)             -- handler pausado por flag que dejó el evento aparcado (Acción 2)
outbox.dead_letter (… mismas columnas …, quarantined_at timestamptz, quarantined_by text, reason text)
outbox.processed_events (event_id uuid, handler text, processed_at timestamptz, PRIMARY KEY (event_id, handler))
```
**Handlers** (valor de la etiqueta `handler`): `orders.confirm-on-payment` (`PaymentCaptured` → `CONFIRMED`), `inventory.consolidate-reservation`, `billing.generate-invoice` (PDF + sello del servicio externo de sellado), `notifications.order-confirmation-email` (API del proveedor de email, cuenta con residencia de datos en la UE), `gdpr.deletion-workflow`.

**Pre-requisitos para diagnosticar:**
- Rol IAM `roles/cloudsql.client` + `roles/iap.tunnelResourceAccessor` + `roles/run.developer` + `roles/logging.viewer` sobre `oms-production` (grupo `oms-oncall@`).
- El usuario `oms_ops` tiene `SELECT`, `INSERT`, `UPDATE` y `DELETE` en el esquema `outbox`.
- Dashboard [`oms-outbox`](https://grafana.example.com/d/oms-outbox): paneles "Edad del más antiguo", "Pendientes por event_type", "Fallos por handler".
- **Setup de sesión** ejecutado (ver [`../README.md`](../README.md#setup-de-sesión-del-on-call-copiar-una-vez-al-empezar-el-incidente)): `promq`, `psql`, `flag_on`/`flag_off`/`flag_get`.

---

## Diagnóstico (en orden, ejecuta y mira)

> Los comandos están **copiables**. No los reescribas a las 3 AM.

### 0. ¿Cuánto backlog hay y está creciendo? (30 s)

```bash
promq 'max(outbox_oldest_pending_age_seconds)'
promq 'sum(outbox_pending_events)'
promq 'deriv(sum(outbox_pending_events)[15m:1m]) * 60'                  # eventos/min que crece (+) o se vacía (−)
promq 'sum(rate(outbox_events_published_total[5m])) * 60'               # ritmo de publicación (eventos/min)
promq 'sum by (handler) (rate(outbox_publish_failures_total[5m])) * 60'  # fallos/min por handler
promq 'time() - max(outbox_relay_last_success_timestamp_seconds)'       # segundos desde el último tick OK
```

**Si el último tick OK fue hace > 120 s o no devuelve nada:** el relay está parado. Paso 1.
**Si el relay publica (ritmo > 0) pero un `handler` concentra los fallos:** dependencia caída. Paso 3.
**Si no hay fallos, el ritmo de publicación es alto y aun así crece:** es throughput (pico de pedidos). Paso 5 y **Acción 5**.
**Si la edad es alta pero los pendientes son pocos (< 20) y no crecen:** huele a poison message. Paso 4.
**Si dura > 30 min o hay pedidos en `PENDING` desde hace > 15 min:** anuncia en `#incident-response`: *"Investigando OutboxBacklogGrowing: confirmaciones/emails con retraso de N min. Checkout OK."*

### 1. ¿El relay está vivo y en qué revisión?

```bash
gcloud run services describe oms-outbox-relay --region=$REGION \
  --format="yaml(status.latestReadyRevisionName,status.latestCreatedRevisionName,status.conditions,spec.template.metadata.annotations)"
gcloud run revisions list --service=oms-outbox-relay --region=$REGION --limit=3 \
  --format="table(metadata.name,metadata.creationTimestamp,status.conditions[0].status)"
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="oms-outbox-relay" AND severity>=ERROR' \
  --freshness=30m --limit=30 --format="value(timestamp,resource.labels.revision_name,jsonPayload.event,jsonPayload.error_code)"
```

**Si `latestCreatedRevisionName` ≠ `latestReadyRevisionName`, o la revisión es de las últimas 2 h:** un deploy ha roto el relay. **Acción 4** (volver a la revisión anterior).
**Si los logs muestran `db.connection.failed` o `password authentication failed`:** problema de BD o de secreto. Revisa `09` y el secreto `oms-db-app-password`.
**Si no hay errores pero el último tick OK es antiguo:** el bucle está colgado dentro de una instancia viva. Paso 5 (leases) y después **Acción 6**.

### 2. ¿Qué está pendiente, desde cuándo y cuánto ha reintentado?

```bash
psql -X -c "
  SELECT event_type,
         count(*)                                         AS pendientes,
         date_trunc('second', now() - min(occurred_at))   AS mas_antiguo,
         max(attempts)                                    AS max_attempts,
         count(*) FILTER (WHERE attempts >= 10)           AS agotados,
         count(*) FILTER (WHERE locked_until > now())     AS en_proceso,
         count(*) FILTER (WHERE deferred_by IS NOT NULL)  AS aparcados_por_flag
  FROM outbox.events
  WHERE published_at IS NULL
  GROUP BY event_type
  ORDER BY mas_antiguo DESC;"
```

**Si domina `OrderConfirmed`:** fallan billing o notificaciones (paso 3).
**Si hay `PaymentCaptured` pendientes:** hay pedidos pagados que no pasan a `CONFIRMED`. Es lo más urgente: avisa a `#oms-backoffice`.
**Si `agotados` > 0:** eventos que el relay ya no reintenta. Si son 1–5 → paso 4 (poison). Si son decenas del mismo handler → la dependencia estuvo caída mucho tiempo: arréglala (Acción 3) y **luego** reencola (Acción 3, segundo bloque).

### 3. ¿Qué handler falla y por qué?

```bash
psql -X -c "
  SELECT split_part(last_error, ':', 1)   AS handler,
         left(split_part(last_error, ':', 2), 70) AS error,
         count(*) AS eventos, max(attempts) AS max_attempts
  FROM outbox.events
  WHERE published_at IS NULL AND last_error IS NOT NULL
  GROUP BY 1, 2 ORDER BY eventos DESC LIMIT 10;"
promq 'histogram_quantile(0.95, sum by (le, handler) (rate(outbox_handler_duration_seconds_bucket[5m])))'
promq 'sum by (reason) (rate(invoice_generation_failures_total[15m]))'
gcloud logging read 'resource.labels.service_name="oms-outbox-relay" AND jsonPayload.event="outbox.handler.failed"' \
  --freshness=30m --limit=20 --format="value(timestamp,jsonPayload.handler,jsonPayload.error_code,jsonPayload.duration_ms)"
```

| Si ves… | Significa | Ve a |
|---|---|---|
| `billing.generate-invoice` con `timeout` o p95 > 10 s | El servicio de sellado o la generación de PDF van lentos | **Acción 2** (pausar PDF) |
| `notifications.order-confirmation-email` con `http_401`/`http_403` | API key del proveedor de email inválida (rotación de secreto) | **Acción 3** |
| `notifications…` con `http_429` | Rate limit del proveedor (¿campaña de marketing en la misma cuenta?) | No escales el relay. Espera al backoff y avisa a marketing |
| `notifications…` con `http_5xx` | Proveedor de email caído | Su status page. Los eventos esperan; Acción 7 si dura > 30 min |
| Un solo `aggregate_id` con el mismo error repetido | Poison message | Paso 4 |

### 4. ¿Hay poison messages?

```bash
psql -X -c "
  SELECT id, event_type, aggregate_id, attempts,
         date_trunc('second', now() - occurred_at) AS edad,
         left(last_error, 100) AS last_error
  FROM outbox.events
  WHERE published_at IS NULL AND attempts >= 5
  ORDER BY attempts DESC, occurred_at
  LIMIT 20;"
psql -X -c "SELECT count(*) AS en_cuarentena, max(quarantined_at) AS ultima FROM outbox.dead_letter;"
```

**Si hay 1–5 eventos con `attempts` alto, el mismo error y el resto del outbox fluye:** son poison messages. **Acción 1**. Copia el `id` y el `last_error` al ticket: es la pista para el bug.
**Si son muchos y del mismo handler:** no es poison, es la dependencia (paso 3).

### 5. ¿Leases colgados o contención de locks?

```bash
psql -X -c "
  SELECT locked_by,
         count(*) FILTER (WHERE locked_until >  now()) AS leases_vivos,
         count(*) FILTER (WHERE locked_until <= now()) AS leases_caducados
  FROM outbox.events WHERE published_at IS NULL AND locked_by IS NOT NULL
  GROUP BY locked_by;"
psql -X -c "
  SELECT pid, state, now() - xact_start AS xact_age, wait_event_type, left(query, 80) AS query
  FROM pg_stat_activity
  WHERE datname = 'oms' AND application_name = 'oms-outbox-relay'
  ORDER BY xact_age DESC NULLS LAST;"
promq 'max by (instance) (db_pool_in_use{job="oms-outbox-relay"} / db_pool_size{job="oms-outbox-relay"})'
promq 'max(run_instance_count{job="oms-outbox-relay"})'
```

**Si hay leases caducados de una instancia que ya no existe:** normal tras un crash; se recogen solos en ≤ 60 s.
**Si una instancia (`locked_by`) mantiene leases vivos que se renuevan pero no publica:** su bucle está colgado. **Acción 6**.
**Si el relay tiene transacciones de > 30 s o espera locks (`wait_event_type = Lock`):** alguien bloquea `outbox.events` (una migración, un `VACUUM FULL` manual). Diagnóstico 3 del runbook [`01`](../01-db-pool-saturated/runbook.md#3-hay-alguna-query-bloqueando-a-otras).
**Si el pool del relay está > 90 %:** el relay no tiene conexiones para avanzar. No escales sin revisar `01`.

---

## Acciones (de menor a mayor impacto, idempotentes)

> **Prohibido, aunque el negocio apriete:** `UPDATE outbox.events SET published_at = now()` para "vaciar la cola", `DELETE` o `TRUNCATE` de eventos pendientes. Cada fila es un email, una factura o una confirmación que **nunca** se enviará (PM #1: 212 facturas reconstruidas a mano durante dos días). Si un evento estorba, se mueve a `outbox.dead_letter` (Acción 1), que es reversible.

### Acción 1: Aislar el poison message en `outbox.dead_letter` (impacto: bajo · 1 evento · reversible)

```bash
EVENT_ID=<id del paso 4>; TICKET=<INC-xxxx>
psql -X -v ON_ERROR_STOP=1 -v ev="$EVENT_ID" -v ticket="$TICKET" <<'SQL'
SET lock_timeout = '5s';   -- si el relay tiene el lease/lock, falla en 5 s en vez de esperar: reintenta
WITH moved AS (
  DELETE FROM outbox.events WHERE id = :'ev' AND published_at IS NULL
  RETURNING *
)
INSERT INTO outbox.dead_letter
SELECT moved.*, now(), current_user, 'manual: ' || :'ticket' FROM moved;
SQL
```

**Qué hace:** mueve el evento, en una sola transacción, de la cola a la tabla de cuarentena. No se pierde: queda con su payload y su error para analizarlo y reencolarlo cuando el bug esté arreglado.
**Por qué primero:** afecta a un único pedido y quita el ruido de la alerta para ver si hay algo más detrás.
**Idempotente:** sí. La segunda ejecución no encuentra el `id` en `outbox.events` (`DELETE 0`) y no inserta nada.
**Cuándo NO:** si el "poison" es en realidad una dependencia caída (decenas de eventos con el mismo error). Mover 300 eventos a cuarentena solo esconde el problema.
**Siguiente paso obligatorio:** avisa en `#oms-backoffice` del `aggregate_id` afectado (ese cliente no recibirá email/factura hasta que se reencole) y abre un bug al equipo dueño del handler.

### Acción 2: Pausar la generación de PDF con feature flag (impacto: bajo · reversible en 15 s)

```bash
flag_on billing.pdf-generation-paused
flag_get billing.pdf-generation-paused
```

**Qué hace:** el handler `billing.generate-invoice` deja de llamar al servicio de sellado y devuelve "pospuesto" sin consumir intento. El resto de handlers (email, confirmación, inventario) procesan el evento y lo marcan en `processed_events`; el evento se queda en el outbox con `deferred_by = 'billing.generate-invoice'`, fuera de la alerta y visible en `outbox_deferred_events`. Al apagar el flag, el relay limpia `deferred_by` y los factura en orden.
**Por qué en este orden:** el PDF es el handler más lento y el que menos urge (la factura puede llegar en horas; el email, no). Pausarlo libera carriles y conexiones del relay para lo urgente.
**Idempotente:** sí. Encender un flag ya encendido no cambia nada.
**Coste:** facturas retrasadas hasta que se apague el flag. No hay huecos en la numeración (REG-IVA-002): el número se asigna al generar, no al confirmar.
**Cuándo NO:** si el handler que falla no es billing. Pausar el PDF no arregla el email.

### Acción 3: Arreglar la dependencia del handler y reencolar los eventos agotados (impacto: medio · nueva revisión)

**3a. Credenciales del proveedor de email (patrón PM #2):**
```bash
gcloud secrets versions list oms-email-provider-api-key --limit=3 --format="table(name,state,createTime)"
gcloud run services describe oms-outbox-relay --region=$REGION --format="yaml(spec.template.spec.containers[0].env)" | grep -A3 EMAIL_PROVIDER_API_KEY
gcloud run services update oms-outbox-relay --region=$REGION \
  --update-secrets=EMAIL_PROVIDER_API_KEY=oms-email-provider-api-key:latest
```
**Qué hace:** vuelve a enlazar el secreto a la versión vigente (si la revisión apuntaba a una versión fijada que se deshabilitó en la rotación). Crea una revisión nueva; los leases de la antigua caducan en 60 s y la nueva los recoge.
**Idempotente:** sí. Repetir el mismo `update` no crea una revisión nueva.

**3b. Reencolar los eventos que agotaron intentos por ese handler (solo cuando el paso 3 ya no muestre fallos nuevos):**
```bash
HANDLER=notifications.order-confirmation-email
psql -X -v ON_ERROR_STOP=1 -v h="$HANDLER" <<'SQL'
UPDATE outbox.events
   SET attempts = 0, next_attempt_at = now(), last_error = NULL
 WHERE published_at IS NULL AND attempts >= 10
   AND last_error LIKE :'h' || ':%';
SQL
```
**Por qué es seguro:** los handlers que ya procesaron el evento lo tienen en `outbox.processed_events` y lo ignoran. Solo se ejecuta el que falló. Ningún cliente recibe dos emails.
**Idempotente:** sí. La segunda ejecución no encuentra eventos con `attempts >= 10`.
**Cuándo NO:** si la dependencia sigue fallando. Reencolar solo vuelve a quemar los 10 intentos.

### Acción 4: Volver a la revisión anterior del relay (impacto: medio · solo si el paso 1 señaló un deploy)

```bash
gcloud run revisions list --service=oms-outbox-relay --region=$REGION --limit=3 --format="value(metadata.name)"
gcloud run services update-traffic oms-outbox-relay --region=$REGION --to-revisions=<REVISION_ANTERIOR>=100
```
**Qué hace:** la revisión anterior vuelve a tener el 100 % y sus instancias mínimas; la rota se queda a 0. Los leases de la rota caducan en 60 s.
**Idempotente:** sí. Repetirlo deja el mismo reparto.
**Coste:** el fix o la feature del deploy se pierde hasta que se arregle. Avisa en `#oms-releases` y sigue con [`07-canary-rollout-failed`](../07-canary-rollout-failed/runbook.md).

### Acción 5: Escalar el relay respetando la fórmula del pool (impacto: medio · solo si es throughput)

```bash
# Fórmula: hoy 310 (orders 20×10 + catalog 20×5 + relay 2×5). Relay a 4×5 = 20 → 320 < 380. Nunca más de 6 (340).
gcloud run services update oms-outbox-relay --region=$REGION --min-instances=4 --max-instances=4
promq 'max(run_instance_count{job="oms-outbox-relay"})'
```
**Qué hace:** cuatro relays reclaman lotes en paralelo con `SKIP LOCKED`, sin doble entrega. Como el relay no tiene ingress, el autoscaling por peticiones no actúa: se fija con `min-instances`.
**Por qué tan tarde:** escalar solo ayuda si el cuello es el propio relay (diagnóstico 0: sin fallos, ritmo alto y aun así crece). Si falla una dependencia, más relays = más llamadas a una API que ya falla o limita (lo que pasó en PM #1).
**Idempotente:** sí. **Cuándo NO:** si la CPU de Cloud SQL > 85 % o el pool de algún servicio > 90 % (ver `01`).

### Acción 6: Forzar instancias nuevas del relay (impacto: medio · solo tras los pasos 1 y 5)

```bash
gcloud run services update oms-outbox-relay --region=$REGION --update-env-vars=RELAY_FORCED_RESTART="$(date -u +%Y%m%dT%H%M)"
```
**Qué hace:** crea una revisión idéntica con instancias nuevas. Úsala **solo** cuando el diagnóstico demostró que el bucle está colgado (tick OK antiguo, sin errores, leases que no avanzan) y las Acciones 3–4 no aplican. No es una solución: deja el `RELAY_FORCED_RESTART` en el ticket y abre un bug con los logs de la instancia colgada.
**Idempotente:** con el mismo valor, sí (sin cambio, no hay revisión nueva). Los eventos en vuelo se reprocesan sin duplicar gracias a la deduplicación.

### Acción 7: Comunicación a clientes (impacto: alto en reputación · aprueba el Engineering Manager)

| Retraso de emails o confirmaciones | Qué se hace | Quién |
|---|---|---|
| < 30 min | Nada hacia fuera. Los emails llegan solos al drenar. | On-call |
| 30 min – 2 h | Aviso a Atención al cliente (`#oms-backoffice`) con la macro "pedido recibido, la confirmación llegará con retraso". | On-call |
| > 2 h, o pedidos `PENDING` que pierden el envío del día | Aviso en la web/app ("Mis pedidos") y estimación de envío. | EM + Atención al cliente |

**Nunca** se reenvían emails a mano ni se lanza un envío masivo: el outbox los entregará y el cliente recibiría dos.

---

## Verificación

**Métricas que tienen que volver a la normalidad:**
```promql
max(outbox_oldest_pending_age_seconds) < 60                                   # FR-014 con margen
deriv(sum(outbox_pending_events)[15m:1m]) <= 0                               # la cola no crece
sum by (handler) (rate(outbox_publish_failures_total[10m])) == 0              # ningún handler falla
sum(outbox_deferred_events) == 0                                              # nada aparcado (tras apagar la Acción 2)
histogram_quantile(0.99, sum by (le) (rate(order_confirmation_lag_seconds_bucket[15m]))) < 60
```
**Tiempo estimado de drenado** (útil para la comunicación): `promq 'sum(outbox_pending_events) / clamp_min(-deriv(sum(outbox_pending_events)[10m:30s]), 0.001)'` (segundos).

**Ventana de observación:** **15 minutos consecutivos** con las 5 condiciones, después de que los pendientes vuelvan a la base (< 50).

**Cómo confirmas:**
1. Panel "Edad del más antiguo" en [`oms-outbox`](https://grafana.example.com/d/oms-outbox) por debajo de 60 s.
2. Ningún `PaymentCaptured` pendiente: `psql -X -c "SELECT count(*) FROM outbox.events WHERE event_type = 'PaymentCaptured' AND published_at IS NULL AND occurred_at < now() - interval '1 minute';"` → `0`.
3. `OutboxBacklogGrowing` y `OutboxRelayStalled` en *resolved* en Alertmanager.
4. `#incident-response`: *"Outbox drenado: más antiguo XX s, 0 fallos por handler durante 15 min. Eventos en dead_letter: N (ticket). Flags activos: [lista]. Cerrando."*

---

## Rollback

| Acción | Rollback | Cuándo |
|---|---|---|
| 1 (dead letter) | Reencolar con el bloque de abajo. Idempotente igual que la ida. | Tras desplegar el fix del handler. |
| 2 (pausar PDF) | `flag_off billing.pdf-generation-paused` | Cuando el servicio de sellado responda (p95 del handler < 5 s). Las facturas pendientes se generan solas; vigila 10 min `OutboxHandlerFailing`. |
| 3a (secreto) | `gcloud run services update-traffic oms-outbox-relay --region=$REGION --to-revisions=<REVISION_ANTERIOR>=100` | Si la revisión nueva no arranca. |
| 3b (reencolar) | No aplica: los eventos vuelven a contar intentos. Si vuelven a fallar, se agotan y la cuarentena V3 los recoge. | — |
| 4 (revisión anterior) | `update-traffic --to-latest` cuando el fix esté desplegado (en ventana OPS-002). | — |
| 5 (escalar) | `gcloud run services update oms-outbox-relay --region=$REGION --min-instances=1 --max-instances=2` | Al volver a la base de pendientes, o si el pool de Cloud SQL sube > 85 %. |
| 6 (forzar instancias) | No aplica (revisión idéntica). | — |

**Reencolar un evento de `outbox.dead_letter`** (rollback de la Acción 1 y de la cuarentena automática V3):
```bash
EVENT_ID=<id>
psql -X -v ON_ERROR_STOP=1 -v ev="$EVENT_ID" <<'SQL'
WITH m AS (DELETE FROM outbox.dead_letter WHERE id = :'ev' RETURNING *)
INSERT INTO outbox.events (id, aggregate_type, aggregate_id, event_type, payload, occurred_at, attempts, next_attempt_at)
SELECT id, aggregate_type, aggregate_id, event_type, payload, occurred_at, 0, now() FROM m;
SQL
```

---

## Después del incidente

- [ ] Duración (T_alerta → T_verificado), pico de pendientes, edad máxima y **nº de pedidos afectados** (emails tardíos > 5 min y pedidos `PENDING` > 15 min).
- [ ] `flag_get billing.pdf-generation-paused`: **OFF** antes de cerrar. Un PDF pausado olvidado es una factura que no sale.
- [ ] Relay de vuelta a `min-instances=1`/`max-instances=2` si se escaló (la fórmula del pool es de todos).
- [ ] Cada evento en `outbox.dead_letter` tiene ticket y dueño. La cuarentena no es un cementerio: se revisa en la daily.
- [ ] Si se usó la Acción 3a: revisa por qué el secreto estaba fijado a una versión (debe ser `:latest`).
- [ ] Si hubo comunicación a clientes (Acción 7): post-mortem en < 48 h con Atención al cliente.
- [ ] Si un paso del runbook no sirvió: actualízalo en el mismo PR del post-mortem.

---

## Enlaces relacionados

- Alerta YAML: [`alert.yaml`](alert.yaml) · Post-mortems: [`post-mortems.md`](post-mortems.md)
- Decisión de arquitectura: [`ADR-009 Outbox pattern`](../../../anexo-arquitectura/05-adrs/ADR-009-outbox-pattern.md)
- SLO relacionado (no lo consume, pero comparte flujo): [`../../slos/slos-orders.md`](../../slos/slos-orders.md). FR-014 se vigila con `order_confirmation_lag_seconds` en [`instrumentation.md`](../../observability/instrumentation.md#outbox-shared-oms-outbox-relay)
- Dashboard: <https://grafana.example.com/d/oms-outbox>
- Runbooks relacionados: [`01-db-pool-saturated`](../01-db-pool-saturated/runbook.md) (pool del relay), [`09-postgres-unavailable`](../09-postgres-unavailable/runbook.md) (sin BD no hay relay), [`07-canary-rollout-failed`](../07-canary-rollout-failed/runbook.md) (revisión rota), [`06-cloud-sql-disk-full`](../06-cloud-sql-disk-full/runbook.md) (el outbox publicado sin archivar ocupa disco)
