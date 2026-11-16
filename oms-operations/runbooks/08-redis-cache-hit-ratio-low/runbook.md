# Runbook: Hit ratio bajo en la caché del catálogo (Redis vivo, pero sin aciertos)

> **Adicional (fuera del catálogo).** Es la avería silenciosa de la caché: Redis responde, no hay errores, pero cada vez menos lecturas aciertan y la carga vuelve a Postgres sin que nada se rompa... todavía.
> **Versión:** V3 (ver `post-mortems.md`). **Owner:** equipo SRE (con el equipo Catalog como co-owner de claves y TTL). **Última revisión:** 2026-11-03.

---

## Trigger

**Alerta:** `RedisCacheHitRatioLow` (severity: **warning**, Slack `#oms-alerts`; no pagina)
**Condición:** `hit / (hit + miss)` de `cache_requests_total{cache="catalog"}` **< 70 %** durante **10 minutos**, con al menos 5 lecturas/s (de noche, con poco tráfico, el ratio es ruidoso). La base es **> 90 %**.
**Alerta temprana asociada:** `RedisMemoryPressure` (warning): memoria > 90 % del máximo **y** evictions durante 15 min. Suele adelantarse al hit ratio.
**Alerta de la automatización V3:** `CatalogCacheWarmerStale` (el pre-calentado diario no se ha ejecutado en 26 h).
**Escalado:** este runbook no pagina. Si el hit ratio bajo ya afecta al usuario, lo que pagina es `APILatencyP95High` ([runbook 02](../02-api-p95-high/runbook.md)) o `DBConnectionPoolSaturated` ([runbook 01](../01-db-pool-saturated/runbook.md)). Si llegas desde ahí, este es el sitio correcto para la causa.

