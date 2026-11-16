# Runbook: Pasarela de pago Stripe degradada

> **Elegido del catálogo (V3 + V4: circuit breaker como mitigación).** Es el runbook de "dependencia externa que no controlamos": no podemos arreglar Stripe, pero sí decidir qué ve el cliente mientras Stripe falla.
> **Versión:** V3 (ver `post-mortems.md`). **Owner:** equipo SRE + equipo Payments. **Última revisión:** 2026-10-14 (tras PM #3).

---

## Trigger

**Alerta:** `StripeGatewayDegraded` (severity: **critical**, PagerDuty)
**Condición:** en las llamadas de `oms-orders` a Stripe (`payment_provider_requests_total{provider="stripe"}`), el ratio de `outcome=~"api_error|timeout"` supera el **5 % durante 5 min** (con al menos 10 llamadas y 3 fallos en 10 min), **o** el p95 de `payment_provider_request_duration_seconds` supera **2,5 s durante 5 min** (con al menos 20 llamadas en 10 min). La alerta lleva la etiqueta `signal="errors"` o `signal="latency"` según la rama que haya disparado.

> **`card_error` NO es un fallo.** Es el emisor rechazando la tarjeta (fondos insuficientes, tarjeta caducada, 3DS fallido). Es una respuesta definitiva y correcta, cuenta como "buena" en el SLO y **nunca** dispara esta alerta.

**Alertas asociadas:**
| Alerta | Severity | Qué te dice |
|---|---|---|
| `StripeGatewayErrorsElevated` | warning (Slack `#oms-alerts`) | errores > 2 % o p95 > 1,5 s durante 10 min. Aviso temprano sin despertar a nadie. |
| `StripeCircuitOpen` | info (Slack) | el circuit breaker de Stripe está abierto en ≥ 1 instancia. Los pedidos entran como `PENDING_PAYMENT`. |
| `PaymentRetryQueueStuck` | warning | `payment_retry_queue_depth` no baja en ~45 min con el breaker cerrado: la cola de reintentos no drena. Ve al paso 6. |
| `PaymentsAuthBurnRateFast` / `...Slow` | critical | el SLO de Payments (99,0 %) está quemando budget a ×14,4 / ×6. Suele llegar 2–5 min después de `StripeGatewayDegraded`. |
| `StripeBreakerControllerStale` | warning | la automatización V3 (`oms-ops-stripe-breaker`) lleva > 10 min sin ejecutarse. Estás en modo manual. |

**Cuándo dispara (patrones conocidos):**
1. **Incidente de Stripe** (API con 5xx o latencia alta, a veces solo en una región o en un tipo de operación). Es lo más frecuente: ver PM #1, #2 y #3.
2. **Problema nuestro de red de salida**: DNS, Cloud NAT sin puertos (`oms-nat-euw3`), regla de firewall de egress. Stripe está bien, pero no llegamos.
3. **Clave de API inválida**: alguien ha rotado la clave en el Dashboard de Stripe o ha subido una versión mala a Secret Manager (`oms-stripe-secret-key`). Stripe responde **401** y el SDK lo cuenta como `api_error`.
4. **Rate limit de Stripe (429)**: lo provocamos nosotros. Casi siempre es un drenaje de la cola de reintentos sin control de ritmo, o el job `oms-reconcile` llamando a la API en bucle.

---

## Contexto

**Cómo funciona el pago (ADR-006):** la tarjeta la tokeniza Stripe.js en el navegador. `oms-orders` recibe un `pm_…` y llama a `paymentIntents.create(confirm=true, capture_method=manual)` con `Idempotency-Key: auth-<order_id>`. Timeout por llamada `2500 ms`, sin reintento síncrono (no cabe en los 3 s del SLO). Cada instancia de `oms-orders` tiene su **circuit breaker**: se abre si ≥ 50 % de las últimas 20 llamadas (mínimo 10) son `api_error|timeout`, espera 30 s y deja pasar 3 llamadas de prueba (semiabierto).

**Qué ve el cliente:**
- **Breaker cerrado y Stripe fallando:** "No hemos podido procesar tu pago, inténtalo de nuevo" (HTTP **502** en `POST /api/orders`, el pedido hace rollback y se libera el stock). Es **5xx para el SLO 1 de Orders** y malo para el SLO de Payments. Es el peor estado posible.
- **Breaker abierto (automático o forzado):** no se llama a Stripe. El pedido se guarda como **`PENDING_PAYMENT`**, el stock queda reservado, la API devuelve **HTTP 202** y el cliente ve *"Pedido recibido. Confirmaremos tu pago por email en los próximos minutos"*. El cobro entra en la cola `payments.retry_queue` y lo reintenta el worker `payment-retry` (corre en `oms-outbox-relay`) con la **misma** clave de idempotencia.
- **Coherencia con los SLOs:** un `PENDING_PAYMENT` **cuenta como malo para el SLO de Payments** (no hubo respuesta definitiva en < 3 s) pero **no es 5xx para el SLO 1 de Orders** (el pedido se creó). Abrir el breaker protege la venta, no el budget de Payments: cada hora de breaker abierto en horario diurno son ~100–150 intentos malos, **≈ 16–24 % del budget de Payments** (616 intentos / 28 d).

**Por qué reintentar es seguro (nunca doble cobro):** Stripe guarda el resultado de cada `Idempotency-Key` durante 24 h. Si la primera llamada llegó a autorizar y nosotros vimos un timeout, el reintento con `auth-<order_id>` devuelve **el mismo** PaymentIntent, no crea otro. Por eso la cola caduca a las **20 h** (por debajo de las 24 h de Stripe): pasado ese plazo el worker no reintenta, busca el PaymentIntent por `metadata.order_id` y, si no existe, cancela el pedido y libera el stock.

**Por qué no conmutamos a otro PSP:** **no hay PSP secundario, es una decisión de ADR-006.** El puerto `PaymentGateway` permite escribir un `AdyenPaymentGateway`, pero hoy no existe ni hay contrato. Y aunque existiera, no se podría conmutar en caliente: el `pm_…` del pedido es un token **de Stripe** y otro PSP no puede cobrarlo. El cliente tendría que volver a meter la tarjeta en otro iframe (otro alcance SAQ-A, otra CSP). Para ~2.200 autorizaciones/día, breaker + cola cubre el riesgo a una fracción del coste. Si Stripe se come más del 50 % del budget de Payments en un trimestre, se reabre ADR-006 (ver "Después del incidente").

**Qué NO afecta:**
- **Catálogo, login y "Mis pedidos"**: no llaman a Stripe.
- **Pedidos ya autorizados**: la captura (al confirmar el pedido) también va a Stripe y se reintenta sola vía outbox (ADR-009). Puede retrasar `OrderConfirmed` y el email de confirmación (runbook 04), pero no se pierde.
- **Datos de tarjeta**: nunca pasan por nuestro backend (SAQ-A). Este incidente no tiene ángulo PCI salvo que `PANDetectedInLogs` dispare a la vez (eso va a seguridad).

**Pre-requisitos para diagnosticar:**
- Grupo `oms-oncall@`: `roles/run.developer`, `roles/logging.viewer`, `roles/monitoring.viewer`, `roles/secretmanager.viewer` (ver versiones, **no** su contenido), `roles/compute.networkViewer` (Cloud NAT) y `roles/iap.tunnelResourceAccessor` (bastión).
- El valor de `oms-stripe-secret-key` **no** lo lee el on-call (REG-PCI-001). Para rotarla hace falta el lead de Payments con `roles/secretmanager.secretVersionManager` vía PAM.
- Acceso *View only* al Dashboard de Stripe (cuenta `acme-oms`, modo live) para buscar `req_…` y abrir ticket a Stripe.
- Dashboard [`oms-payments`](https://grafana.example.com/d/oms-payments): paneles "Stripe outcome ratio", "Stripe p95", "Breaker por instancia", "Retry queue" y "SLO Payments".
- **Setup de sesión** ejecutado (ver [`../README.md`](../README.md#setup-de-sesión-del-on-call-copiar-una-vez-al-empezar-el-incidente)). Además, define este helper para cerrar el breaker de forma gradual (porcentaje del flag en Unleash):

```bash
flag_rollout() {  # uso: flag_rollout <flag> <0-100>
  local sid; sid=$(curl -sf "$UNLEASH_URL/api/admin/projects/oms/features/$1/environments/production/strategies" \
                     -H "Authorization: $UNLEASH_TOKEN" | jq -r '.[] | select(.name=="flexibleRollout") | .id')
  curl -sf -X PUT "$UNLEASH_URL/api/admin/projects/oms/features/$1/environments/production/strategies/$sid" \
    -H "Authorization: $UNLEASH_TOKEN" -H "Content-Type: application/json" \
    -d "{\"name\":\"flexibleRollout\",\"parameters\":{\"rollout\":\"$2\",\"stickiness\":\"random\",\"groupId\":\"$1\"}}" \
    && echo "flag $1 rollout = $2 %"
}
```

> **Atajo V3:** el controlador `oms-ops-stripe-breaker` (Cloud Run Job cada minuto) fuerza el breaker solo cuando `StripeGatewayDegraded` lleva 3 min activa y lo cierra en rampa cuando Stripe se recupera. Si te ha paginado, **lo normal es que el breaker ya esté forzado** cuando abras el portátil. Tu trabajo es confirmar el diagnóstico, comunicar y vigilar el drenaje. Comprueba su estado en `#incident-response` (publica cada cambio) y con `flag_get payments.stripe-circuit-force-open`. Si cambias el flag a mano (`flag_rollout`, `flag_on`, `flag_off`), el controlador se aparta durante 1 h: a partir de ahí mandas tú.

---

## Diagnóstico (en orden, ejecuta y mira)

> Los comandos están **copiables**. No los reescribas a las 3 AM.

### 0. ¿Es real y cuánto impacto hay ahora mismo? (30 s)

```bash
promq 'sum by (outcome) (rate(payment_provider_requests_total{provider="stripe"}[5m])) * 300'   # llamadas por outcome en 5 min
promq 'histogram_quantile(0.95, sum by (le, operation) (rate(payment_provider_request_duration_seconds_bucket{provider="stripe"}[5m])))'
promq 'max by (instance) (circuit_breaker_state{breaker="stripe"})'                          # 0 cerrado · 1 semiabierto · 2 abierto
promq 'sum(rate(http_requests_total{job="oms-orders", endpoint="/api/orders", method="POST", code=~"5.."}[5m])) * 300'
flag_get payments.stripe-circuit-force-open
```

**Si `api_error`+`timeout` son > 5 % y hay 5xx en `POST /api/orders`:** el cliente ya está viendo "no hemos podido procesar tu pago". Anuncia en `#incident-response`: *"Investigando StripeGatewayDegraded, checkout con errores de pago"*. Si el breaker no está forzado y los 5xx siguen > 1/min, **ve directo a la Acción 1** y diagnostica después: el diagnóstico de los pasos 1–5 no cambia la primera acción.
**Si casi todo es `card_error`:** no es un fallo de Stripe. Si el volumen de `card_error` se ha multiplicado por > 5 respecto a la semana pasada, es probable un ataque de *card testing*: escala a seguridad y mira el runbook [`10`](../10-checkout-unavailable/runbook.md) (paso A, Cloud Armor).
**Si el breaker está en 2 en todas las instancias y no hay 5xx:** la mitigación ya está puesta (automática o V3). Sigue con el paso 1 para saber cuándo cerrarlo.

### 1. ¿Qué tipo de error devuelve Stripe? (1 min)

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="oms-orders"
  AND jsonPayload.module="payments" AND jsonPayload.event="payment.authorization.failed"' \
  --freshness=15m --limit=500 --format="value(jsonPayload.error_code,jsonPayload.stripe_http_status)" \
  | sort | uniq -c | sort -rn
# Muestra 5 request IDs de Stripe para el ticket de soporte (también son el atributo stripe.request_id del span payments.authorize)
gcloud logging read 'resource.labels.service_name="oms-orders" AND jsonPayload.event="payment.authorization.failed"' \
  --freshness=15m --limit=5 --format="value(timestamp,jsonPayload.stripe_request_id,jsonPayload.duration_ms)"
```

| Si domina… | Significa | Ve a |
|---|---|---|
| `stripe_api_error` con `500/502/503` | Stripe falla del lado servidor | paso 2 (confirmar que es Stripe) |
| `stripe_timeout` o `stripe_connection_error` (sin status) | No llega respuesta en 2,5 s: Stripe lento **o** nuestra red de salida | paso 2 y, si Stripe está verde, paso 3 |
| `stripe_authentication_error` con `401` | Nuestra clave de API no vale | paso 4 |
| `stripe_rate_limit` con `429` | Estamos llamando demasiado | paso 5 |

### 2. ¿Es Stripe o somos nosotros? (2 min)

Compara tres puntos de vista. Mira además <https://status.stripe.com> (componente *API*, región *EU*) y suscribe el incidente.

```bash
# a) Probe externa desde europe-west1 (blackbox, NO pasa por nuestro Cloud NAT)
promq 'avg_over_time(probe_success{probe="stripe-api"}[10m])'
promq 'avg_over_time(probe_duration_seconds{probe="stripe-api"}[10m])'

# b) Desde el bastión: sale por el MISMO Cloud NAT que oms-orders. Usa la clave restringida de solo lectura.
gcloud compute ssh oms-ops-bastion --zone=europe-west3-a --tunnel-through-iap --command='
  KEY=$(gcloud secrets versions access latest --secret=oms-stripe-healthcheck-key)
  for i in 1 2 3 4 5; do
    curl -s -o /dev/null -u "$KEY:" --max-time 10 \
      -w "%{http_code} dns=%{time_namelookup}s tcp=%{time_connect}s tls=%{time_appconnect}s total=%{time_total}s\n" \
      https://api.stripe.com/v1/balance
  done'
```

| Probe externa (a) | Bastión (b) | Conclusión |
|---|---|---|
| falla / lenta | falla / lenta | **Es Stripe.** Acción 1 + comunicación (Acción 5). Abre ticket a Stripe con los `req_…` del paso 1. |
| OK | falla / `000` / dns alto | **Es nuestra salida a Internet.** Paso 3. |
| OK | OK (200, < 1 s) | **Stripe responde; el problema está en la app** (clave, ritmo, código). Pasos 4 y 5. Si nada encaja, mira si coincide con un deploy: [`07-canary-rollout-failed`](../07-canary-rollout-failed/runbook.md). |

### 3. ¿Nuestra red de salida (DNS, Cloud NAT) está bien?

```bash
gcloud compute ssh oms-ops-bastion --zone=europe-west3-a --tunnel-through-iap --command='getent hosts api.stripe.com'
gcloud logging read 'resource.type="nat_gateway" AND jsonPayload.allocation_status="DROPPED"' \
  --freshness=15m --limit=20 --format="value(timestamp,jsonPayload.connection.dest_ip)"
gcloud compute routers get-status oms-nat-router --region=$REGION \
  --format="yaml(result.natStatus[].name,result.natStatus[].userAllocatedNatIps,result.natStatus[].minExtraNatIpsNeeded)"
```

**Si `getent` no devuelve IP:** el DNS de la VPC falla. Escala a plataforma (`#oms-sre`) y aplica la Acción 1 mientras tanto.
**Si hay líneas `DROPPED`:** Cloud NAT se ha quedado sin puertos (pico de instancias de Cloud Run). Acción 3c.
**Si todo está limpio:** la red no es la causa.

### 4. ¿Ha cambiado la clave de API? (solo si viste 401)

```bash
gcloud secrets versions list oms-stripe-secret-key --limit=3 --format="table(name,state,createTime)"
gcloud run services describe oms-orders --region=$REGION --format=json \
  | jq -r '.spec.template.spec.containers[0].env[] | select(.name=="STRIPE_SECRET_KEY") | .valueFrom.secretKeyRef'
gcloud logging read 'resource.labels.service_name="oms-orders" AND jsonPayload.stripe_http_status=401' \
  --freshness=15m --format="value(resource.labels.revision_name)" | sort | uniq -c
```

**Si hay una versión creada en las últimas horas y los 401 empezaron a la vez:** la versión nueva es mala o la rotación se hizo a medias. Acción 3a.
**Si los 401 salen solo de una revisión:** esa revisión apunta a una versión del secreto distinta del resto. Acción 3a con la versión buena.

### 5. ¿Estamos provocando el 429 nosotros?

```bash
promq 'sum by (operation, job) (rate(payment_provider_requests_total{provider="stripe"}[5m]))'
flag_get payments.retry-queue-paused
flag_get orders.batch-jobs-paused
```

**Si `operation="authorize_retry"` (job `oms-outbox-relay`) tiene más ritmo que `authorize` (`oms-orders`):** el drenaje de la cola está compitiendo con el tráfico real. Acción 3b.
**Si domina `operation="reconcile"`:** el job de reconciliación nocturna está en bucle. `flag_on orders.batch-jobs-paused` (es el kill switch de ese job) y avisa en `#oms-backoffice`.

### 6. ¿Cómo está la cola de reintentos?

```bash
promq 'max(payment_retry_queue_depth)'
psql -X -c "
  SELECT status, count(*) AS n, min(created_at) AS mas_antiguo, max(attempts) AS max_intentos
  FROM payments.retry_queue
  WHERE status IN ('pending', 'in_backoff', 'requires_action')
  GROUP BY status ORDER BY n DESC;"
```

**Si `mas_antiguo` tiene > 12 h:** esos pedidos caducan a las 20 h. Prioriza que la cola drene (Acción 4) y avisa a soporte de cuántos clientes pueden recibir "pago no completado".
**Si `requires_action` crece:** son pagos que piden 3DS al reintentarse fuera de sesión. El cliente ha recibido un email con enlace para completar el pago; no hay nada que hacer en infra.
**Si la cola no baja con el breaker cerrado (`PaymentRetryQueueStuck`):** mira si `payments.retry-queue-paused` se quedó en ON y los logs del worker: `gcloud logging read 'resource.labels.service_name="oms-outbox-relay" AND jsonPayload.module="payments" AND severity>=ERROR' --freshness=30m --limit=20`.

---

## Acciones (de menor a mayor impacto, idempotentes)

> Si el cliente está viendo errores, **la Acción 1 va primero aunque no sepas aún la causa**: convierte 502 en 202 y deja tiempo para diagnosticar. Las Acciones 2–3 atacan la causa y la 4 es la vuelta a la normalidad.

### Acción 1: Forzar el circuit breaker (impacto: bajo para el cliente · reversible en 15 s)

```bash
flag_rollout payments.stripe-circuit-force-open 100
flag_on payments.stripe-circuit-force-open
flag_on payments.retry-queue-paused          # que el worker no reintente contra un Stripe caído
flag_get payments.stripe-circuit-force-open
```

**Qué hace:** en ≤ 15 s todas las instancias de `oms-orders` dejan de llamar a Stripe. Los pedidos entran como `PENDING_PAYMENT` (HTTP 202) y el cobro queda en `payments.retry_queue`. El worker se queda quieto hasta que lo reanudes en la Acción 4.
**Por qué primero y por qué forzarlo si ya hay breaker automático:** el automático es **por instancia** y oscila: cada 30 s deja pasar 3 pedidos reales de prueba que pueden fallar con 502, y las instancias nuevas arrancan con el breaker cerrado. Forzarlo da un comportamiento uniforme y predecible.
**Idempotente:** sí. Encender un flag encendido o fijar el mismo porcentaje no cambia nada.
**Coste:** el cliente no sabe si su pago ha ido bien hasta recibir el email. ~10 % de los pagos en cola piden 3DS al reintentar y requieren que el cliente pulse un enlace (convierte ~60 %). Todo el tiempo con el breaker abierto quema budget de Payments.
**Cuándo NO:** si el paso 0 muestra que los errores son solo `card_error`, o si el problema es un 401 que vas a resolver en < 5 min con la Acción 3a (aun así, si hay 5xx, fuérzalo igual: es barato).

### Acción 2: Reintento rápido para fallos transitorios (impacto: bajo · solo si `signal=errors` y la latencia es normal)

```bash
flag_on payments.stripe-fast-retry
```

**Qué hace:** en el camino síncrono baja el timeout a 1.200 ms y permite **1 reintento** con la misma `Idempotency-Key` (peor caso ~2,5 s, dentro de los 3 s del SLO). Recupera los 5xx sueltos de Stripe sin abrir el breaker.
**Por qué es seguro:** la clave `auth-<order_id>` garantiza que el segundo intento devuelve el mismo PaymentIntent si el primero llegó a autorizar.
**Idempotente:** sí.
**Cuándo NO:** si `signal="latency"` o el p95 de Stripe es > 1 s. Un timeout más corto contra un Stripe lento solo genera más timeouts. En ese caso, Acción 1. Tampoco con 429: más reintentos empeoran el rate limit.

### Acción 3: Corregir la causa cuando es nuestra (impacto: medio · rolling update de ~60 s)

**3a. Clave de API (401).** El lead de Payments sube la versión buena o identifica la última válida. El on-call fija esa versión (no `latest`):
```bash
gcloud run services update oms-orders --region=$REGION --update-secrets=STRIPE_SECRET_KEY=oms-stripe-secret-key:<VERSION_BUENA>
gcloud run services update oms-outbox-relay --region=$REGION --update-secrets=STRIPE_SECRET_KEY=oms-stripe-secret-key:<VERSION_BUENA>
```
Idempotente: repetir el `update` con la misma versión no crea revisión nueva. **No** es "reiniciar": es cambiar la referencia del secreto; la revisión anterior sigue sirviendo hasta que la nueva está lista.

**3b. Rate limit (429) por el drenaje.** `flag_on payments.retry-queue-paused` corta el drenaje en ≤ 15 s. Cuando los 429 desaparezcan, reanuda con un ritmo más bajo (Acción 4).

**3c. Cloud NAT sin puertos.**
```bash
gcloud compute routers nats update oms-nat-euw3 --router=oms-nat-router --region=$REGION \
  --enable-dynamic-port-allocation --min-ports-per-vm=128 --max-ports-per-vm=4096
```
Idempotente (mismos valores, misma config). Anota los valores previos (`gcloud compute routers nats describe oms-nat-euw3 --router=oms-nat-router --region=$REGION`) para el rollback.

**Quién aprueba:** 3a y 3c, el on-call lo comunica en `#incident-response`; fuera de ventana (OPS-002) se permite y se anota en el ticket.

### Acción 4: Cerrar el breaker de forma gradual y drenar la cola con control de ritmo (impacto: medio · es la vuelta a la normalidad)

Empieza solo cuando se cumplan **las tres**: status.stripe.com en *Operational* o *Monitoring*, `probe_success{probe="stripe-api"}` = 1 durante 10 min, y el bastión (paso 2b) devuelve 200 en < 1 s.

```bash
flag_rollout payments.stripe-circuit-force-open 50    # la mitad de los pedidos vuelve a Stripe
# espera 5 min y mira el ratio de errores (debe ser < 2 %); luego:
flag_rollout payments.stripe-circuit-force-open 20
# espera 5 min; luego:
flag_off payments.stripe-circuit-force-open
flag_rollout payments.stripe-circuit-force-open 100   # REARMA el flag (sigue OFF). Si lo dejas a 0, el próximo flag_on no hará nada.

# Drenaje: arranca a 20 cobros/min (valor por defecto) y solo con el breaker ya cerrado
gcloud run services describe oms-outbox-relay --region=$REGION --format=json \
  | jq -r '.spec.template.spec.containers[0].env[] | select(.name=="PAYMENT_RETRY_MAX_PER_MIN") | .value'
flag_off payments.retry-queue-paused
```

**Qué hace:** devuelve tráfico real a Stripe por escalones (50 → 20 → 0 % forzado) y, en cada escalón, si el error supera el 5 % se vuelve a 100. Después reanuda el worker, que cobra la cola a 20/min con la misma clave de idempotencia. 200 pedidos en cola tardan ~10 min.
**Por qué gradual:** en el PM #2 apagamos el breaker de golpe con Stripe aún inestable. Volvió a abrirse solo, hubo 5xx otra vez y el drenaje a toda velocidad gastó los intentos de 7 pedidos que acabaron cancelados.
**Idempotente:** sí. Cada comando fija un estado absoluto (porcentaje o ON/OFF), no incrementa.
**Subir el ritmo de drenaje** (solo si `mas_antiguo` > 12 h y Stripe lleva 30 min estable; hay margen de sobra hasta el rate limit de Stripe en modo live, ~100 operaciones/s):
```bash
gcloud run services update oms-outbox-relay --region=$REGION --update-env-vars=PAYMENT_RETRY_MAX_PER_MIN=60
```
No cambies `--max-instances` de `oms-outbox-relay`: el ritmo lo controla la variable, no el número de instancias (y respetas la fórmula del pool, < 380).

### Acción 5: Comunicación con producto y soporte (impacto: ninguno técnico · obligatoria si el breaker lleva > 15 min forzado)

- **Soporte (`#oms-backoffice`):** *"Stripe tiene una incidencia. Los pedidos se aceptan y el cobro se confirma por email en cuanto se recupere. Si un cliente pregunta: su pedido está guardado, no tiene que repetirlo y no se le cobrará dos veces. Si recibe un email pidiendo completar el pago, es legítimo (verificación 3DS de su banco)."*
- **Producto / EM de guardia:** número de pedidos en `PENDING_PAYMENT`, hora estimada de Stripe y budget de Payments consumido (`promq 'slo:payments_auth_bad:ratio_rate1h'`).
- **Status page:** *"Los pagos con tarjeta se están confirmando con retraso por una incidencia de nuestro proveedor. Tu pedido queda registrado y te avisaremos por email."*
- **Quién decide** alargar el breaker más de 4 h o cancelar pedidos en cola manualmente: Engineering Manager (escalado nivel 2 de PagerDuty).

---

## Verificación

> Con el breaker forzado al 100 % **no hay llamadas a Stripe**, así que el ratio de errores sale vacío y `StripeGatewayDegraded` se resuelve sola. **Eso no es recuperación.** Verifica siempre con el flag ya en OFF.

```promql
# 1. Errores de Stripe por debajo del 1 % (card_error excluido)
sum(rate(payment_provider_requests_total{provider="stripe", outcome=~"api_error|timeout"}[10m]))
  / sum(rate(payment_provider_requests_total{provider="stripe"}[10m])) < 0.01

# 2. p95 de Stripe por debajo de 1,5 s
histogram_quantile(0.95, sum by (le) (rate(payment_provider_request_duration_seconds_bucket{provider="stripe"}[10m]))) < 1.5

# 3. Breaker cerrado en todas las instancias y ningún intento nuevo encolado
max(circuit_breaker_state{breaker="stripe"}) == 0
sum(increase(payment_authorizations_total{outcome="queued"}[10m])) == 0

# 4. Cola drenada (o solo quedan pagos en requires_action, que esperan al cliente)
max(payment_retry_queue_depth) == 0
```

**Ventana de observación:** **15 minutos consecutivos** con 1–3 en verde tras el `flag_off`, y la condición 4 cumplida (o la cola bajando a ≥ 15/min).

**Cómo confirmas:**
1. Panel "Stripe outcome ratio" y "Retry queue" en [`oms-payments`](https://grafana.example.com/d/oms-payments).
2. `flag_get` de `payments.stripe-circuit-force-open`, `payments.retry-queue-paused` y `payments.stripe-fast-retry`: los tres en `enabled=false`. El rollout del primero tiene que estar en 100: `curl -sf "$UNLEASH_URL/api/admin/projects/oms/features/payments.stripe-circuit-force-open/environments/production/strategies" -H "Authorization: $UNLEASH_TOKEN" | jq -r '.[].parameters.rollout'`.
3. Ejecuta la query del paso 6: no debe quedar nada en `pending` con > 1 h.
4. Comunica en `#incident-response`: *"Stripe recuperado: errores < 1 % y p95 < 1,5 s durante 15 min, breaker cerrado, cola drenada (N cobrados, M requires_action, K cancelados). Flags restaurados. Budget de Payments consumido: X %. Cerrando."* Actualiza status page a resuelto y avisa a soporte.

---

## Rollback

| Acción | Rollback | Cuándo |
|---|---|---|
| 1 (breaker forzado) | Es la Acción 4 (cierre gradual). Si el cierre empeora: `flag_rollout payments.stripe-circuit-force-open 100` | En cuanto un escalón supere el 5 % de errores. |
| 1 (cola pausada) | `flag_off payments.retry-queue-paused` | Con el breaker ya en OFF (Acción 4). |
| 2 (reintento rápido) | `flag_off payments.stripe-fast-retry` | Si sube `timeout` tras activarlo (Stripe está lento, no intermitente) o al cerrar el incidente. |
| 3a (secreto fijado) | `gcloud run services update oms-orders --region=$REGION --update-secrets=STRIPE_SECRET_KEY=oms-stripe-secret-key:<VERSION_ANTERIOR>` (y en `oms-outbox-relay`) | Si los 401 siguen o aparecen otros. Más rápido: `gcloud run services update-traffic oms-orders --region=$REGION --to-revisions=<REVISION_ANTERIOR>=100`. |
| 3c (Cloud NAT) | `gcloud compute routers nats update oms-nat-euw3 --router=oms-nat-router --region=$REGION` con los valores anotados | Si aparecen errores nuevos de NAT. Al día siguiente, revisar en horario laboral. |
| 4 (ritmo de drenaje) | `gcloud run services update oms-outbox-relay --region=$REGION --update-env-vars=PAYMENT_RETRY_MAX_PER_MIN=20` | Si aparece algún 429, o en cuanto la cola llegue a 0. |

---

## Después del incidente

- [ ] Anota en el ticket: T_alerta, T_breaker forzado, T_flag OFF, T_cola a 0, pedidos `PENDING_PAYMENT` totales y su desenlace (cobrados / `requires_action` / cancelados).
- [ ] **Todos los flags de payments en OFF y el rollout de `payments.stripe-circuit-force-open` en 100** (`flag_get` de cada uno).
- [ ] Si fue Stripe: enlaza el incidente de status.stripe.com y la respuesta del ticket con los `req_…`. Suma el budget consumido al contador trimestral "budget de Payments consumido por Stripe" (`slos/slos-payments.md`). **Si supera el 50 % del budget en el trimestre, abre la re-evaluación de ADR-006** (multi-PSP).
- [ ] Si la causa fue nuestra (401, NAT, 429): post-mortem en < 48 h. Una causa propia no debería repetirse.
- [ ] Revisa con soporte los pedidos cancelados por caducidad de la cola: cada uno es una venta perdida y un cliente al que hay que escribir.
- [ ] Si el controlador V3 no actuó: `StripeBreakerControllerStale` y logs del job `oms-ops-stripe-breaker`.
- [ ] Si algún paso no sirvió o faltó: actualiza este runbook en el mismo PR del post-mortem.

---

## Enlaces relacionados

- Alerta YAML: [`alert.yaml`](alert.yaml)
- Post-mortems históricos: [`post-mortems.md`](post-mortems.md)
- SLO que protege: [`../../slos/slos-payments.md`](../../slos/slos-payments.md) (99,0 % de autorizaciones con respuesta definitiva en < 3 s) y, de rebote, [`../../slos/slos-orders.md`](../../slos/slos-orders.md) (SLO 1)
- Política de error budget: [`../../slos/error-budget-policy.md`](../../slos/error-budget-policy.md)
- Decisión de arquitectura: [`ADR-006 Stripe con tokenización`](../../../anexo-arquitectura/05-adrs/ADR-006-stripe-tokenizacion.md) (sin PSP secundario) y [`ADR-009 Outbox`](../../../anexo-arquitectura/05-adrs/ADR-009-outbox-pattern.md)
- Dashboard: <https://grafana.example.com/d/oms-payments> · Estado de Stripe: <https://status.stripe.com>
- Runbooks relacionados: [`10-checkout-unavailable`](../10-checkout-unavailable/runbook.md) (triage si no sabes aún que es Stripe), [`01-db-pool-saturated`](../01-db-pool-saturated/runbook.md) (si la latencia de Stripe acaba reteniendo conexiones), [`04-outbox-backlog-creciente`](../04-outbox-backlog-creciente/runbook.md) (capturas y emails retrasados), [`07-canary-rollout-failed`](../07-canary-rollout-failed/runbook.md) (si coincide con un deploy)
