# Runbook: Latencia alta en endpoints críticos (p95 / p99 de la API)

> **Elegido del catálogo (Media, V1+V2+V3).** Es el runbook de "funciona, pero lento": no hay errores, pero el usuario espera y abandona.
> **Versión:** V3 (ver `post-mortems.md`). **Owner:** equipo SRE (con el equipo de Orders para las queries). **Última revisión:** 2026-09-30.

---

## Trigger

**Alerta principal:** `APILatencyP95High`
- severity **warning** (Slack `#oms-alerts`): p95 > **250 ms** durante **10 min** en un endpoint crítico (NFR-PERF-002).
- severity **critical** (PagerDuty): p95 > **500 ms** durante **10 min** (el doble del objetivo: el usuario ya lo nota en cada clic).

**Endpoints críticos:** `GET /api/orders*` y `GET /api/products*`, `/api/cart*` y `/api/auth/token` (emisión de token). El p95 se calcula con `histogram_quantile(0.95, …)` sobre `http_request_duration_seconds_bucket`, por `job` y `endpoint`, y solo cuenta si el endpoint recibe > 0,2 req/s (por debajo, 3 peticiones lentas mueven el p95 y la alerta sería ruido). `POST /api/orders` **no** entra: incluye la llamada a Stripe y tiene su propio presupuesto (anexo, NFR-PERF-002 original: 400 ms) y sus propias alertas en el runbook 03.

**Alertas asociadas (mismo runbook):**

| Alerta | Severity | Condición |
|---|---|---|
| `APILatencyP99High` | warning | p99 > 800 ms durante 10 min en un endpoint crítico (NFR-PERF-003) |
| `SearchLatencyHigh` | warning | p95 de `/api/search` > 300 ms durante 10 min (NFR-PERF-004). **V3:** dispara `catalog.search-suggestions-disabled` automáticamente |
| `OrdersLatencyBurnRate` | critical / warning | Burn rate del SLO 2 de Orders (95 % de `GET /api/orders*` < 250 ms y 99 % < 800 ms, 28 d): 14,4× en 1 h (critical) o 6× en 6 h (warning) |
| `FlagAutomatorStale` | warning | La automatización V3 (`oms-ops-flag-automator`) lleva > 15 min sin latido. Alerta compartida: `observability/ops-automation-alerts.yaml` |

**Cuándo dispara (patrones conocidos):**
1. **Query lenta nueva**: una release añade una consulta sin índice (seq scan en `orders.orders`). Solo un endpoint, siempre lento. Ver PM #1.
2. **Pico + cold starts**: campaña de marketing, Cloud Run escala de 3 a 15 instancias y cada instancia nueva tarda 3–5 s en arrancar. Sube sobre todo el p99. Ver PM #2.
3. **CPU**: de Cloud Run (búsqueda con sugerencias, PDF de facturas) → runbook 12, o de Cloud SQL (queries pesadas, batch) → este runbook, paso 6.
4. **Dependencia lenta**: pool de BD esperando (→ 01), Redis con hit ratio bajo (→ 08), Stripe lento (→ 03).
5. **Revisión recién desplegada** (canary o 100 %) → runbook 07.

---

## Contexto

**Qué impacto tiene:**
- **"Mis pedidos" y el detalle de pedido** tardan: el cliente recarga la página (duplica la carga) o llama a soporte "¿se ha hecho mi pedido?". Consume el budget del **SLO 2 de Orders** (5 % de peticiones pueden superar 250 ms y 1 % 800 ms en 28 d; con ~40.000 `GET /api/orders*` al día, eso son ~56.000 y ~11.200 peticiones lentas permitidas).
- **Catálogo y carrito lentos**: menos conversión. Cada 100 ms de más en la ficha de producto se nota en el embudo; con > 1 s el usuario abandona.
- **Login (emisión de token) lento**: el usuario no llega ni a ver el carrito.
- La latencia es la antesala de los errores: si la causa es una dependencia saturada, en minutos llegan los timeouts y los 5xx (→ 01, 10, 11).

**Qué NO afecta:**
- **No hay pérdida de datos** ni pedidos a medias: lento no es incorrecto. Las transacciones siguen siendo ACID.
- **No consume el SLO 1 de Orders** (disponibilidad) mientras no haya 5xx.
- **Pagos**: el SLO de Payments mide `< 3 s` en la autorización y tiene su propio runbook (03). Si lo lento es `payments.authorize`, ve allí.
- **Outbox, emails y facturas**: van por el worker `oms-outbox-relay`, que no tiene ingress ni entra en este p95.