**Cuándo dispara (patrones conocidos):**
1. **Invalidación masiva**: un import de catálogo del backoffice (cambio de precios de temporada) emite un `ProductUpdated` por producto, y cada uno invalida el producto y **todos los listings de su categoría** (ADR-005 §3). Con miles de productos, la caché se vacía en bucle (PM #1).
2. **TTL mal configurado tras un deploy**: una revisión nueva de `oms-catalog` lee `CATALOG_CACHE_TTL_*` en otra unidad o con otro valor (PM #2) → [runbook 07](../07-canary-rollout-failed/runbook.md).
3. **Cambio de formato de clave**: la revisión nueva usa otro prefijo de versión (`catalog:v4:` → `catalog:v5:`). La caché arranca fría; es esperado y dura ~30 min, pero en hora punta duele.
4. **Evictions por memoria**: el dataset ha crecido (más productos, más combinaciones de filtros) y no cabe en `memory-size-gb`. Redis expulsa claves calientes.
5. **Scraping del long tail**: un bot recorre los 50.000 productos, la mayoría fríos. Cada petición es un miss y, además, las claves frías expulsan a las calientes.

---

## Contexto

**Qué impacto tiene (en lenguaje de negocio):**
- **El catálogo va más lento.** Cada miss cuesta lo que cuesta Postgres: el listing pasa de ~35 ms a ~220 ms (ADR-005). Con el hit ratio al 60 %, el p95 de `GET /api/products` supera los **150 ms de NFR-PERF-001**: navegar se nota lento y la conversión baja.
- **Postgres recibe la carga que Redis dejaba de absorber.** Con un hit ratio del 90 % llegan a Postgres 1 de cada 10 lecturas; con el 60 %, 4 de cada 10 (**×4**). El primario es compartido: si la CPU de Cloud SQL sube, también sufren el checkout (SLO 1 de Orders) y "Mis pedidos" (SLO 2 de Orders).
- **No hay errores.** Por eso es una warning: hay margen para diagnosticar en horario laboral, siempre que la latencia y la CPU aguanten.

**Qué NO afecta:**
- **Disponibilidad del catálogo** (NFR-AVAIL-002): no hay 5xx mientras Postgres aguante.
- **Checkout, pagos, outbox**: no usan la caché (ADR-005 §4). Solo los toca la CPU compartida de Cloud SQL.
- **Carritos**: viven en el mismo Redis (`cart:*`, TTL 7 d) y no cuentan en este ratio. Pero con evictions por memoria **también se pueden expulsar carritos** (`maxmemory-policy=volatile-lru`); la SPA los recupera de `localStorage` ([runbook 05](../05-redis-cache-down/runbook.md#contexto)).
- **Precios**: un hit ratio bajo nunca sirve precios incorrectos. Lo contrario (servir stale) sí puede mostrar precios de hace unos minutos, pero el checkout revalida siempre contra Postgres.

**Pre-requisitos para diagnosticar:**
- Grupo `oms-oncall@` con `roles/redis.viewer` (y `roles/redis.admin` para cambiar el tamaño), `roles/iap.tunnelResourceAccessor`, `roles/run.developer`, `roles/logging.viewer` y `roles/compute.securityAdmin` (regla de Cloud Armor contra bots).
- Dashboard [`oms-cache`](https://grafana.example.com/d/oms-cache) (paneles "Hit ratio por key_type", "Hit ratio por revisión", "Memoria / evictions", "Invalidaciones por motivo") y [`oms-deploys`](https://grafana.example.com/d/oms-deploys).
- **Setup de sesión** ejecutado (ver [`../README.md`](../README.md#setup-de-sesión-del-on-call-copiar-una-vez-al-empezar-el-incidente)), más el helper del bastión del [runbook 05](../05-redis-cache-down/runbook.md#contexto):

```bash
bastion() { gcloud compute ssh oms-ops-bastion --zone=europe-west3-a --tunnel-through-iap --command "$1"; }
```

**Formato de clave** (lo necesitas para el paso 3): `catalog:<versión>:listing:<categoryId>:<filtrosHash>:<página>`, `catalog:<versión>:product:<id>`, `catalog:<versión>:categories`. La versión actual es `v4`. TTL lógicos (ADR-005): listing 300 s, producto 30 s, categorías 1.800 s. TTL físico = TTL lógico + 1 h (margen para `catalog.serve-stale`).

---

## Diagnóstico (en orden, ejecuta y mira)

> Los comandos están **copiables**. Las causas están ordenadas por frecuencia: la primera que encaje suele ser la buena.

### 0. ¿Es real y ya afecta al usuario? (30 s)

```bash
promq 'sum by (key_type) (rate(cache_requests_total{job="oms-catalog", cache="catalog", result="hit"}[5m])) / sum by (key_type) (rate(cache_requests_total{job="oms-catalog", cache="catalog", result=~"hit|miss"}[5m]))'
promq 'sum(rate(cache_requests_total{job="oms-catalog", cache="catalog", result=~"hit|miss"}[5m]))'
promq 'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="oms-catalog", endpoint=~"/api/products.*"}[5m])))'
promq 'max(cloudsql_cpu_utilization{database_id="oms-production:oms-pg-prod"})'
```

**Si el ratio cae en todos los `key_type` a la vez:** causa global (evictions, deploy, invalidación masiva). Sigue en orden.
**Si cae solo `product`:** típico de scraping del long tail (paso 4) o de un import que toca muchos productos (paso 2).
**Si cae solo `listing`:** invalidaciones por categoría (paso 2) o filtros nuevos con combinaciones que no se repiten (`filtrosHash` distinto en cada petición).
**Si el p95 > 150 ms o la CPU de Cloud SQL > 70 %:** ya hay impacto. Ejecuta la **Acción 1** antes de seguir y avisa en `#oms-alerts`: *"Hit ratio de catálogo al XX %, mitigando con serve-stale"*.

### 1. ¿Faltan memoria y hay evictions?

```bash
promq 'max(redis_memory_used_bytes{instance_id="oms-redis-prod"} / redis_memory_max_bytes{instance_id="oms-redis-prod"})'
promq 'sum(rate(redis_evicted_keys_total{instance_id="oms-redis-prod"}[5m]))'
bastion 'oms-redis-cli INFO keyspace'                      # keys, expires, avg_ttl (ms)
gcloud redis instances describe oms-redis-prod --region=$REGION --format="yaml(memorySizeGb,redisConfigs)"
```

**Memoria > 90 % y evictions > 10/s:** la caché no cabe. Acción 2 (si es un bot) o **Acción 5** (escalar memoria).
**Evictions = 0:** la memoria no es la causa. Sigue con el paso 2.
**`maxmemory-policy` distinto de `volatile-lru`:** alguien lo ha cambiado (Memorystore no permite `CONFIG SET`, solo `gcloud ... --update-redis-config`). Revisa el audit log del paso 3 y devuélvelo a `volatile-lru`.

### 2. ¿Hay una invalidación masiva (import del backoffice)?

```bash
promq 'sum by (reason) (rate(cache_invalidations_total{cache="catalog"}[5m])) * 60'     # invalidaciones por minuto
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="oms-catalog"
  AND jsonPayload.event=~"^catalog.import.(started|completed)$"' --freshness=6h --limit=10 \
  --format="table(timestamp,jsonPayload.event,jsonPayload.import_id,jsonPayload.products_count)"
```

**`reason="bulk_import"` > 100/min (la base es < 5/min):** un import está vaciando la caché. Acción 1 + **Acción 4a** (pausar el import) y avisa en `#oms-backoffice`.
**`reason="event"` alto sin import:** algún proceso está emitiendo `ProductUpdated` en bucle (por ejemplo, una sincronización de stock mal hecha). Busca el origen por `jsonPayload.source` en los logs y escala al equipo Catalog.
**Invalidaciones normales:** sigue con el paso 3.

### 3. ¿Ha cambiado algo en un deploy (TTL o formato de clave)?

```bash
promq 'sum by (revision) (rate(cache_requests_total{job="oms-catalog", cache="catalog", result="hit"}[5m])) / sum by (revision) (rate(cache_requests_total{job="oms-catalog", cache="catalog", result=~"hit|miss"}[5m]))'
gcloud run revisions list --service=oms-catalog --region=$REGION --limit=3 --format="table(metadata.name,metadata.creationTimestamp,status.conditions[0].status)"
# Compara las variables de caché entre la revisión actual y la anterior
for r in <REVISION_ACTUAL> <REVISION_ANTERIOR>; do echo "== $r"; gcloud run revisions describe $r --region=$REGION \
  --format="value(spec.containers[0].env)" | tr ';' '\n' | grep -E "CATALOG_CACHE_(TTL|KEY_VERSION)"; done
# ¿Qué prefijos hay en Redis y qué TTL tienen las claves?
bastion "oms-redis-cli --scan --pattern 'catalog:*' -i 0.01 | head -5000 | cut -d: -f1-3 | sort | uniq -c | sort -rn"
bastion "oms-redis-cli --scan --pattern 'catalog:v*:listing:*' | head -10 | xargs -n1 oms-redis-cli TTL"
```

**Hit ratio bajo solo en la revisión nueva:** la causa está en el deploy. Si hay canary en curso, para el rollout y vuelve atrás ([runbook 07](../07-canary-rollout-failed/runbook.md)).
**`TTL` de los listings de 0–5 s (lo esperado: hasta 3.900 s, contando la hora de margen) o `avg_ttl` del paso 1 muy bajo:** TTL mal configurado (PM #2). **Acción 4b**.
**Conviven dos prefijos (`catalog:v4:` y `catalog:v5:`) y el nuevo crece:** cambio de formato de clave. Es un arranque en frío esperado: **Acción 3** (pre-calentar) y espera. Las claves viejas expiran solas en ≤ 1,5 h; no las borres a mano (un `DEL` masivo bloquea Redis).
**Nada ha cambiado en 24 h:** sigue con el paso 4.

### 4. ¿Hay un bot recorriendo el catálogo (long tail)?

```bash
# Logs del Load Balancer (muestreo del 10 % en el backend oms-catalog: multiplica los recuentos por 10)
gcloud logging read 'resource.type="http_load_balancer" AND resource.labels.backend_service_name="oms-catalog"
  AND httpRequest.requestUrl:"/api/products/"' --freshness=15m --limit=20000 \
  --format="value(httpRequest.remoteIp,httpRequest.userAgent)" | sort | uniq -c | sort -rn | head -10
```

**Una IP o rango con > 5 % de las peticiones a `/api/products/{id}`, user agent de script (`python-requests`, `Go-http-client`, vacío) o ids consecutivos:** scraping. **Acción 2**.
**Tráfico repartido, sin bot:** si tampoco hay evictions, invalidaciones ni deploy, es un cambio de patrón de uso (campaña sobre productos fríos). Acción 1 + Acción 3 y revisa el dimensionado (Acción 5) en horario laboral.

---

## Acciones (de menor a mayor impacto, idempotentes)

> Primero, que el usuario no lo note (Acción 1). Después, quitar la causa. Escala solo si la anterior no basta en 10 min.

### Acción 1: Servir stale mientras se recupera la caché (impacto: bajo · reversible en 15 s)

```bash
flag_on catalog.serve-stale
flag_get catalog.serve-stale
```

**Qué hace:** si la clave existe en Redis pero su `soft_expires_at` ya pasó, se sirve igualmente y se refresca en segundo plano (stale-while-revalidate, una sola petición a Postgres por clave).
**Cuándo ayuda:** TTL demasiado corto (PM #2) e invalidaciones. **Cuándo no ayuda:** evictions (la clave ya no existe), prefijo nuevo (no hay claves con ese prefijo) y bot (claves frías que nunca existieron). En esos casos no hace daño, pero no esperes que el ratio suba.
**Idempotente:** sí.
**Coste:** el catálogo puede enseñar precio o stock con hasta 1 h de antigüedad en el peor caso; el checkout revalida siempre contra Postgres, así que nunca se cobra un precio incorrecto. **Cuándo NO:** durante un cambio de precios urgente pedido por negocio (por ejemplo, retirar una oferta errónea); en ese caso, coordina antes con `#oms-backoffice`.

### Acción 2: Limitar al bot en Cloud Armor (impacto: bajo · solo si el paso 4 encontró scraping)

```bash
gcloud compute security-policies rules describe 1100 --security-policy=oms-edge-policy >/dev/null 2>&1 \
  || gcloud compute security-policies rules create 1100 --security-policy=oms-edge-policy \
       --description="runbook-08: throttle scraping /api/products ($(date +%F))" \
       --expression="request.path.startsWith('/api/products/') && inIpRange(origin.ip, '<RANGO_DEL_BOT>/24')" \
       --action=throttle --rate-limit-threshold-count=120 --rate-limit-threshold-interval-sec=60 \
       --conform-action=allow --exceed-action=deny-429 --enforce-on-key=IP
```

**Qué hace:** a ese rango le deja 120 peticiones/min por IP a fichas de producto (un humano no pasa de 20) y devuelve 429 al resto. El usuario real no se entera. Es más estricta que la regla permanente (prioridad 1000: 300/min por IP para todo el mundo, desde PM #3), que un bot repartido entre muchas IPs puede esquivar.
**Por qué un rango concreto y no un bloqueo:** un `deny` por país o por un rango amplio es justo lo que tumbó el catálogo en el [PM #1 del runbook 11](../11-catalog-unavailable/post-mortems.md). Usa el rango más pequeño que cubra al bot.
**Idempotente:** sí. El `describe ||` evita crear la regla dos veces; si ya existe con otro rango, edítala con `rules update 1100 --expression=...`.
**Cuándo NO:** si el user agent es un buscador verificado (Googlebot desde los rangos publicados de Google). El SEO del catálogo depende de él; en ese caso, solo Acción 1 + Acción 3.

### Acción 3: Pre-calentar los productos y listings más vistos (impacto: bajo · carga controlada sobre Postgres)

```bash
gcloud run jobs execute oms-catalog-cache-warmer --region=$REGION --args="--top=2000" --wait
```

**Qué hace:** el job lee el top 2.000 de productos y listings de las últimas 24 h (de los logs agregados del catálogo) y los carga en Redis con el TTL vigente. Usa una conexión propia (`DB_POOL_SIZE=2`, `application_name=oms-cache-warmer`) contra **la réplica de lectura**, así que no compite con el checkout. Tarda ~3 min.
**Idempotente:** sí. Volver a escribir una clave con el mismo valor solo renueva su TTL.
**Cuándo NO:** si la causa es TTL mal configurado (las claves volverían a caducar en segundos: arregla antes el TTL con la Acción 4b) o evictions al 100 % (las claves precalentadas expulsarían a otras: primero la Acción 5).

### Acción 4: Quitar la causa (impacto: medio · según lo que encontró el diagnóstico)

**4a. Import masivo → pausarlo con flag:**
```bash
flag_on catalog.bulk-import-paused
```
El importador del backoffice comprueba el flag antes de cada lote de 500 productos y se para limpiamente; el import queda a medias y se reanuda desde su último lote. Avisa en `#oms-backoffice`: *"Import de catálogo pausado: está vaciando la caché. Lo reanudamos a las HH:MM, fuera de hora punta."* Idempotente.

**4b. TTL mal configurado → corregir la variable (o revertir la revisión):**
```bash
gcloud run services update oms-catalog --region=$REGION \
  --update-env-vars=CATALOG_CACHE_TTL_LISTING_SECONDS=300,CATALOG_CACHE_TTL_PRODUCT_SECONDS=30,CATALOG_CACHE_TTL_CATEGORIES_SECONDS=1800
```
Crea una revisión con los TTL de ADR-005; idempotente (los mismos valores no crean revisión nueva). Si el problema es de código (unidades) y no de variable, revierte el tráfico: `gcloud run services update-traffic oms-catalog --region=$REGION --to-revisions=<REVISION_ANTERIOR>=100` ([runbook 07](../07-canary-rollout-failed/runbook.md)). Fuera de la ventana de OPS-002 está permitido para mitigar; anótalo en el ticket.

### Acción 5: Escalar la memoria de Memorystore (impacto: medio-alto · requiere aprobación del EM en hora punta)

```bash
gcloud redis instances describe oms-redis-prod --region=$REGION --format="value(memorySizeGb)"   # anótalo para el rollback
gcloud redis instances update oms-redis-prod --region=$REGION --size=12
```

**Qué hace:** amplía la instancia de 8 a 12 GB. En STANDARD_HA, Memorystore redimensiona la réplica, hace failover y después redimensiona el antiguo primario: hay **cortes de conexión de segundos** y la instancia está en `UPDATING` varios minutos. El circuit breaker lo absorbe.
**Idempotente:** sí. Repetirlo con el mismo tamaño no cambia nada.
**Coste:** ~+50 % del coste de Memorystore. Revísalo contra OPS-003 (800 €/mes) en el post-mortem.
**Cuándo NO:** si las evictions las provoca un bot (Acción 2) o un import (Acción 4a). Más memoria solo alarga el problema.
**Quién aprueba:** el EM de guardia si es en hora punta (10:00–14:00 y 19:00–23:00) o en día de campaña; fuera de esas horas, el on-call y lo anota en el ticket.

---

## Verificación

**Métricas que tienen que volver a la normalidad:**
```promql
# 1. Hit ratio por encima del 90 % (la base)
sum(rate(cache_requests_total{job="oms-catalog", cache="catalog", result="hit"}[5m]))
  / sum(rate(cache_requests_total{job="oms-catalog", cache="catalog", result=~"hit|miss"}[5m])) > 0.9
# 2. Sin evictions sostenidas
sum(rate(redis_evicted_keys_total{instance_id="oms-redis-prod"}[5m])) < 1
# 3. NFR-PERF-001: p95 del catálogo < 150 ms
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="oms-catalog", endpoint=~"/api/products.*"}[5m]))) < 0.15
# 4. Postgres liberado
max(cloudsql_cpu_utilization{database_id="oms-production:oms-pg-prod"}) < 0.6
```

**Ventana de observación:** **30 minutos** consecutivos (dos ciclos completos del TTL de listings más margen). Si usaste `serve-stale`, la ventana empieza **después** de apagarlo.

**Cómo confirmas:**
1. Paneles "Hit ratio por key_type" y "Memoria / evictions" en [`oms-cache`](https://grafana.example.com/d/oms-cache).
2. Apaga `catalog.serve-stale` y comprueba que el ratio se mantiene > 90 % otros 15 min. Si cae, la causa sigue activa: vuelve al diagnóstico.
3. `RedisCacheHitRatioLow` y `RedisMemoryPressure` en *resolved*.
4. Comunica en `#oms-alerts` (o en `#incident-response` si hubo incidente abierto): *"Hit ratio de catálogo recuperado: XX % durante 30 min, p95 XX ms, evictions 0. Causa: [import / TTL / prefijo / memoria / bot]. Acciones: N. Flags activos: [lista]."*

---

## Rollback

| Acción | Rollback | Cuándo |
|---|---|---|
| 1 (serve-stale) | `flag_off catalog.serve-stale` | Cuando el hit ratio esté > 90 % durante 15 min, o de inmediato si negocio necesita que un cambio de precio se vea ya. |
| 2 (throttle al bot) | `gcloud compute security-policies rules update 1100 --security-policy=oms-edge-policy --preview` (la deja en modo observación, sin borrarla) y después `rules delete 1100` | Si bloquea a usuarios reales (paso 1 del [runbook 11](../11-catalog-unavailable/runbook.md)) o tras 7 días sin tráfico del bot. |
| 3 (pre-calentado) | No hace falta: las claves expiran por TTL. | — |
| 4a (import pausado) | `flag_off catalog.bulk-import-paused` | Fuera de hora punta, con el hit ratio > 90 %. Si el import es grande, pide que se relance en modo `--batch-invalidate`. |
| 4b (TTL) | `gcloud run services update-traffic oms-catalog --region=$REGION --to-revisions=<REVISION_ANTERIOR>=100` | Si la revisión nueva da errores. |
| 5 (más memoria) | `gcloud redis instances update oms-redis-prod --region=$REGION --size=<TAMAÑO_ANOTADO>` | **No** en el mismo día: reducir con el dataset que no cabía devuelve las evictions. Se decide en el post-mortem con 7 días de datos de memoria. |

---

## Después del incidente

- [ ] Anota en el ticket la causa (una de las 5 del Trigger), el hit ratio mínimo, la duración y si el p95 llegó a superar 150 ms.
- [ ] Comprueba con `flag_get` que `catalog.serve-stale` y `catalog.bulk-import-paused` están como estaban.
- [ ] Si fue un import: confirma con `#oms-backoffice` que se relanza con `--batch-invalidate` (una única invalidación por categoría al final, no una por producto).
- [ ] Si fue un deploy: el análisis del canary ([runbook 07](../07-canary-rollout-failed/runbook.md)) tiene que incluir el hit ratio por revisión. Si no lo paró, abre ticket.
- [ ] Si fue memoria: abre ticket de capacity planning con la curva de `redis_memory_used_bytes` de 30 días.
- [ ] Si el ratio sostenido de la semana está por debajo del 70 %, se cumple el criterio de re-evaluación de ADR-005: llévalo a la revisión de arquitectura.
- [ ] Si algún paso no sirvió, actualiza este runbook.

---

## Enlaces relacionados

- Alerta YAML: [`alert.yaml`](alert.yaml) · Post-mortems: [`post-mortems.md`](post-mortems.md)
- Decisión de arquitectura: [`ADR-005`](../../../anexo-arquitectura/05-adrs/ADR-005-redis-cache-catalogo.md) (TTL, invalidación, criterios de re-evaluación)
- SLO afectado por la CPU compartida de Cloud SQL: [`../../slos/slos-orders.md`](../../slos/slos-orders.md) (SLO 2, latencia) · NFR-PERF-001 en [`../../observability/instrumentation.md`](../../observability/instrumentation.md)
- Dashboard: <https://grafana.example.com/d/oms-cache>
- Runbooks relacionados: [`05-redis-cache-down`](../05-redis-cache-down/runbook.md) (si Redis falla en vez de fallar el ratio), [`02-api-p95-high`](../02-api-p95-high/runbook.md) (cuando ya hay latencia de usuario), [`07-canary-rollout-failed`](../07-canary-rollout-failed/runbook.md) (si la causa es una revisión nueva), [`01-db-pool-saturated`](../01-db-pool-saturated/runbook.md) (si los misses saturan el pool), [`11-catalog-unavailable`](../11-catalog-unavailable/runbook.md)
