# Runbook: CPU alta en los servidores de aplicación (instancias de Cloud Run)

> **Adicional, fuera del catálogo.** En el OMS los "servidores" son las instancias de Cloud Run de `oms-catalog`, `oms-orders` y `oms-outbox-relay`: no hay VMs de aplicación. Este runbook cubre la CPU de esas instancias. La CPU de **Cloud SQL** es otro problema (→ [01](../01-db-pool-saturated/runbook.md) / [02](../02-api-p95-high/runbook.md#6-está-lenta-la-base-de-datos)).
> **Versión:** V3 (ver `post-mortems.md`). **Owner:** equipo SRE. **Última revisión:** 2026-10-21.

---

## Trigger

**Alerta principal:** `CloudRunCPUSaturated`
- severity **warning** (Slack `#oms-alerts`): p99 de `run_container_cpu_utilization` > **85 %** durante **10 min** en un servicio. El autoescalado de Cloud Run apunta al 60 % de CPU: si el p99 está al 85 %, no está consiguiendo seguir el ritmo.
- severity **critical** (PagerDuty): lo mismo **y** el servicio está al **≥ 90 % de `max-instances`**. Ya no puede escalar más: lo siguiente es latencia y 429.

**Alerta asociada:** `CloudRunAtMaxInstances` (severity **critical**): `run_instance_count >= run_max_instances` durante **5 min** en `oms-catalog` u `oms-orders`. Cloud Run deja de crear instancias, las peticiones esperan en cola y, si la cola se llena, devuelve **429**. (`oms-outbox-relay` queda fuera: su techo es 2 y llegar a él con backlog es normal → runbook [04](../04-outbox-backlog-creciente/runbook.md).)

**Cuándo dispara (patrones conocidos):**
1. **Bot o scraping** del catálogo: miles de `GET /api/products` y `/api/search` con combinaciones que no están en caché. De madrugada y con poca conversión. Ver PM #1.
2. **Tráfico legítimo**: campaña, "Días sin IVA", Black Friday (NFR-SCAL-001: ×5). Sube todo a la vez y la conversión es normal.
3. **Trabajo caro dentro del servicio**: generación de PDF de facturas (`billing`, dentro de `oms-orders`), sugerencias de búsqueda (`oms-catalog`), reconciliación. Ver PM #2.
4. **Regresión de una revisión**: un bucle, una serialización cara, un log en exceso. La CPU sube justo después de un deploy → [07](../07-canary-rollout-failed/runbook.md).

> **Límite físico (el que no se puede romper):** la fórmula de capacidad del pool. `max-instances × DB_POOL_SIZE` de todos los servicios tiene que quedar **< 380** (`max_connections = 400` de `oms-pg-prod` menos 20 reservadas). Hoy: `oms-orders` 20×10 + `oms-catalog` 20×5 + `oms-outbox-relay` 2×5 = **310**. **Margen: 70 conexiones.** Subir `max-instances` sin hacer la cuenta convierte un problema de CPU en un problema de BD que tumba el checkout (PM #1).

---

## Contexto

**Qué impacto tiene:**
- Con la CPU al límite, cada petición tarda más: primero sube el p95 y el p99 (runbook 02), después aparecen timeouts y, con el servicio al máximo de instancias, **429 "no available instance"**.
- `oms-catalog` saturado: el catálogo, la búsqueda y el login (IAM vive aquí) van lentos o fallan. Consume el objetivo del 99,95 % del catálogo (NFR-AVAIL-002, ~22 min/mes).
- `oms-orders` saturado: el checkout se ralentiza y puede dar 5xx (SLO 1 de Orders), y "Mis pedidos" sale del SLO 2.

**Qué NO afecta:**
- **Los datos:** la CPU alta no corrompe nada; las transacciones que fallan hacen rollback.
- **El otro servicio:** `oms-catalog` y `oms-orders` escalan por separado (ADR-001, un monolito desplegado en 3 servicios). Que uno esté saturado no quita CPU al otro, **salvo** que la reacción sea subir `max-instances` y agotar las conexiones de Postgres, que sí son compartidas.
- **Cloud SQL y Redis**, salvo que la causa sea un bot que fuerza misses de caché (entonces sube también la CPU de Cloud SQL: paso 4).
- **Eventos y facturas:** si se pausa la generación de PDF, los eventos esperan en el outbox. No se pierde ninguno.

**Pre-requisitos para diagnosticar:**
- Grupo `oms-oncall@` con `roles/run.developer`, `roles/monitoring.viewer` y `roles/logging.viewer` sobre `oms-production`. Para Cloud Armor, `roles/compute.securityAdmin`, que se obtiene por elevación temporal (Privileged Access Manager, 4 h, justificación = número de incidente).
- Dashboards: [`oms-overview`](https://grafana.example.com/d/oms-overview) (paneles "Cloud Run: CPU e instancias" y "Tráfico vs semana anterior") y [`oms-db-overview`](https://grafana.example.com/d/oms-db-overview) (CPU de Cloud SQL).
- Cloud Armor: política `oms-edge-policy`, asociada al backend del HTTPS Load Balancer (serverless NEG de `oms-catalog` y `oms-orders`).
- **Setup de sesión** ejecutado (ver [`../README.md`](../README.md#setup-de-sesión-del-on-call-copiar-una-vez-al-empezar-el-incidente)) y `export JOB=<servicio de la alerta>`.

---

## Diagnóstico (en orden, ejecuta y mira)

### 0. ¿Qué servicio, cuánta CPU y cuánto margen de instancias? (30 s)

```bash
promq 'histogram_quantile(0.99, sum by (le, job) (rate(run_container_cpu_utilization_bucket{job=~"oms-catalog|oms-orders|oms-outbox-relay"}[5m])))'
promq 'sum by (job) (run_instance_count) / on (job) max by (job) (run_max_instances)'
promq 'histogram_quantile(0.95, sum by (le, job) (rate(http_request_duration_seconds_bucket{job=~"oms-catalog|oms-orders"}[5m])))'
promq 'sum by (job, code) (rate(http_requests_total{job=~"oms-catalog|oms-orders", code=~"429|5.."}[5m]))'
```

**Si la CPU está > 85 % pero las instancias < 70 % del máximo y no hay 429:** el autoescalado tiene margen y debería alcanzar. Sin impacto todavía: sigue el diagnóstico sin prisa.
**Si las instancias están al máximo, o aparecen 429 o 5xx:** hay impacto. Anuncia en `#incident-response`: *"CPU saturada en $JOB, al máximo de instancias, investigando si es tráfico legítimo o bot."*

### 1. ¿Es tráfico legítimo o un bot?

```bash
# Tráfico frente a la misma hora de la semana pasada
promq 'sum by (job) (rate(http_requests_total{job=~"oms-catalog|oms-orders"}[5m])) / sum by (job) (rate(http_requests_total{job=~"oms-catalog|oms-orders"}[5m] offset 1w))'
# "Conversión": añadidos al carrito por cada 100 vistas de producto (hoy frente a hace una semana)
promq '100 * sum(rate(http_requests_total{endpoint=~"/api/cart.*", method="POST"}[10m])) / sum(rate(http_requests_total{endpoint=~"/api/products.*", method="GET"}[10m]))'
promq '100 * sum(rate(http_requests_total{endpoint=~"/api/cart.*", method="POST"}[10m] offset 1w)) / sum(rate(http_requests_total{endpoint=~"/api/products.*", method="GET"}[10m] offset 1w))'
# Quién genera el tráfico (logs del Load Balancer, últimos 10 min)
gcloud logging read 'resource.type="http_load_balancer" AND httpRequest.requestUrl:"/api/"' \
  --freshness=10m --limit=5000 --format=json \
  | jq -r '.[].httpRequest | "\(.remoteIp)\t\(.userAgent // "-" | .[0:60])"' | sort | uniq -c | sort -rn | head -15
```

**Bot o scraping** (→ **Acción 2**): el tráfico es varias veces el de la semana pasada pero la conversión cae a una fracción de la normal; unas pocas IPs o rangos (a menudo de proveedores cloud) concentran > 20 % de las peticiones; user agents raros, vacíos o "headless"; patrón de paginación secuencial o búsquedas que barren el abecedario.
**Tráfico legítimo** (→ **Acciones 1 y 3**): el tráfico sube, pero la conversión se mantiene parecida y está repartido entre miles de IPs. Comprueba en `#oms-releases` si hay campaña anunciada.
> Las IPs son dato personal (GDPR): cópialas solo en el ticket del incidente (acceso restringido), no en Slack.

### 2. ¿Es una revisión nueva?

```bash
gcloud run revisions list --service=$JOB --region=$REGION --limit=3 --format="table(metadata.name, metadata.creationTimestamp)"
promq "histogram_quantile(0.99, sum by (le, revision) (rate(run_container_cpu_utilization_bucket{job=\"$JOB\"}[5m])))"
promq "sum by (revision) (rate(http_requests_total{job=\"$JOB\"}[5m]))"
```

**Si la CPU subió justo tras un deploy y la revisión nueva consume más CPU por petición** (CPU similar con mucho menos tráfico que la anterior): regresión. Ve a [07-canary-rollout-failed](../07-canary-rollout-failed/runbook.md), Acción 2.
**Si no hay deploy reciente o todas las revisiones se comportan igual:** sigue.

### 3. ¿Qué trabajo se come la CPU dentro del servicio?

```bash
# Segundos de servicio por segundo, por endpoint: la mejor aproximación a "quién usa la CPU"
promq "topk(8, sum by (endpoint) (rate(http_request_duration_seconds_sum{job=\"$JOB\"}[5m])))"
# Trabajo en segundo plano dentro de oms-orders: facturas PDF generadas por minuto (hoy frente a hace una semana)
promq '60 * sum(rate(invoices_generated_total[5m]))'
promq '60 * sum(rate(invoices_generated_total[5m] offset 1w))'
flag_get catalog.search-suggestions-disabled; flag_get billing.pdf-generation-paused; flag_get orders.batch-jobs-paused
```

**Si en `oms-catalog` domina `/api/search` (sugerencias incluidas):** **Acción 1a**.
**Si en `oms-orders` las facturas por minuto multiplican las de la semana pasada** (cada PDF cuesta ~300 ms de CPU y se genera en el mismo proceso que atiende el checkout): **Acción 1b**.
**Si hay una exportación o reconciliación en marcha** (`/api/orders/export` arriba en la lista o `oms-reconcile` activo): **Acción 1c**.
**Si domina el endpoint de siempre, pero con mucho más volumen:** es capacidad pura. **Acción 3**.

### 4. ¿Es la CPU de Cloud Run o la de Cloud SQL?

```bash
promq 'max(cloudsql_cpu_utilization{database_id="oms-production:oms-pg-prod"})'
promq 'max by (job) (db_pool_in_use / db_pool_size)'
```

**Si Cloud SQL está > 80 % y Cloud Run < 70 %:** este no es tu runbook. El cuello es la BD: [02, paso 6](../02-api-p95-high/runbook.md#6-está-lenta-la-base-de-datos) o [01](../01-db-pool-saturated/runbook.md).
**Si las dos están altas:** típico de un bot que fuerza misses de caché. Atiende primero el origen (Acción 2) y **no subas `max-instances`**: más instancias = más conexiones contra una BD saturada.
**Si solo Cloud Run está alta:** sigue con las acciones.

### 5. ¿Cuánto margen queda en la fórmula de capacidad? (antes de tocar `max-instances`)

```bash
for s in oms-orders oms-catalog oms-outbox-relay; do
  gcloud run services describe $s --region=$REGION --format=json \
  | jq -r --arg s "$s" '[$s, (.spec.template.metadata.annotations["autoscaling.knative.dev/maxScale"] // "?"),
         ((.spec.template.spec.containers[0].env // [])[] | select(.name=="DB_POOL_SIZE") | .value)] | @tsv'
done | awk '{t += $2 * $3; print} END {print "TOTAL", t, "/ 380  → margen", 380 - t, "conexiones"}'
```

El cálculo lee la configuración **real** (si alguien aplicó la Acción 3b del runbook 01, aquí se ve). Apunta el total y el margen en el ticket.

---

## Acciones (de menor a mayor impacto, idempotentes)

### Acción 1: Apagar el trabajo caro con flags (impacto: bajo · ≤ 15 s · sin redeploy)

```bash
flag_on catalog.search-suggestions-disabled     # 1a · oms-catalog: autocompletado y sugerencias
flag_on billing.pdf-generation-paused           # 1b · oms-orders: los PDF de factura se generan después
flag_on orders.batch-jobs-paused                # 1c · export CSV y reconciliación
```

**Qué hace:** quita la CPU que no tiene un cliente esperando. Enciende solo el flag que el paso 3 ha señalado.
**Por qué primero:** es instantáneo, reversible y no toca ni capacidad ni conexiones.
**Idempotente:** sí. **V3:** si `CloudRunCPUSaturated` lleva ≥ 10 min en `oms-orders`, `oms-ops-flag-automator` ya habrá encendido `billing.pdf-generation-paused` (y `catalog.search-suggestions-disabled` en `oms-catalog`). Compruébalo con `flag_get`.
**Coste:** 1a, búsqueda sin sugerencias; 1b, la factura llega por email con retraso (el evento `OrderConfirmed` espera en el outbox; FR-022 no tiene plazo síncrono); 1c, exports GDPR retrasados unas horas (plazo legal 30 días). Avisa en `#oms-backoffice` si enciendes 1b o 1c.
**Cuándo NO:** si el paso 3 no señaló ese trabajo. Encender flags "por si acaso" degrada el producto sin ganar nada.

### Acción 2: Frenar el bot en el borde con Cloud Armor (impacto: bajo-medio · solo si el paso 1 confirmó bot)

```bash
# 0. Reglas que ya existen (V3: 1000 = 300 req/min por IP en catálogo y búsqueda; 1100 = ban de 10 min a > 1.200 req/min en /api/*)
gcloud compute security-policies describe oms-edge-policy --format=json \
  | jq -r '.rules[] | "\(.priority)\t\(.action)\tpreview=\(.preview // false)\t\(.description // "")"'

# 2a. Regla temporal MÁS ESTRICTA por encima de la permanente (prioridad 950 < 1000). Primero en preview (solo registra).
gcloud compute security-policies rules describe 950 --security-policy=oms-edge-policy >/dev/null 2>&1 || \
gcloud compute security-policies rules create 950 --security-policy=oms-edge-policy \
  --description="INC rate limit temporal catálogo por IP" \
  --expression="request.path.startsWith('/api/products') || request.path.startsWith('/api/search')" \
  --action=throttle --rate-limit-threshold-count=120 --rate-limit-threshold-interval-sec=60 \
  --conform-action=allow --exceed-action=deny-429 --enforce-on-key=IP --preview

# 2b. Tras 2 min, mira a quién habría bloqueado (deben ser las IPs del paso 1, no miles de clientes)
gcloud logging read 'resource.type="http_load_balancer" AND jsonPayload.previewSecurityPolicy.priority=950 AND jsonPayload.previewSecurityPolicy.outcome="DENY"' \
  --freshness=5m --limit=2000 --format=json | jq -r '.[].httpRequest.remoteIp' | sort | uniq -c | sort -rn | head

# 2c. Si cuadra, activarla de verdad
gcloud compute security-policies rules update 950 --security-policy=oms-edge-policy --no-preview

# 2d. (Opcional) Bloquear rangos concretos y persistentes del paso 1 (máx. 10 rangos por regla)
gcloud compute security-policies rules describe 900 --security-policy=oms-edge-policy >/dev/null 2>&1 || \
gcloud compute security-policies rules create 900 --security-policy=oms-edge-policy \
  --description="INC bloqueo rangos scraping" --src-ip-ranges="<RANGO1>,<RANGO2>" --action=deny-403
```

**Qué hace:** si el bot ha pasado por debajo de la regla permanente (reparte el tráfico entre muchas IPs, cada una a < 300 req/min), baja el límite a 120 peticiones por minuto y por IP (un humano navegando hace < 60). El exceso recibe 429 **en el Load Balancer**, sin llegar a Cloud Run. El bot deja de consumir CPU y conexiones.
**Por qué en preview primero:** clientes detrás de un NAT corporativo o de una operadora móvil (CGNAT) comparten IP. Dos minutos de preview evitan bloquear a una oficina entera.
**Idempotente:** sí. `describe || create` no duplica la regla; `update --no-preview` repetido no cambia nada.
**Cuándo NO:** si el paso 1 indica tráfico legítimo. Un rate limit durante una campaña bloquea clientes que quieren comprar.
**Comunica** en `#oms-sre` las reglas creadas: el equipo de seguridad las revisa y decide si pasan a Terraform.

### Acción 3: Subir `max-instances` respetando la fórmula (impacto: medio · nueva revisión sin downtime)

```bash
# Ejemplo con el reparto de hoy (310, margen 70). Recalcula SIEMPRE con el paso 5.
gcloud run services update oms-catalog --region=$REGION --max-instances=32   # +12 × 5 = +60 → 370 < 380
# o bien
gcloud run services update oms-orders  --region=$REGION --max-instances=26   # +6 × 10 = +60 → 370 < 380
```

**Qué hace:** da más techo al autoescalado del servicio saturado. Con el margen actual se puede subir **uno de los dos**, no ambos.
**Idempotente:** sí (mismos valores, ninguna revisión nueva).
**Coste:** más instancias facturadas mientras dure el pico; rolling update de ~60 s sin downtime. Fuera de ventana OPS-002 está permitido para mitigar; anótalo en el ticket.
**Cuándo NO:** si la CPU de Cloud SQL es > 80 % (paso 4) o si el tráfico es de un bot (primero la Acción 2). **Nunca** por encima del margen de la fórmula: si no hay margen, Acción 5.

### Acción 4: Más CPU por instancia o menos concurrencia (impacto: medio · nueva revisión)

```bash
gcloud run services describe $JOB --region=$REGION \
  --format="value(spec.template.spec.containers[0].resources.limits, spec.template.spec.containerConcurrency)"   # anota para el rollback
# 4a. Al máximo de instancias y sin margen en la fórmula: más CPU por instancia (no abre más conexiones)
gcloud run services update $JOB --region=$REGION --cpu=2 --memory=2Gi
# 4b. CPU alta pero lejos del máximo de instancias: menos peticiones por instancia → el autoescalado reparte antes
gcloud run services update $JOB --region=$REGION --concurrency=40
```

**Qué hace:** 4a duplica la CPU de cada instancia sin cambiar el número de instancias ni de conexiones: **no toca la fórmula**. 4b hace que cada instancia acepte menos peticiones simultáneas y que Cloud Run cree instancias antes.
**Idempotente:** sí.
**Coste:** 4a duplica el coste por instancia. 4b necesita **más** instancias para el mismo tráfico: no la uses si ya estás al máximo.
**Cuándo NO:** 4b con `CloudRunAtMaxInstances` activa (empeora la cola).

### Acción 5: Sin margen y con tráfico legítimo: redistribuir conexiones (impacto: alto · requiere aprobación)

```bash
promq 'max(db_pool_in_use{job="oms-catalog"})'     # tiene que ser ≤ 2 sostenido (el catálogo vive de Redis)
gcloud run services update oms-catalog --region=$REGION --update-env-vars=DB_POOL_SIZE=3 --max-instances=30
# 30 × 3 = 90 (antes 20 × 5 = 100) → total 300: más instancias de catálogo con MENOS conexiones
```

**Qué hace:** el catálogo sirve > 90 % desde Redis y casi no usa su pool. Reducir su `DB_POOL_SIZE` permite más instancias sin aumentar las conexiones totales, y libera margen para `oms-orders`.
**Coste:** si la caché falla durante el pico (runbook 05), cada instancia de catálogo tiene menos conexiones para los misses. Por eso se combina con `flag_on catalog.serve-stale`.
**Quién aprueba:** Engineering Manager de guardia (PagerDuty nivel 2). Si tampoco basta, el siguiente paso es de capacidad (tier de Cloud SQL o PgBouncer) y se coordina con el runbook 01, Acción 4.

---

## Verificación

**Métricas que tienen que volver a la normalidad:**
```promql
# 1. CPU p99 de cada servicio por debajo del 70 %
max(histogram_quantile(0.99, sum by (le, job) (rate(run_container_cpu_utilization_bucket{job=~"oms-catalog|oms-orders"}[5m])))) < 0.7

# 2. Margen de instancias: por debajo del 80 % del máximo
max(sum by (job) (run_instance_count) / on (job) max by (job) (run_max_instances)) < 0.8

# 3. Sin 429 ni 5xx por encima del 0,1 %
sum(rate(http_requests_total{job=~"oms-catalog|oms-orders", code=~"429|5.."}[5m])) / sum(rate(http_requests_total{job=~"oms-catalog|oms-orders"}[5m])) < 0.001

# 4. Latencia de vuelta en objetivo (runbook 02)
max(histogram_quantile(0.95, sum by (le, job) (rate(http_request_duration_seconds_bucket{job=~"oms-catalog|oms-orders"}[5m])))) < 0.25
```

**Ventana de observación:** **15 minutos** con las 4 condiciones en verde, **con el tráfico todavía alto** (si el pico ya ha pasado, la verificación no demuestra que la acción funcionó: anótalo).

**Cómo confirmas:**
1. Paneles "Cloud Run: CPU e instancias" y "Tráfico vs semana anterior" de [`oms-overview`](https://grafana.example.com/d/oms-overview).
2. Si usaste la Acción 2: `gcloud logging read 'resource.type="http_load_balancer" AND jsonPayload.enforcedSecurityPolicy.priority=950 AND jsonPayload.enforcedSecurityPolicy.outcome="DENY"' --freshness=10m --limit=1000 --format="value(httpRequest.remoteIp)" | sort -u | wc -l` debe dar pocas IPs (las del bot), no cientos.
3. `CloudRunCPUSaturated` y `CloudRunAtMaxInstances` *resolved* en Alertmanager.
4. Comunica en `#incident-response`: *"CPU de $JOB normalizada: p99 XX %, instancias N/M, sin 429. Causa: <bot / campaña / PDF / regresión>. Acciones: [lista]. Flags activos: [lista]. Reglas de Cloud Armor: [prioridades]. Cerrando."*

---

## Rollback

| Acción | Rollback | Cuándo |
|---|---|---|
| 1a / 1b / 1c (flags) | `flag_off catalog.search-suggestions-disabled` · `flag_off billing.pdf-generation-paused` · `flag_off orders.batch-jobs-paused` | Uno a uno, 10 min entre cada uno, cuando la CPU lleve 30 min < 60 %. Al apagar `billing.pdf-generation-paused` se genera de golpe el atraso de facturas: hazlo fuera del pico. |
| 2 (Cloud Armor) | `gcloud compute security-policies rules delete 950 --security-policy=oms-edge-policy --quiet` (y la 900 si se creó). **Nunca** borres las permanentes 1000 y 1100. | **Inmediato** si soporte recibe quejas de clientes con 429. Si no, a las 24 h; seguridad decide si la regla pasa a Terraform como permanente. |
| 3 (max-instances) | `gcloud run services update <svc> --region=$REGION --max-instances=20` | Cuando acabe el pico (el día siguiente). Si la CPU de Cloud SQL sube > 85 % tras el cambio, **inmediato**. |
| 4 (CPU / concurrencia) | `gcloud run services update $JOB --region=$REGION --cpu=<anotado> --memory=<anotado> --concurrency=<anotado>` | Al día siguiente, dentro de la ventana OPS-002. |
| 5 (pool del catálogo) | `gcloud run services update oms-catalog --region=$REGION --update-env-vars=DB_POOL_SIZE=5 --max-instances=20` y `flag_off catalog.serve-stale` | Si `db_pool_pending_acquires{job="oms-catalog"}` > 0 durante 5 min, inmediato. Si no, al acabar el pico. |

> Rollback universal de cualquier cambio de revisión (3, 4 o 5): `gcloud run services update-traffic $JOB --region=$REGION --to-revisions=<REVISION_ANTERIOR>=100`.

---

## Después del incidente

- [ ] Ticket con: servicio, causa (bot / legítimo / trabajo caro / regresión), pico de CPU e instancias, acciones aplicadas y margen de la fórmula antes y después.
- [ ] **Deja `max-instances`, `DB_POOL_SIZE` y los flags como estaban**, o documenta en el ticket por qué se quedan y recalcula la fórmula con el paso 5.
- [ ] Si fue un bot: las reglas de Cloud Armor creadas a mano pasan a Terraform o se borran. Ninguna regla manual sobrevive más de 7 días.
- [ ] Si fue tráfico legítimo y faltó capacidad: entrada al capacity planning de Black Friday (NFR-SCAL-001, ×5). Con la fórmula actual, ×5 no cabe: hace falta PgBouncer o un tier mayor.
- [ ] Si el PDF de facturas fue la causa: priorizar sacarlo de `oms-orders` a un Cloud Run Job propio.
- [ ] Si algún paso no sirvió o faltó: actualiza este runbook en el mismo PR del post-mortem.

---

## Enlaces relacionados

- Alerta YAML: [`alert.yaml`](alert.yaml)
- Post-mortems históricos: [`post-mortems.md`](post-mortems.md)
- SLOs afectados: [`../../slos/slos-orders.md`](../../slos/slos-orders.md) (SLO 1 y SLO 2) · [política de error budget](../../slos/error-budget-policy.md)
- Dashboard: <https://grafana.example.com/d/oms-overview>
- Runbooks relacionados: [`02-api-p95-high`](../02-api-p95-high/runbook.md) (latencia: la consecuencia habitual de este) · [`01-db-pool-saturated`](../01-db-pool-saturated/runbook.md) (fórmula de capacidad y CPU de Cloud SQL) · [`07-canary-rollout-failed`](../07-canary-rollout-failed/runbook.md) (regresión de revisión) · [`11-catalog-unavailable`](../11-catalog-unavailable/runbook.md) (si el catálogo ya no responde) · [`04-outbox-backlog-creciente`](../04-outbox-backlog-creciente/runbook.md) (facturas pausadas)
