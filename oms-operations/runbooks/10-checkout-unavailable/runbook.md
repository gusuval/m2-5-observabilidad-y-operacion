# Runbook: Checkout no disponible (triage end-to-end)

> **Fuera de catálogo.** Es la puerta de entrada cuando "el cliente no puede comprar" y todavía no sabes por qué. Su trabajo es **localizar la capa en < 5 minutos** y mandarte al runbook específico, o mitigar él mismo si la capa es el borde (CDN, LB, Cloud Armor) o un deploy.
> **Versión:** V3 (ver `post-mortems.md`). **Owner:** equipo SRE. **Última revisión:** 2026-09-29 (tras PM #3).

---

## Trigger

**Alerta:** `CheckoutUnavailable` (severity: **critical**, PagerDuty). Dos reglas con el mismo nombre:
- `signal="probe"`: la probe sintética `checkout-e2e` (blackbox desde **europe-west1**, una ejecución por minuto: login → carrito → `POST /api/orders` con tarjeta de test → cancelación) falla en **≥ 2 de las 3 últimas ejecuciones** (`avg_over_time(probe_success{probe="checkout-e2e"}[3m]) < 0.5`).
- `signal="5xx"`: el ratio de 5xx en `POST /api/orders` supera el **5 % durante 3 min** (con al menos 5 peticiones en esos 3 min).

**También te traen aquí** (definidas en [`01-db-pool-saturated/alert.yaml`](../01-db-pool-saturated/alert.yaml), no se redefinen): `OrdersAvailabilityBurnRateFast` (×14,4 en 1 h) y `OrdersAvailabilityBurnRateSlow` (×6 en 6 h) del SLO 1 de Orders.

**Alertas asociadas:** `CheckoutDegraded` (warning, Slack: 5xx > 1 % en 10 min o ≥ 2 fallos de probe en 15 min), `CheckoutProbeMissing` (warning: la probe no reporta, estamos ciegos) y `CheckoutTriageAutomationStale` (warning: el triage automático V3 no pasa su autotest).

**Por qué hay probe y no solo métricas de la app:** si el fallo está **antes** de `oms-orders` (DNS, certificado, Cloud Armor, LB, Cloud Run sin instancias), la app no registra nada y el ratio de 5xx sale vacío. Además, un 403/429 de Cloud Armor es un 4xx y **el SLO 1 lo excluye**. La probe ve lo que ve el cliente (PM #1).

**Cuándo dispara (patrones conocidos):**
1. **Borde**: regla de Cloud Armor que bloquea tráfico legítimo (PM #1), certificado, CDN o URL map mal cambiados.
2. **Deploy**: revisión nueva de `oms-orders` con errores (PM #3) → runbook 07.
3. **Base de datos**: Postgres caído (09), pool saturado (01) o locks en `inventory` (PM #2).
4. **Stripe**: con el breaker cerrado, un Stripe caído da 502 en el checkout → runbook 03.
5. **Login**: si el cliente no puede autenticarse, no llega al checkout.

---

## Contexto

**Qué impacto tiene:** el cliente llega al final de la compra y no puede terminarla. Es **pérdida directa de ingreso** (~2.500 pedidos/día; en hora punta, 2–4 pedidos por minuto) y el peor momento posible para fallar: ya ha decidido comprar. Muchos no vuelven.

**Presupuestos que se consumen:**
- **NFR-AVAIL-001**: checkout 99,9 % mensual ≈ **43 min de indisponibilidad al mes**. Se mide con la probe (`avg_over_time(probe_success{probe="checkout-e2e"}[30d])`) y cuenta **cualquier** capa, incluido el borde.
- **SLO 1 de Orders** (99,9 % no-5xx de `POST /api/orders` válidos, 28 d): budget ≈ 70 peticiones fallidas ≈ 40 min equivalentes. Solo ve lo que llega a la app. Un bloqueo de Cloud Armor (4xx) o la página de mantenimiento (el SPA no envía la petición) **no consumen SLO, pero sí NFR-AVAIL-001**: apúntalo en el ticket.

**Qué NO afecta (normalmente):**
- **Catálogo**: lo sirve `oms-catalog` desde Redis, en otro servicio de Cloud Run. Si el catálogo también cae, el problema es más amplio (borde, Postgres): mira además [`11-catalog-unavailable`](../11-catalog-unavailable/runbook.md).
- **Carritos**: viven en Redis (`oms-redis-prod`, TTL 7 días). No se pierden aunque el checkout esté caído horas.
- **Pedidos ya creados**: siguen su flujo por el outbox (ADR-009). Confirmaciones y facturas pueden retrasarse, no perderse.

**Pre-requisitos para diagnosticar:**
- Grupo `oms-oncall@`: `roles/run.developer`, `roles/logging.viewer`, `roles/monitoring.viewer`, `roles/compute.loadBalancerAdmin` (solo backends), `roles/compute.securityAdmin` **vía PAM** (`gcloud pam grants create --entitlement=oms-oncall-armor --requested-duration=7200s --justification="INC-<n>" --location=global`), `roles/cloudsql.client` y `roles/iap.tunnelResourceAccessor`.
- Dashboards: [`oms-overview`](https://grafana.example.com/d/oms-overview) (fila "Checkout end-to-end": probe, 5xx por capa, pedidos/min frente a la semana pasada) y [`oms-deploys`](https://grafana.example.com/d/oms-deploys).
- **Setup de sesión** ejecutado (ver [`../README.md`](../README.md#setup-de-sesión-del-on-call-copiar-una-vez-al-empezar-el-incidente)).
- **Nombres del borde:** dominio `www.oms.example.com` · URL map `oms-web-urlmap` · backend services `oms-orders` y `oms-catalog` (NEG serverless `oms-orders-neg-euw3`) · backend bucket `oms-spa` con Cloud CDN · política Cloud Armor `oms-edge-policy` · certificado gestionado `oms-web-cert` (forwarding rule `oms-web-https-fr`, proxy `oms-web-https-proxy`).

> **Atajo V3:** cuando dispara `CheckoutUnavailable`, el job `oms-ops-checkout-triage` ejecuta los pasos 0–2 y las ramas A–G en < 60 s y publica una **tarjeta de triage** en `#incident-response` (y como nota en PagerDuty) con la capa sospechosa y el runbook al que ir. **Léela primero**, pero confirma con el comando de la rama antes de actuar: el job sugiere, tú decides.

---

## Diagnóstico: árbol de triage (objetivo < 5 min)

```
0. ¿Es real?            probe + 5xx + pedidos/min ─────────────── no ─→ falso positivo de la probe (rama H)
1. ¿Qué código recibe el cliente en el LB?
   ├─ 403/429 denied_by_security_policy ───────────────────────────→ A. Cloud Armor
   ├─ sin respuesta / error TLS / 404 del URL map ─────────────────→ A. Borde (DNS, certificado, URL map)
   ├─ 502/503 failed_to_connect / backend_timeout ─────────────────→ B. Cloud Run
   └─ 5xx response_sent_by_backend (la app responde con error) ────→ 2. ¿Qué ha cambiado? → B…F
2. ¿Qué ha cambiado en las últimas 2 h? deploy → B/07 · infra → A · flag → revertir flag
```

### 0. ¿Es real y cuánto? (30 s)

```bash
promq 'avg_over_time(probe_success{probe="checkout-e2e"}[5m])'
promq 'sum(rate(http_requests_total{job="oms-orders", endpoint="/api/orders", method="POST", code=~"5.."}[5m])) / sum(rate(http_requests_total{job="oms-orders", endpoint="/api/orders", method="POST", code!~"4.."}[5m]))'
promq 'sum(rate(orders_created_total[15m])) / sum(rate(orders_created_total[15m] offset 7d))'   # pedidos frente a hace una semana
```

**Si la probe falla y los pedidos están < 50 % de la semana pasada:** es real. Anuncia en `#incident-response`: *"Investigando CheckoutUnavailable: los clientes no pueden completar compras"* y publica el mensaje "Investigando" de la Acción 0.
**Si la probe falla pero 5xx ≈ 0 y los pedidos están normales:** sospecha de la probe. Rama H.
**Si la probe está bien pero 5xx > 5 %:** fallo parcial (una revisión, una instancia, un tipo de pedido). Sigue con el paso 1.

### 1. ¿Qué código recibe el cliente en el balanceador? (1 min, el paso que más tiempo ahorra)

```bash
gcloud logging read 'resource.type="http_load_balancer" AND httpRequest.requestMethod="POST"
  AND httpRequest.requestUrl:"/api/orders"' --freshness=10m --limit=1000 \
  --format="value(httpRequest.status,jsonPayload.statusDetails)" | sort | uniq -c | sort -rn | head
```

| Si domina… | Capa | Ve a |
|---|---|---|
| `403` o `429` con `denied_by_security_policy` | Cloud Armor | rama **A** |
| `502`/`503` con `failed_to_connect_to_backend` o `backend_timeout` | Cloud Run no responde | rama **B** |
| `5xx` con `response_sent_by_backend` | la app responde con error | paso **2** y ramas B–F |
| `200`/`201`/`202` y casi nada de error | el LB está bien: el fallo está antes (DNS, TLS) o en la probe | ramas **A** (borde) y **H** |
| sin líneas | no llega tráfico al LB | rama **A** (DNS, certificado) |

### 2. ¿Qué ha cambiado en las últimas 2 h? (1 min)

```bash
gcloud run revisions list --service=oms-orders --region=$REGION --limit=3 \
  --format="table(metadata.name,metadata.creationTimestamp,status.conditions[0].status)"
gcloud run services describe oms-orders --region=$REGION --format="yaml(status.traffic)"
gcloud logging read 'logName="projects/oms-production/logs/cloudaudit.googleapis.com%2Factivity"
  AND protoPayload.methodName:("securityPolicies" OR "urlMaps" OR "backendServices" OR "certificatemanager" OR "sql.instances" OR "secretmanager")' \
  --freshness=2h --format="table(timestamp,protoPayload.methodName,protoPayload.authenticationInfo.principalEmail)"
curl -sf "$UNLEASH_URL/api/admin/events?project=oms" -H "Authorization: $UNLEASH_TOKEN" \
  | jq -r '.events[:10][] | "\(.createdAt) \(.type) \(.featureName) \(.createdBy)"'
```

**Revisión creada < 2 h antes del inicio, o tráfico repartido entre dos revisiones:** rama B → Acción 2 y runbook 07.
**Cambio en `securityPolicies`, `urlMaps` o `certificatemanager`:** rama A.
**Un flag cambiado justo antes:** vuelve a dejarlo como estaba (`flag_off`/`flag_on`) y comprueba la probe en 2 min.
**Nada ha cambiado:** la causa es carga o una dependencia. Sigue por las ramas C–F en orden.

### Ramas por capa (cada una con su comando y su destino)

**A. Borde: Cloud Armor, certificado, CDN, URL map**
```bash
gcloud logging read 'resource.type="http_load_balancer" AND jsonPayload.enforcedSecurityPolicy.outcome="DENY"
  AND httpRequest.requestUrl:"/api/orders"' --freshness=15m --limit=1000 \
  --format="value(jsonPayload.enforcedSecurityPolicy.priority,httpRequest.remoteIp)" > /tmp/armor-deny.txt
cut -f1 /tmp/armor-deny.txt | sort | uniq -c                  # peticiones denegadas por prioridad de regla
sort -u /tmp/armor-deny.txt | cut -f1 | uniq -c               # IPs distintas por prioridad de regla
gcloud compute security-policies rules describe <PRIORIDAD> --security-policy=oms-edge-policy
gcloud compute ssl-certificates describe oms-web-cert --global --format="value(managed.status,expireTime)"
curl -sS -o /dev/null -w "%{http_code} tls=%{time_appconnect}s\n" https://www.oms.example.com/
```
**Muchas IPs distintas (cientos) bloqueadas por una regla reciente** = tráfico legítimo → **Acción 1**. **Pocas IPs o un único ASN** = ataque real (p. ej. *card testing*): la regla está haciendo su trabajo, **no la toques**; avisa a seguridad. **Certificado distinto de `ACTIVE` o caducado** → escala a plataforma (`#oms-sre`): la renovación del certificado gestionado puede tardar, valora la Acción 3. **404 o backend equivocado** → revierte el URL map: `gcloud compute url-maps import oms-web-urlmap --source=<export_previo.yaml> --global` (el export diario está en `gs://oms-production-infra-backups/url-maps/`).

**B. Cloud Run `oms-orders`**
```bash
gcloud run services describe oms-orders --region=$REGION \
  --format="value(status.conditions[0].status,status.latestReadyRevisionName)"
promq 'sum by (revision, code) (rate(http_requests_total{job="oms-orders", endpoint="/api/orders", method="POST"}[5m]))'
promq 'max(run_instance_count{job="oms-orders"}) / max(run_max_instances{job="oms-orders"})'
```
**Los 5xx se concentran en una revisión nueva** → **Acción 2** y [`07-canary-rollout-failed`](../07-canary-rollout-failed/runbook.md). **Instancias en el techo** → [`12-server-cpu-high`](../12-server-cpu-high/runbook.md). **Servicio `False` en todas las revisiones o región caída** (mira <https://status.cloud.google.com>) → Acción 4.

**C. Postgres**
```bash
promq 'max(pg_up)'
gcloud sql instances describe oms-pg-prod --format="value(state,gceZone)"
PGCONNECT_TIMEOUT=5 psql -X -At -c "SELECT now();"
```
**`pg_up` = 0, estado distinto de `RUNNABLE` o el `psql` no conecta** → [`09-postgres-unavailable`](../09-postgres-unavailable/runbook.md). Activa también la Acción 3 si no hay solución en 10 min.

**D. Pool de conexiones**
```bash
promq 'max by (job) (db_pool_in_use / db_pool_size)'
```
**> 0,9 en `oms-orders`** → [`01-db-pool-saturated`](../01-db-pool-saturated/runbook.md).

**E. Inventory (locks en la reserva de stock)**
```bash
promq 'sum(pg_locks_waiting{schema="inventory"})'
promq 'sum by (outcome) (rate(stock_reservations_total[5m]))'
```
**Locks esperando > 5 y `outcome="error"` subiendo** → runbook 01, **paso 3** (sesión bloqueante) y **Acción 1b**. Es el patrón del PM #2.

**F. Stripe**
```bash
promq 'max(circuit_breaker_state{breaker="stripe"})'
promq 'sum by (outcome) (rate(payment_provider_requests_total{provider="stripe"}[5m]))'
```
**`api_error`/`timeout` altos y 5xx en checkout** → [`03-stripe-payment-gateway-degraded`](../03-stripe-payment-gateway-degraded/runbook.md), Acción 1. **Breaker en 2 y sin 5xx**: el checkout **funciona** (pedidos en `PENDING_PAYMENT`, HTTP 202); no es un incidente de checkout.

**G. IAM / login**
```bash
promq 'sum(rate(auth_login_attempts_total{outcome="error"}[5m])) / sum(rate(auth_login_attempts_total[5m]))'
gcloud secrets versions list oms-jwt-signing-key --limit=3 --format="table(name,state,createTime)"
```
**Errores de login > 2 % y una versión del secreto creada hace poco** → fija la versión anterior en los dos servicios (validan el mismo token): `gcloud run services update oms-catalog --region=$REGION --update-secrets=JWT_SIGNING_KEY=oms-jwt-signing-key:<N-1>` y lo mismo en `oms-orders`. Avisa a seguridad: puede haber sido una rotación deliberada.

**H. ¿Falla solo la probe?**
```bash
gcloud logging read 'resource.type="http_load_balancer" AND httpRequest.userAgent="oms-probe/checkout-e2e"' \
  --freshness=10m --format="value(timestamp,httpRequest.requestMethod,httpRequest.requestUrl,httpRequest.status)" | head -20
```
Te dice en qué paso se cae la probe (login, carrito o pedido). **Si los pedidos reales están normales y la probe falla en el pago**, suele ser el modo test de Stripe o la cuenta sintética (bloqueada, sin stock del SKU de prueba). No hay impacto en clientes: silencia la alerta 1 h en Alertmanager con el motivo y abre ticket a SRE.

---

## Acciones (de menor a mayor impacto, idempotentes)

> Las causas de BD, pool y Stripe se mitigan **en su runbook** (01, 03, 09). Aquí están las acciones propias del triage: borde, deploy, contención y región. Si el checkout está caído al 100 % (probe 3/3 y pedidos ≈ 0), activa la **Acción 3 en paralelo** desde el minuto 5: no le quitas nada a nadie y el cliente ve un mensaje amable en vez de un error.

### Acción 0: Declarar y comunicar (impacto: ninguno · siempre)

Abre el incidente en `#incident-response` y publica en la status page (repite cada 30 min aunque no haya novedades):
- **Investigando:** *"Algunos clientes no pueden completar sus compras. Estamos investigándolo. Tu carrito está guardado."*
- **Identificado / Mitigando:** *"Hemos identificado la causa y estamos aplicando una solución. Tu carrito sigue guardado; podrás terminar la compra en cuanto se resuelva."*
- **Resuelto:** *"El proceso de compra funciona con normalidad desde las HH:MM. Si un pedido te dio error, no se ha cobrado. Si ves un cargo, es una preautorización que el banco libera en unos días."*

**A soporte (`#oms-backoffice`):** *"Incidencia en el checkout desde HH:MM. El carrito del cliente está guardado y no ha perdido nada; que lo intente de nuevo en 30 min. Si el pago dio error, no se ha cobrado. Si el pedido aparece como 'pendiente de pago', se confirmará por email. Próxima actualización: HH:MM."*
**A negocio (EM de guardia, que reenvía a producto y dirección):** capa afectada, hora de inicio, % de checkouts fallando, pedidos por debajo de la semana pasada (paso 0), acción en curso y hora de la próxima actualización.

### Acción 1: Desactivar la regla de Cloud Armor que bloquea tráfico legítimo (impacto: bajo · reversible)

```bash
gcloud pam grants create --entitlement=oms-oncall-armor --requested-duration=7200s \
  --justification="INC-<n> regla Cloud Armor bloqueando checkout" --location=global
gcloud compute security-policies rules describe <PRIORIDAD> --security-policy=oms-edge-policy --format=yaml > /tmp/armor-rule-<PRIORIDAD>.yaml
gcloud compute security-policies rules update <PRIORIDAD> --security-policy=oms-edge-policy --preview
```

**Qué hace:** pone la regla en **modo preview**: se sigue evaluando y registrando (`previewSecurityPolicy` en los logs), pero no bloquea. El efecto es inmediato (< 1 min).
**Por qué preview y no borrar:** conserva la regla tal cual para el post-mortem y el rollback es un flag de la propia regla. En el PM #1 se borró y hubo que reconstruirla desde Terraform.
**Idempotente:** sí. Poner en preview una regla que ya lo está no cambia nada.
**Cuándo NO:** si la rama A mostró pocas IPs o un único ASN (es un ataque). Si dudas, pregunta a seguridad y, mientras tanto, activa la Acción 3.

### Acción 2: Devolver el tráfico a la revisión anterior (impacto: bajo-medio · coincide con un deploy)

```bash
gcloud run revisions list --service=oms-orders --region=$REGION --limit=3 --format="value(metadata.name)"
gcloud run services update-traffic oms-orders --region=$REGION --to-revisions=<REVISION_ANTERIOR>=100
```

**Qué hace:** manda el 100 % del tráfico a la revisión que funcionaba. Cloud Run conserva las revisiones y el cambio tarda segundos; no hay build ni redeploy.
**Idempotente:** sí. Repetir el comando deja el mismo reparto.
**Coste:** se pierde la funcionalidad nueva hasta que se arregle. Si la revisión traía una migración de BD, comprueba en `#oms-releases` que es *expand-and-contract* (compatible hacia atrás) antes de revertir.
**Después:** sigue el runbook [`07-canary-rollout-failed`](../07-canary-rollout-failed/runbook.md) para la causa y el bloqueo del pipeline.

### Acción 3: Página de mantenimiento del checkout (impacto: medio · reversible en 15 s)

```bash
flag_on checkout.maintenance-page
flag_get checkout.maintenance-page
```

**Qué hace:** el SPA (que lee los flags públicos vía `oms-catalog`, caché de 15 s) sustituye el botón "Pagar" por *"Estamos teniendo problemas para procesar pedidos. Tu carrito está guardado: vuelve a intentarlo en unos minutos"*. El cliente puede seguir navegando y llenando el carrito.
**Por qué:** un 5xx después de meter la tarjeta destruye la confianza y genera reintentos (y posibles dobles retenciones). Un mensaje amable con el carrito guardado recupera a una parte de esos clientes.
**Idempotente:** sí.
**Coste:** cierra el checkout **también** a quien habría podido comprar. Úsala con fallo ≥ 50 % o cuando la causa está en una dependencia con solución larga (09, región). El SLO 1 deja de contar fallos (no llegan peticiones), pero **el tiempo con la página cuenta para NFR-AVAIL-001**: anótalo.
**Cuándo NO:** con fallos parciales < 20 % que se están mitigando en otra rama. Ahí la página haría más daño que el error.

### Acción 4: Failover del LB a Cloud Run en europe-west1 (impacto: alto · último recurso, requiere aprobación)

Solo si `oms-orders` está caído en `europe-west3` por un problema de la plataforma (incidente regional de Cloud Run) y Postgres está sano.

```bash
gcloud run services update oms-orders --region=europe-west1 --min-instances=2 --max-instances=6
gcloud compute backend-services add-backend oms-orders --global \
  --network-endpoint-group=oms-orders-neg-euw1 --network-endpoint-group-region=europe-west1
```

**Qué hace:** activa el servicio en frío de `europe-west1` (misma imagen, conecta por VPC a `oms-pg-prod` en Frankfurt, +~10 ms) y lo añade como backend. El LB enruta a la región que responde.
**Fórmula de capacidad:** `max-instances=6` × 10 conexiones = 60, que con los 310 actuales da **370 < 380**. No la subas sin recalcular. REG-GDPR-001: europe-west1 es UE, no hay problema de residencia.
**Idempotente:** sí. Si el backend ya está añadido, `add-backend` falla con "already exists" sin cambiar nada.
**Quién aprueba:** Engineering Manager de guardia (PagerDuty nivel 2). Si no responde en 10 min, decide el on-call y lo documenta.

---

## Verificación

```promql
# 1. La probe pasa en las 10 últimas ejecuciones
min_over_time(probe_success{probe="checkout-e2e"}[10m]) == 1

# 2. 5xx en POST /api/orders por debajo del 0,1 % (objetivo del SLO 1)
sum(rate(http_requests_total{job="oms-orders", endpoint="/api/orders", method="POST", code=~"5.."}[10m]))
  / sum(rate(http_requests_total{job="oms-orders", endpoint="/api/orders", method="POST", code!~"4.."}[10m])) < 0.001

# 3. Los pedidos vuelven al ritmo de la semana pasada (≥ 80 %)
sum(rate(orders_created_total[15m])) / sum(rate(orders_created_total[15m] offset 7d)) > 0.8
```

**Ventana de observación:** **10 minutos consecutivos** con las tres en verde, con `checkout.maintenance-page` ya en OFF (con la página activa los pedidos no pueden recuperarse).

**Cómo confirmas:**
1. Fila "Checkout end-to-end" de [`oms-overview`](https://grafana.example.com/d/oms-overview).
2. Repite el paso 1 del diagnóstico: los `403/429/5xx` en `/api/orders` vuelven a su nivel habitual (casi cero).
3. `CheckoutUnavailable` en *resolved* y, si dispararon, las burn rate de Orders bajando.
4. Status page en "Resuelto", mensaje a soporte y en `#incident-response`: *"Checkout recuperado desde HH:MM: probe 10/10, 5xx < 0,1 %, pedidos al XX % de la semana pasada. Capa: <A–H>. Acciones: <lista>. Flags activos: <lista o ninguno>. Indisponibilidad para NFR-AVAIL-001: N min. Cerrando."*

---

## Rollback

| Acción | Rollback | Cuándo |
|---|---|---|
| 0 (comunicación) | Mensaje de "Resuelto" en status page y soporte | Al cerrar. |
| 1 (regla en preview) | `gcloud compute security-policies rules update <PRIORIDAD> --security-policy=oms-edge-policy --no-preview` | Solo con la regla corregida (Terraform + PR), o si al quitarla vuelve el ataque que bloqueaba. |
| 2 (tráfico a la revisión anterior) | `gcloud run services update-traffic oms-orders --region=$REGION --to-revisions=<REVISION_NUEVA>=10,<REVISION_ANTERIOR>=90` (canary otra vez) | Cuando la revisión corregida pase el canary (runbook 07). Nunca directamente al 100 %. |
| 3 (mantenimiento) | `flag_off checkout.maintenance-page` | En cuanto la probe pase 3 veces seguidas con la causa mitigada. Mira los 5 min siguientes: si los 5xx vuelven, enciéndela otra vez. |
| 4 (failover a europe-west1) | `gcloud compute backend-services remove-backend oms-orders --global --network-endpoint-group=oms-orders-neg-euw1 --network-endpoint-group-region=europe-west1` y después `gcloud run services update oms-orders --region=europe-west1 --min-instances=0` | Con `europe-west3` estable 30 min. Primero el backend, luego las instancias. |

---

## Después del incidente

- [ ] Ticket: T_alerta, T_capa localizada (objetivo < 5 min), T_mitigado, T_verificado, capa (A–H) y si la tarjeta de triage V3 acertó.
- [ ] Minutos de indisponibilidad para NFR-AVAIL-001 (43 min/mes) **y** budget del SLO 1 consumido. Si difieren mucho (borde o mantenimiento), explica por qué en el ticket.
- [ ] Todos los flags como estaban (`flag_get checkout.maintenance-page` y los que hayas tocado en otros runbooks).
- [ ] Si fue Cloud Armor: la corrección va por Terraform, con la regla en `preview` 24 h y el chequeo `armor-preview-check` del pipeline (V3).
- [ ] Si fue un deploy: ¿por qué no lo paró el canary? Ticket a 07.
- [ ] Si la causa no estaba en el árbol: añade la rama en este runbook y en `oms-ops-checkout-triage` en el mismo PR del post-mortem.
- [ ] Post-mortem en < 48 h si duró > 15 min o consumió > 20 % del budget.

---

## Enlaces relacionados

- Alerta YAML: [`alert.yaml`](alert.yaml) (las burn rate de Orders están en [`../01-db-pool-saturated/alert.yaml`](../01-db-pool-saturated/alert.yaml))
- Post-mortems históricos: [`post-mortems.md`](post-mortems.md)
- SLO: [`../../slos/slos-orders.md`](../../slos/slos-orders.md) (SLO 1) · Política: [`../../slos/error-budget-policy.md`](../../slos/error-budget-policy.md)
- Árbol completo entre runbooks: [`../README.md`](../README.md#cómo-se-relacionan-árbol-de-decisión-rápido-a-las-3-am)
- Runbooks a los que deriva: [`01-db-pool-saturated`](../01-db-pool-saturated/runbook.md) · [`03-stripe-payment-gateway-degraded`](../03-stripe-payment-gateway-degraded/runbook.md) · [`07-canary-rollout-failed`](../07-canary-rollout-failed/runbook.md) · [`09-postgres-unavailable`](../09-postgres-unavailable/runbook.md) · [`11-catalog-unavailable`](../11-catalog-unavailable/runbook.md) · [`12-server-cpu-high`](../12-server-cpu-high/runbook.md)
- Dashboards: <https://grafana.example.com/d/oms-overview> · <https://grafana.example.com/d/oms-deploys>