**Pre-requisitos para diagnosticar:**
- Grupo `oms-oncall@` con `roles/run.developer`, `roles/monitoring.viewer`, `roles/cloudtrace.user`, `roles/logging.viewer`, `roles/cloudsql.client` y `roles/iap.tunnelResourceAccessor` sobre `oms-production`.
- Dashboards: [`oms-overview`](https://grafana.example.com/d/oms-overview) (panel "Latencia p95/p99 por endpoint") y [`oms-db-overview`](https://grafana.example.com/d/oms-db-overview). Cloud Trace: <https://console.cloud.google.com/traces/list?project=oms-production>.
- **Setup de sesión** ejecutado (ver [`../README.md`](../README.md#setup-de-sesión-del-on-call-copiar-una-vez-al-empezar-el-incidente)): `promq`, `psql`, `flag_on`/`flag_off`/`flag_get`, `$REGION`.
- `runbook_url` de la alerta trae `job`, `endpoint` y el valor. Expórtalos para copiar los comandos tal cual:

```bash
export JOB=oms-orders ENDPOINT='/api/orders/{id}'     # cámbialos por los de la alerta
```

---

## Diagnóstico (en orden, ejecuta y mira)

> Los comandos están **copiables**. El objetivo de los pasos 0–3 es contestar en 5 min: **¿qué endpoint, qué servicio y qué tramo de la petición?**

### 0. ¿Qué endpoints y servicios están lentos? (30 s)

```bash
promq 'histogram_quantile(0.95, sum by (le, job, endpoint) (rate(http_request_duration_seconds_bucket{job=~"oms-catalog|oms-orders"}[5m]))) > 0.25' | sort -k2 -nr
promq 'histogram_quantile(0.99, sum by (le, job) (rate(http_request_duration_seconds_bucket{job=~"oms-catalog|oms-orders"}[5m])))'
promq 'sum by (job) (rate(http_requests_total{job=~"oms-catalog|oms-orders"}[5m])) / sum by (job) (rate(http_requests_total{job=~"oms-catalog|oms-orders"}[5m] offset 1w))'
```

**Si sale 1 solo endpoint** (p. ej. `/api/orders/{id}`) y el resto está bien: es una query o un cambio de código de ese endpoint. Ve al paso 2 (¿deploy?) y al 6 (queries).
**Si salen casi todos los endpoints de un `job`**: el problema es del servicio (CPU, cold starts, pool). Pasos 1, 4 y 5.
**Si salen los dos `job`**: es algo compartido (Cloud SQL o Redis). Pasos 3 y 6.
**Si la tercera query da > 2** (el doble de tráfico que hace una semana): hay un pico. Pregunta en `#oms-releases` si hay campaña y ten en mente el paso 4 (cold starts) y el runbook 12.
**Si solo sube el p99 y el p95 está bien:** casi siempre son cold starts o GC. Ve directo al paso 4.

Anuncia en `#incident-response`: *"Investigando APILatencyP95High en <job> <endpoint>, p95 = X ms. Sin errores por ahora."*

### 1. ¿Hay errores además de latencia?

```bash
promq 'sum by (job) (rate(http_requests_total{job=~"oms-catalog|oms-orders", code=~"5.."}[5m])) / sum by (job) (rate(http_requests_total{job=~"oms-catalog|oms-orders"}[5m]))'
promq 'max by (job) (db_pool_in_use / db_pool_size)'
```

**Si los 5xx superan el 1 %:** ya no es solo latencia. Si el pool está > 90 %, ve al runbook [`01-db-pool-saturated`](../01-db-pool-saturated/runbook.md). Si el checkout falla, al [`10-checkout-unavailable`](../10-checkout-unavailable/runbook.md).
**Si 5xx < 1 % y pool < 75 %:** sigue aquí.

### 2. ¿Ha habido un despliegue reciente?

```bash
gcloud run revisions list --service=$JOB --region=$REGION --limit=3 \
  --format="table(metadata.name, metadata.creationTimestamp, status.conditions[0].status)"
gcloud run services describe $JOB --region=$REGION --format="yaml(status.traffic)"
promq "histogram_quantile(0.95, sum by (le, revision) (rate(http_request_duration_seconds_bucket{job=\"$JOB\"}[5m])))"
```

**Si la revisión más reciente tiene < 2 h y su p95 es claramente peor que el de la anterior:** es una regresión de código. Ve al runbook [`07-canary-rollout-failed`](../07-canary-rollout-failed/runbook.md) (rollback de tráfico). No sigas investigando la query en caliente: primero se revierte, después se analiza.
**Si no hay revisión nueva en las últimas 24 h, o todas las revisiones están igual de lentas:** no es el deploy. Sigue.

### 3. ¿Qué tramo de la petición se come el tiempo?

Primero, los histogramas por tramo (rápido, en Prometheus):

```bash
promq 'histogram_quantile(0.95, sum by (le, job) (rate(db_pool_acquire_duration_seconds_bucket[5m])))'   # espera por conexión de BD
promq 'histogram_quantile(0.95, sum by (le) (rate(stock_reservation_duration_seconds_bucket[5m])))'      # inventory.reserve (objetivo < 50 ms)
promq 'histogram_quantile(0.95, sum by (le) (rate(payment_provider_request_duration_seconds_bucket{provider="stripe"}[5m])))'
promq 'histogram_quantile(0.95, sum by (le) (rate(auth_token_issue_duration_seconds_bucket[5m])))'       # emisión de token (objetivo < 100 ms)
promq 'sum(rate(cache_requests_total{cache="catalog", result="hit"}[5m])) / sum(rate(cache_requests_total{cache="catalog", result=~"hit|miss"}[5m]))'
```

Después, las trazas lentas reales (Cloud Trace guarda el 100 % de las trazas > 800 ms). Agrupa por nombre de span y ordena por duración media:

```bash
curl -sG "https://cloudtrace.googleapis.com/v1/projects/oms-production/traces" \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  --data-urlencode "startTime=$(date -u -d '-30 min' +%Y-%m-%dT%H:%M:%SZ)" \
  --data-urlencode "endTime=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --data-urlencode "filter=latency:250ms root:\"GET $ENDPOINT\"" \
  --data-urlencode "view=COMPLETE" --data-urlencode "pageSize=50" \
| jq -r 'def t: (.[11:13]|tonumber)*3600 + (.[14:16]|tonumber)*60 + (.[17:-1]|tonumber);
    [.traces[]?.spans[] | {name, ms: (((.endTime|t) - (.startTime|t)) * 1000)}]
    | group_by(.name) | map({span: .[0].name, n: length, avg: (map(.ms)|add/length|floor), max: (map(.ms)|max|floor)})
    | sort_by(-.avg) | .[:10][] | "\(.span)\tn=\(.n)\tavg=\(.avg) ms\tmax=\(.max) ms"'
```

> En `POST /api/orders` (o `/api/cart` que acaba en checkout) los spans son `catalog.snapshot`, `inventory.reserve`, `payments.authorize`, `orders.save` y `outbox.write`. En los `GET` verás spans automáticos de OpenTelemetry: `pg.query` (Postgres) y `redis.*`. El span raíz siempre es el más largo: lo que importa es **qué hijo se acerca a él**.

**Si domina `pg.query`, `orders.save` u `outbox.write`, o la espera del pool es > 50 ms:** el tiempo se va en Postgres. Paso 6.
**Si domina `inventory.reserve` (p95 > 50 ms):** contención de locks en `inventory.stock_items`. Diagnóstico 3 del runbook [`01`](../01-db-pool-saturated/runbook.md#3-hay-alguna-query-bloqueando-a-otras) y Acción 4 de este.
**Si domina `payments.authorize` o el p95 de Stripe es > 1 s:** es Stripe. Ve al runbook [`03-stripe-payment-gateway-degraded`](../03-stripe-payment-gateway-degraded/runbook.md).
**Si domina `catalog.snapshot` o el hit ratio es < 80 %:** la caché no está funcionando. Ve al runbook [`08-redis-cache-hit-ratio-low`](../08-redis-cache-hit-ratio-low/runbook.md) (o al [`05`](../05-redis-cache-down/runbook.md) si Redis no responde). Mientras tanto, Acción 2.
**Si ningún span hijo explica el tiempo** (el raíz dura 900 ms y los hijos suman 100 ms): el tiempo se va **dentro del proceso** (CPU, GC, cola de concurrencia) o **antes** de entrar (cold start). Pasos 4 y 5.

### 4. ¿Son cold starts?

```bash
promq 'sum by (job) (increase(run_container_startup_latency_seconds_count[15m]))'
promq 'histogram_quantile(0.95, sum by (le, job) (rate(run_container_startup_latency_seconds_bucket[15m])))'
promq 'sum by (job) (run_instance_count)'
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="'$JOB'" AND textPayload:"Starting new instance"' \
  --freshness=30m --limit=50 --format="value(timestamp)" | wc -l
gcloud run services describe $JOB --region=$REGION --format=json \
  | jq -r '.spec.template.metadata.annotations | "minScale=\(.["autoscaling.knative.dev/minScale"] // "0")  cpu-boost=\(.["run.googleapis.com/startup-cpu-boost"] // "false")"'
```

**Si hay > 5 arranques en 15 min y la latencia de arranque p95 es > 2 s:** las peticiones que caen en instancias nuevas esperan el arranque. Encaja con "sube el p99 más que el p95". Ve a la **Acción 5** (`min-instances` + startup CPU boost).
**Si hay 0–2 arranques:** no son cold starts. Paso 5.

### 5. ¿Está saturada la CPU de Cloud Run?

```bash
promq 'histogram_quantile(0.99, sum by (le, job) (rate(run_container_cpu_utilization_bucket{job=~"oms-catalog|oms-orders"}[5m])))'
promq 'sum by (job) (run_instance_count) / on (job) max by (job) (run_max_instances)'
promq 'topk(5, sum by (job, endpoint) (rate(http_request_duration_seconds_sum{job=~"oms-catalog|oms-orders"}[5m])))'
```

La tercera query da los **segundos de servicio por segundo** que consume cada endpoint: es la mejor aproximación a "quién se come la CPU".

**Si la CPU p99 es > 85 % o las instancias están al máximo:** la latencia es un síntoma de falta de CPU. Si el top es `/api/search` → **Acción 1**. Si no, sigue el runbook [`12-server-cpu-high`](../12-server-cpu-high/runbook.md).
**Si la CPU es < 70 %:** Cloud Run va sobrado. El cuello está fuera: paso 6.

### 6. ¿Está lenta la base de datos?

```bash
promq 'max(cloudsql_cpu_utilization{database_id="oms-production:oms-pg-prod"})'
psql -X -c "
  SELECT pid, application_name, now() - query_start AS duracion, wait_event_type, wait_event,
         query_id, left(query, 90) AS query
  FROM pg_stat_activity
  WHERE datname = 'oms' AND state = 'active' AND now() - query_start > interval '1 second'
    AND pid <> pg_backend_pid()
  ORDER BY duracion DESC LIMIT 15;"
psql -X -c "
  SELECT queryid, calls, round(mean_exec_time::numeric, 1) AS media_ms,
         round(total_exec_time::numeric / 1000) AS total_s, rows / greatest(calls, 1) AS filas_por_llamada,
         left(regexp_replace(query, '\s+', ' ', 'g'), 100) AS query
  FROM pg_stat_statements
  WHERE dbid = (SELECT oid FROM pg_database WHERE datname = 'oms') AND calls > 50
  ORDER BY mean_exec_time DESC LIMIT 10;"
```

> `pg_stat_statements` es acumulado desde el último reset. Para ver "lo de ahora", cruza con la primera consulta (lo que se ejecuta en este momento) o con **Cloud SQL Query Insights** (consola → `oms-pg-prod` → Query insights, últimos 30 min).

**Si la misma query aparece varias veces en `pg_stat_activity` con > 1 s y tiene `media_ms` alta en `pg_stat_statements`:** esa es la query lenta. Anota `query_id` y el texto en el ticket. **Acción 4** (cancelarla) y, si es de lectura de pedidos, **Acción 3** (réplica).
**Si `wait_event_type = Lock`:** hay contención. Diagnóstico 3 del runbook 01.
**Si la CPU de Cloud SQL es > 85 % y no hay una query dominante:** la BD está al límite por volumen. **Acciones 2 y 3** (quitar lecturas del primario). Si no basta, Acción 6.
**Si la CPU de Cloud SQL es < 50 % y no hay queries > 1 s:** la BD no es el problema. Vuelve al paso 3 con más trazas o escala a `#oms-sre`.

---

## Acciones (de menor a mayor impacto, idempotentes)

> Primero flags (≤ 15 s, sin redeploy, reversibles), después cambios de configuración y, por último, cambios en la BD. Si una acción no mejora el p95 en 10 min, pasa a la siguiente.

### Acción 1: Desactivar las sugerencias de búsqueda (impacto: bajo · flag)

```bash
flag_on catalog.search-suggestions-disabled
flag_get catalog.search-suggestions-disabled
```

**Qué hace:** apaga el autocompletado y las sugerencias de `/api/search`, la parte más cara en CPU de `oms-catalog` y en queries de trigramas contra Postgres. La búsqueda principal sigue funcionando.
**Por qué primero:** es la acción más barata y la que más CPU libera en `oms-catalog` (PM #2: −35 % de CPU).
**Idempotente:** sí. Encender un flag ya encendido no cambia nada.
**Coste:** el usuario tiene que escribir la búsqueda completa. Afecta a la conversión de la búsqueda, no a la compra.
**Cuándo NO:** si el endpoint lento es de `oms-orders` y la CPU de `oms-catalog` está bien, no sirve de nada. **V3:** si `SearchLatencyHigh` está activa, `oms-ops-flag-automator` ya lo habrá encendido (compruébalo con `flag_get`).

### Acción 2: Servir el catálogo desde caché aunque haya expirado (impacto: bajo · flag)

```bash
flag_on catalog.serve-stale
```

**Qué hace:** el catálogo sirve la última versión cacheada en Redis aunque el TTL haya expirado (stale-while-revalidate) y deja de ir a Postgres en cada miss.
**Cuándo:** el paso 3 mostró `catalog.snapshot` lento o un hit ratio bajo, o el paso 6 mostró CPU de Cloud SQL alta.
**Idempotente:** sí.
**Coste:** precio o stock mostrados con unos minutos de retraso. El checkout **siempre** revalida precio y stock contra la BD, así que no se vende a un precio incorrecto.

### Acción 3: Leer pedidos de la réplica (impacto: bajo-medio · flag)

```bash
promq 'cloudsql_replication_lag_seconds{replica="oms-pg-prod-replica"}'    # tiene que ser < 5 s
flag_on orders.read-from-replica
```

**Qué hace:** `GET /api/orders*` y el backoffice leen de `oms-pg-prod-replica`, que tiene su propia CPU y su propio cache de páginas. El primario queda para escrituras y checkout.
**Cuándo:** el endpoint lento es `GET /api/orders*` y el paso 6 apunta a la BD (CPU > 70 % o query de lectura lenta).
**Idempotente:** sí.
**Coste:** un pedido recién creado puede tardar ~1 s en aparecer en "Mis pedidos".
**Cuándo NO:** si el lag de la réplica es > 5 s (el cliente vería pedidos que "no existen") o si la query lenta es una escritura.

### Acción 4: Cancelar la query lenta (impacto: medio · solo con un `query_id` identificado en el paso 6)

```bash
export QUERY_ID=<query_id del paso 6>
psql -X -c "
  SELECT pid, now() - query_start AS duracion, pg_cancel_backend(pid) AS cancelada
  FROM pg_stat_activity
  WHERE datname = 'oms' AND usename = 'oms_app' AND state = 'active'
    AND query_id = $QUERY_ID AND now() - query_start > interval '5 seconds'
    AND pid <> pg_backend_pid();"
```

**Qué hace:** cancela (no termina) las ejecuciones en curso de esa query que llevan > 5 s. La conexión sigue viva y vuelve al pool.
**Idempotente:** sí. Si no queda ninguna ejecución larga, devuelve 0 filas.
**Coste:** las peticiones que esperaban esa query reciben un error y el usuario reintenta. Si la query viene de un job batch, antes de cancelar en bucle pausa el job: `flag_on orders.batch-jobs-paused`.
**Cuándo NO:** si `application_name` es `cloudsqladmin` o la sesión es de `oms_ops` (un humano lanzando un `CREATE INDEX` o un `VACUUM`). Pregunta en `#oms-sre` antes.
**Ojo:** cancelar alivia, no arregla. Si la query vuelve a entrar con cada petición, la solución es la Acción 6 (índice) o revertir la revisión que la introdujo (runbook 07).

### Acción 5: Quitar cold starts con instancias mínimas (impacto: medio · nueva revisión sin downtime)

```bash
gcloud run services describe $JOB --region=$REGION --format=json \
  | jq -r '.spec.template.metadata.annotations["autoscaling.knative.dev/minScale"] // "0"'    # anota el valor actual para el rollback
gcloud run services update $JOB --region=$REGION --min-instances=6 --cpu-boost
```

**Qué hace:** mantiene 6 instancias calientes y da CPU extra durante el arranque (startup CPU boost), que reduce el tiempo de arranque a la mitad.
**Por qué no rompe la fórmula de capacidad del pool:** la fórmula (`max-instances × DB_POOL_SIZE < 380`) depende de `max-instances`, que **no se toca**. Las instancias mínimas abren su pool, pero siempre por debajo del techo ya calculado.
**Idempotente:** sí. Repetirlo con los mismos valores no crea revisión nueva.
**Coste:** ~60 s de rolling update sin downtime y las instancias idle se facturan (6 instancias de 1 vCPU ≈ 6–8 €/día). Fuera de ventana (OPS-002) está permitido para mitigar, pero se anota en el ticket.
**Cuándo NO:** si el paso 4 no mostró cold starts. Más instancias calientes no arreglan una query lenta.

### Acción 6: Crear el índice que falta (impacto: alto · requiere aprobación)

Solo si el paso 6 identificó una query de lectura con seq scan y la Acción 4 solo alivia unos minutos.

```bash
psql -X -c "EXPLAIN (ANALYZE false, BUFFERS false) <texto de la query con parámetros de ejemplo>;"   # confirma el Seq Scan
psql -X -c "SET lock_timeout = '5s'; CREATE INDEX CONCURRENTLY IF NOT EXISTS <nombre_idx> ON orders.orders (<columnas>);"
psql -X -c "SELECT indexrelid::regclass, indisvalid FROM pg_index WHERE indexrelid = '<esquema>.<nombre_idx>'::regclass;"
```

**Qué hace:** crea el índice sin bloquear escrituras (`CONCURRENTLY`). Tarda minutos en tablas grandes y consume CPU e IO de Cloud SQL mientras tanto.
**Idempotente:** sí, por `IF NOT EXISTS`. **Pero** si se interrumpe, deja un índice `INVALID` que el `IF NOT EXISTS` da por existente: si `indisvalid = false`, bórralo (`DROP INDEX CONCURRENTLY <esquema>.<nombre_idx>;`) y vuelve a lanzarlo.
**Quién aprueba:** Engineering Manager de guardia (PagerDuty nivel 2) y el tech lead del módulo. Es un cambio de esquema fuera del pipeline: el índice se tiene que añadir **el mismo día** como migración en el repo, o el siguiente deploy lo dará por inexistente.
**Cuándo NO:** si la CPU de Cloud SQL es > 85 % (el `CREATE INDEX` la empeora). Primero Acciones 2 y 3. Si la BD sigue saturada, la vía es escalar el tier (runbook 01, Acción 4).

---

## Verificación

**Métricas que tienen que volver a la normalidad:**
```promql
# 1. p95 de cada endpoint crítico por debajo de 250 ms (NFR-PERF-002)
max(histogram_quantile(0.95, sum by (le, job, endpoint) (rate(http_request_duration_seconds_bucket{job=~"oms-catalog|oms-orders", endpoint=~"/api/orders.*|/api/products.*|/api/cart.*|/api/auth/token"}[5m])))) < 0.25

# 2. p99 por debajo de 800 ms (NFR-PERF-003)
max(histogram_quantile(0.99, sum by (le, job) (rate(http_request_duration_seconds_bucket{job=~"oms-catalog|oms-orders"}[5m])))) < 0.8

# 3. SLO 2: menos del 5 % de GET /api/orders* por encima de 250 ms en la última hora (burn rate < 1)
slo:orders_get_slow250:ratio_rate1h < 0.05

# 4. Si la alerta era de búsqueda: p95 de /api/search < 300 ms
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="oms-catalog", endpoint="/api/search"}[5m]))) < 0.3
```

**Ventana de observación:** **15 minutos consecutivos** con las condiciones en verde (más que en el runbook 01 porque la latencia oscila más que un pool).

**Cómo confirmas:**
1. Panel "Latencia p95/p99 por endpoint" de [`oms-overview`](https://grafana.example.com/d/oms-overview), filtrado por `$JOB` y `$ENDPOINT`.
2. Si has encendido flags, comprueba que el p95 sigue bien **con el tráfico real** (no en un valle): mira `rate(http_requests_total)` durante la ventana.
3. Confirma que `APILatencyP95High` y `OrdersLatencyBurnRate` están *resolved* en Alertmanager.
4. Comunica en `#incident-response`: *"Latencia normalizada: p95 de <endpoint> = XX ms, p99 = YY ms, estables 15 min. Causa: <query / cold starts / CPU / dependencia>. Acciones: N. Flags activos: [lista]. Cerrando."*

---

## Rollback

| Acción | Rollback | Cuándo |
|---|---|---|
| 1 (sugerencias) | `flag_off catalog.search-suggestions-disabled` | Tras 30 min con `/api/search` p95 < 200 ms y CPU de `oms-catalog` < 60 %. Si el p95 vuelve a subir, enciéndelo otra vez y deja el ticket abierto. |
| 2 (serve-stale) | `flag_off catalog.serve-stale` | En cuanto el hit ratio vuelva a > 90 % y la CPU de Cloud SQL < 70 %. No lo dejes más de 4 h: el catálogo se aleja de la realidad. |
| 3 (réplica) | `flag_off orders.read-from-replica` | Inmediato si `cloudsql_replication_lag_seconds{replica="oms-pg-prod-replica"}` > 5 s. Si no, cuando la query lenta esté corregida. |
| 4 (cancelar query) | No hay nada que deshacer: la conexión sigue viva. Si cancelaste un job, `flag_off orders.batch-jobs-paused` cuando acabe el incidente. | — |
| 5 (min-instances) | `gcloud run services update $JOB --region=$REGION --min-instances=<valor anotado>` (el `--cpu-boost` se deja: no tiene coste en reposo) | Al día siguiente, o cuando pase la campaña. Si empeoró algo, el rollback inmediato es `gcloud run services update-traffic $JOB --region=$REGION --to-revisions=<REVISION_ANTERIOR>=100`. |
| 6 (índice) | `psql -X -c "DROP INDEX CONCURRENTLY IF EXISTS <esquema>.<nombre_idx>;"` | Solo si el índice empeora las escrituras (p95 de `orders.save` sube > 30 %). Si no, se queda y se formaliza como migración. |

---

## Después del incidente

- [ ] Anota en el ticket: endpoint, `job`, tramo culpable (span), causa y qué acción fue suficiente.
- [ ] **Deja los flags como estaban** (`flag_get` de cada uno), salvo que el post-mortem decida lo contrario.
- [ ] Si fue una query: abre una issue al equipo del módulo con el `query_id`, el plan (`EXPLAIN`) y el commit que la introdujo. Si `query-plan-check` (V3) no la paró en CI, añade el caso a sus tests.
- [ ] Si fueron cold starts: revisa si `min-instances` base tiene que subir de forma permanente (decisión de capacidad, con el coste delante).
- [ ] Calcula el budget del SLO 2 consumido (`slo:orders_get_slow250:ratio_rate1h` integrado en la duración) y aplícale la [política de error budget](../../slos/error-budget-policy.md).
- [ ] Si llegaste a la Acción 6: post-mortem en < 48 h.
- [ ] Si algún paso no sirvió o faltó: actualiza este runbook en el mismo PR del post-mortem.

---

## Enlaces relacionados

- Alerta YAML: [`alert.yaml`](alert.yaml)
- Post-mortems históricos: [`post-mortems.md`](post-mortems.md)
- SLO que protege: [`../../slos/slos-orders.md`](../../slos/slos-orders.md) (SLO 2, latencia de `GET /api/orders*`) · [política de error budget](../../slos/error-budget-policy.md)
- Instrumentación (métricas y spans): [`../../observability/instrumentation.md`](../../observability/instrumentation.md)
- Dashboards: <https://grafana.example.com/d/oms-overview> · <https://grafana.example.com/d/oms-db-overview>
- Runbooks relacionados: [`01-db-pool-saturated`](../01-db-pool-saturated/runbook.md) (espera de pool) · [`03-stripe-payment-gateway-degraded`](../03-stripe-payment-gateway-degraded/runbook.md) (Stripe lento) · [`07-canary-rollout-failed`](../07-canary-rollout-failed/runbook.md) (regresión de revisión) · [`08-redis-cache-hit-ratio-low`](../08-redis-cache-hit-ratio-low/runbook.md) (caché) · [`12-server-cpu-high`](../12-server-cpu-high/runbook.md) (CPU de Cloud Run)
