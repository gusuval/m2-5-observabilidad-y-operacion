# Runbook: Redis de caché caído (Memorystore `oms-redis-prod`)

> **Bonus (5.º runbook).** Es el ejemplo de **degradación elegante** (OPS-007): Redis puede caerse y el catálogo tiene que seguir sirviendo. El riesgo real no es Redis, sino lo que le pasa a Postgres cuando Redis deja de absorber el 85 % de las lecturas.
> **Versión:** V3 (ver `post-mortems.md`). **Owner:** equipo SRE (con el equipo Catalog como co-owner del adapter de caché). **Última revisión:** 2026-09-29.

---

## Trigger

**Alerta:** `RedisCacheDown` (severity: **critical**, PagerDuty). Tiene dos reglas con el mismo nombre; la etiqueta `trigger` dice cuál ha saltado:
- `trigger="redis_up"`: `max(redis_up{instance_id="oms-redis-prod"}) == 0` durante **1 minuto** (Memorystore no responde según Cloud Monitoring).
- `trigger="app_errors"`: más del **50 %** de las llamadas de la app a Redis (`cache_requests_total{result="error"}`) fallan durante **2 minutos**, con un mínimo de tráfico. Detecta los casos en que Memorystore dice estar sano pero la app no llega (red, AUTH, TLS).

**Alerta temprana asociada:** `RedisCacheErrorsElevated` (warning, Slack `#oms-alerts`): errores parciales > 5 % durante 5 min.
**Alertas de la automatización V3:** `CatalogCacheBypassLeftOn` (el bypass sigue activo 2 h después de que Redis volviera) y `FlagAutomatorStale` (el automatizador de flags no responde).

**Cuándo dispara (patrones conocidos):**
1. **Failover de STANDARD_HA** por fallo del nodo primario o de la zona: 30 s–2 min sin servicio. Suele resolverse solo.
2. **Mantenimiento de Google** (ventana configurada: martes 02:00 UTC) o **reparación** (`state=REPAIRING`): de minutos a ~15 min. Puede volver con el dataset **vacío** (PM #2).
3. **Conectividad**: cambio de firewall o de la VPC, Direct VPC egress de Cloud Run mal configurado en una revisión nueva, AUTH rotado sin actualizar el secreto. `redis_up` sigue en 1 y solo salta `trigger="app_errors"`.
4. **Errores parciales**: memoria llena (`OOM command not allowed`), conexiones en un primario que ya es réplica (`READONLY`), `maxclients` alcanzado. Salta primero la warning.

---

## Contexto

**Qué impacto tiene (en lenguaje de negocio):**
- **Catálogo más lento, pero sirviendo.** El adapter tiene un circuit breaker hacia Redis (timeout de **50 ms**; se abre con > 50 % de fallos en 10 s y reintenta cada 30 s). Con el breaker abierto el catálogo lee directamente de Postgres (read-through sin caché, con single-flight por clave). El p95 de `GET /api/products` pasa de ~35 ms a ~150–220 ms (ADR-005): se incumple NFR-PERF-001, pero no hay errores.
- **El riesgo real es la estampida contra Postgres.** Sin Redis, el primario recibe ~6–10× más lecturas del catálogo. En hora punta eso satura la CPU de Cloud SQL y el pool de `oms-catalog` (→ [runbook 01](../01-db-pool-saturated/runbook.md)). Y como el primario es compartido, **lo nota también el checkout** (SLO 1 de Orders). Por eso la Acción 1 activa el bypass con rate limit propio.
- **Carrito.** El carrito vive en Redis (`cart:{user_ref}`, TTL 7 días) y la SPA guarda **siempre una copia en `localStorage`** (desde PM #1). Con Redis caído, `PUT /api/cart` responde `202 {"mode":"local"}` y la SPA sigue trabajando con su copia local. `POST /api/orders` recibe las líneas del carrito en el cuerpo y revalida precio y stock contra Postgres, así que **se puede comprar**. Lo que se pierde: la sincronización entre dispositivos durante el incidente y, si Redis vuelve vacío, el carrito de quien no vuelva con el mismo navegador.
- **Budget:** NFR-AVAIL-002 (catálogo 99,95 %, ≈ 22 min/mes). Con la degradación funcionando no se consume. Si la estampida provoca 5xx, cada minuto de catálogo caído es el **4,5 %** del budget mensual.

**Qué NO afecta:**
- **Sesión y login**: la sesión es un JWT en el cliente, no vive en Redis.
- **Pedidos, pagos, facturas, outbox**: `oms-orders` y `oms-outbox-relay` no usan Redis (ADR-005 §4). Solo les afecta indirectamente si Postgres se satura.
- **Datos**: Redis no es fuente de verdad de nada. Perder su contenido no pierde pedidos ni precios.
- **Búsqueda** (Elasticsearch) y **SPA** (Cloud CDN): no pasan por Redis.

**Pre-requisitos para diagnosticar:**
- Grupo `oms-oncall@` con `roles/redis.viewer` (describe, operaciones), `roles/redis.admin` (failover, AUTH string y recreación), `roles/iap.tunnelResourceAccessor` (bastión), `roles/run.developer` y `roles/logging.viewer` sobre `oms-production`.
- Dashboard [`oms-cache`](https://grafana.example.com/d/oms-cache) (paneles "Redis up / breaker", "Resultado de cache_requests", "Postgres desde el catálogo") y [`oms-db-overview`](https://grafana.example.com/d/oms-db-overview).
- **Setup de sesión** ejecutado (ver [`../README.md`](../README.md#setup-de-sesión-del-on-call-copiar-una-vez-al-empezar-el-incidente)). Además, para este runbook:

```bash
# Ejecuta comandos en el bastión (allí está oms-redis-cli: redis-cli con host, puerto 6378,
# TLS con la CA de Memorystore y AUTH leído de Secret Manager con la SA del bastión)
bastion() { gcloud compute ssh oms-ops-bastion --zone=europe-west3-a --tunnel-through-iap --command "$1"; }

# Rollout parcial de un flag (estrategia flexibleRollout ya creada en Unleash para catalog.cache-bypass)
flag_rollout() {
  local sid; sid=$(curl -sf "$UNLEASH_URL/api/admin/projects/oms/features/$1/environments/production/strategies" \
      -H "Authorization: $UNLEASH_TOKEN" | jq -r '.[] | select(.name=="flexibleRollout") | .id' | head -1)
  curl -sf -X PUT "$UNLEASH_URL/api/admin/projects/oms/features/$1/environments/production/strategies/$sid" \
    -H "Authorization: $UNLEASH_TOKEN" -H "Content-Type: application/json" \
    -d "{\"name\":\"flexibleRollout\",\"parameters\":{\"rollout\":\"$2\",\"stickiness\":\"random\",\"groupId\":\"$1\"}}" \
    && echo "flag $1 rollout = $2 %"; }
```

> **Atajo V3:** el automatizador `oms-ops-flag-automator` recibe esta alerta por webhook de Alertmanager y ejecuta solo las Acciones 1 y 2a en < 90 s. Mira primero en `#oms-alerts` el mensaje *"flag-automator: catalog.cache-bypass=ON, cart.local-only=ON"*. Si está, **tu trabajo es verificar y diagnosticar la causa**, no repetir las acciones (son idempotentes, pero no hace falta).

---

## Diagnóstico (en orden, ejecuta y mira)

> Los comandos están **copiables**. No los reescribas a las 3 AM.

### 0. ¿Es real, y está funcionando la degradación? (30 s)

```bash
promq 'max(redis_up{instance_id="oms-redis-prod"})'
promq 'sum by (cache, result) (rate(cache_requests_total{job="oms-catalog"}[2m]))'
promq 'max by (job) (circuit_breaker_state{breaker="redis"})'          # 0 cerrado · 1 semiabierto · 2 abierto
promq 'sum(rate(http_requests_total{job="oms-catalog", code=~"5.."}[5m])) / sum(rate(http_requests_total{job="oms-catalog"}[5m]))'
flag_get catalog.cache-bypass; flag_get cart.local-only
```

**Si `redis_up` = 0 y el breaker = 2:** Redis está caído y la app ya se ha protegido. Ve al paso 1 (¿qué le pasa a Memorystore?) y al paso 4 (¿aguanta Postgres?).
**Si `redis_up` = 1 pero `result="error"` domina:** Memorystore está vivo, pero la app no llega o recibe errores. Ve al paso 2 y al 3.
**Si el breaker oscila entre 1 y 2:** Redis responde a ratos (failover en curso o errores parciales). Ejecuta ya la **Acción 1**: el bypass para el flapping del breaker.
**Si el 5xx de `oms-catalog` es > 1 %:** la degradación no está bastando. Anuncia en `#incident-response`: *"Investigando RedisCacheDown, catálogo con errores"*, y salta a la **Acción 1** antes de seguir diagnosticando.

### 1. ¿Qué dice Memorystore? ¿Hay una operación o mantenimiento en curso?

```bash
gcloud redis instances describe oms-redis-prod --region=$REGION \
  --format="yaml(state,statusMessage,tier,memorySizeGb,locationId,currentLocationId,alternativeLocationId,host,port,maintenanceSchedule)"
gcloud redis operations list --region=$REGION --filter="metadata.target~oms-redis-prod" \
  --sort-by=~metadata.createTime --limit=5 --format="table(name.basename(),metadata.verb,metadata.createTime,done)"
gcloud logging read 'resource.type="redis_instance" AND resource.labels.instance_id:"oms-redis-prod"' \
  --freshness=2h --limit=20 --format="table(timestamp,severity,protoPayload.methodName,jsonPayload.message)"
```

**Anota `currentLocationId`** (zona del primario ahora mismo): lo necesitas en la Acción 3.
**Si `state` = `FAILING_OVER`, `MAINTENANCE` o `REPAIRING`, o hay una operación con `done=False`:** Google está trabajando en la instancia. **No hagas failover manual** (fallaría o lo alargaría). Mantén las Acciones 1–2 y espera; revisa cada 5 min. Si `REPAIRING` supera 20 min, abre caso de soporte P1 con Google Cloud y escala al EM.
**Si `maintenanceSchedule.startTime` coincide con la hora del incidente:** es mantenimiento programado. Verás un failover de < 1 min. Mantén las Acciones 1–2 hasta que el paso 2 dé `PONG` estable.
**Si `state` = `READY` pero la app falla:** el problema no es Memorystore. Ve al paso 2 y al 3.

### 2. ¿Responde Redis desde la red de producción? (desde el bastión, misma VPC que Cloud Run)

```bash
bastion 'oms-redis-cli PING'
bastion 'oms-redis-cli INFO replication | grep -E "^role|connected_slaves|master_link_status"'
bastion 'oms-redis-cli INFO clients | grep -E "connected_clients|blocked_clients|maxclients"'
bastion 'oms-redis-cli INFO memory | grep -E "used_memory_human|maxmemory_human|maxmemory_policy"'
bastion 'oms-redis-cli INFO persistence | grep -E "loading"'
```

| Salida | Significa | Siguiente |
|---|---|---|
| `PONG` | Redis está sano desde la VPC: el fallo está en el camino Cloud Run → Redis | Paso 3 |
| `Could not connect` / timeout | La instancia no acepta conexiones (failover, reparación, red de la VPC) | Paso 1 y Acción 3 si `state=READY` |
| `LOADING Redis is loading the dataset` | Acaba de reiniciar y está cargando | Espera; mantén Acciones 1–2 |
| `NOAUTH` / `WRONGPASS` | AUTH rotado: el wrapper del bastión ya lee el nuevo, la app puede que no | Paso 3 (`error_code`) |
| `role:master` y `connected_slaves:0` | Funciona, pero **sin réplica**: otro fallo = caída total | Sigue; anota para el post-mortem |
| `used_memory` ≈ `maxmemory` | Memoria llena; con `volatile-lru` habrá evictions, y si las claves no tienen TTL, `OOM` en escrituras | Errores parciales: ver [runbook 08](../08-redis-cache-hit-ratio-low/runbook.md) |
| `connected_clients` ≈ `maxclients` | Fuga de conexiones en la app (cada instancia debe abrir ≤ 4) | Paso 3 |

### 3. ¿Qué error ve la app y desde qué revisión?

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="oms-catalog"
  AND jsonPayload.event="catalog.cache.error"' --freshness=15m --limit=500 \
  --format="value(jsonPayload.error_code,jsonPayload.revision)" | sort | uniq -c | sort -rn | head
gcloud run services describe oms-catalog --region=$REGION --format="yaml(status.traffic)"
gcloud logging read 'logName:"cloudaudit.googleapis.com%2Factivity" AND (protoPayload.methodName:"compute.firewalls"
  OR protoPayload.methodName:"redis.instances" OR protoPayload.serviceName="secretmanager.googleapis.com")' \
  --freshness=3h --limit=20 --format="table(timestamp,protoPayload.methodName,protoPayload.authenticationInfo.principalEmail)"
```

**`ETIMEDOUT` / `ECONNREFUSED` en todas las revisiones** + cambio reciente de firewall en el audit log → red. Revierte ese cambio con su autor (Terraform `infra/`) y mantén la Acción 1.
**Errores solo en la revisión más nueva:** la revisión nueva no llega a Redis (egress de VPC, variable `REDIS_HOST`). Es un problema de despliegue: [runbook 07](../07-canary-rollout-failed/runbook.md), rollback de tráfico.
**`NOAUTH`:** el AUTH string se rotó y Cloud Run tiene la versión anterior del secreto `oms-redis-auth`. Acción 3b.
**`READONLY`:** las conexiones siguen en el nodo que antes era primario. El cliente debería reconectar solo (`reconnectOnError` con `READONLY`); si persiste > 2 min, Acción 3b.
**`OOM`:** memoria llena; lecturas bien, escrituras mal. Aquí **sí** sirve `catalog.serve-stale` (ver Acción 1) y el runbook 08.

### 4. ¿Aguanta Postgres la carga extra? (decide si hace falta la Acción 2)

```bash
promq 'max(cloudsql_cpu_utilization{database_id="oms-production:oms-pg-prod"})'
promq 'max by (job) (db_pool_in_use{job=~"oms-catalog|oms-orders"} / db_pool_size{job=~"oms-catalog|oms-orders"})'
promq 'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="oms-catalog", endpoint=~"/api/products.*"}[5m])))'
promq 'sum(rate(http_requests_total{job="oms-catalog", code="429"}[5m]))'                       # peticiones recortadas por el rate limit del bypass
promq 'max(cloudsql_replication_lag_seconds{replica="oms-pg-prod-replica"})'
```

**CPU < 70 % y pool de `oms-catalog` < 75 %:** Postgres absorbe la carga. No hace falta la Acción 2.
**CPU > 70 % o pool de `oms-catalog` > 75 %:** ejecuta la **Acción 2** (proteger Postgres).
**Pool de `oms-orders` > 75 %:** el catálogo está robando capacidad al checkout. Acción 2 **ya**, y abre en paralelo el [runbook 01](../01-db-pool-saturated/runbook.md).
**429 > 5 % del tráfico del catálogo:** el rate limit del bypass está recortando a usuarios reales. Acción 2b (réplica) para darle más capacidad de lectura.

---

## Acciones (de menor a mayor impacto, idempotentes)

> El objetivo **no es arreglar Redis** (casi siempre se arregla solo o lo arregla Google): es **que Postgres y el checkout no paguen la caída**. Escala solo si la acción anterior no basta en 5 min.

### Acción 1: Bypass controlado de la caché + carrito local (impacto: bajo · reversible en 15 s)

```bash
flag_on catalog.cache-bypass      # deja de llamar a Redis y lee de Postgres con rate limit propio
flag_on cart.local-only           # la SPA trabaja con el carrito de localStorage; la API no intenta escribir en Redis
flag_get catalog.cache-bypass; flag_get cart.local-only
```

**Qué hace:** con el bypass, `oms-catalog` deja de intentar Redis (sin pagar los 50 ms de timeout ni las sondas del breaker semiabierto) y limita las lecturas a Postgres a `CATALOG_DB_FALLBACK_MAX_RPS=25` por instancia, con single-flight por clave. Lo que excede recibe `429` con `Retry-After: 1` y la SPA reintenta una vez. `cart.local-only` hace que la SPA no dependa del `PUT /api/cart`.
**Por qué primero:** el breaker ya protege, pero oscila (abre, sonda, cierra, vuelve a abrir) y **no tiene rate limit**: es el que provocó la estampida de PM #1. El flag tarda ≤ 15 s y no requiere redeploy.
**Idempotente:** sí. Encender un flag que ya está en ON no cambia nada.
**Coste:** catálogo con p95 ~150–220 ms. Los carritos no se sincronizan entre dispositivos.
**Sobre `catalog.serve-stale`: aquí NO sirve si Redis está caído.** Las copias "stale" viven en Redis (cada clave guarda `soft_expires_at` y tiene un TTL físico de TTL lógico + 1 h). Si Redis no responde, no hay nada stale que servir. **Sí** aplica cuando Redis **responde a lecturas** pero falla en escrituras (`OOM`, `READONLY`) o cuando el que falla es Postgres ([runbook 09](../09-postgres-unavailable/runbook.md)). En ese caso: `flag_on catalog.serve-stale` **en lugar de** `catalog.cache-bypass`.
**Cuándo NO:** si el paso 0 muestra `redis_up=1`, 0 errores y el breaker cerrado (la alerta ya se resolvió sola). Entonces no actives nada y ve a Verificación.

### Acción 2: Proteger Postgres (impacto: bajo-medio · reversible en 15 s)

```bash
flag_on orders.batch-jobs-paused          # 2a: fuera exports y reconciliación (no tienen usuario esperando)
flag_on catalog.read-from-replica         # 2b: las lecturas de catálogo en bypass van a oms-pg-prod-replica
```

**Qué hace:** 2a libera CPU y conexiones del primario. 2b desvía las lecturas del catálogo a la réplica de lectura, que tiene sus propias conexiones y CPU: el primario queda para el checkout.
**Por qué en este orden:** 2a no tiene coste visible para el cliente; 2b sube algo la latencia del catálogo (réplica p95 ~180 ms, ADR-005) y puede mostrar datos con ~1 s de retraso.
**Idempotente:** sí, ambos son flags.
**Coste:** exports CSV y reconciliación retrasados (avisa en `#oms-backoffice`). El precio que ve el cliente en el catálogo puede llevar 1 s de retraso; el checkout **siempre** revalida contra el primario.
**Cuándo NO hacer 2b:** si `cloudsql_replication_lag_seconds{replica="oms-pg-prod-replica"}` > 5 s (paso 4).

### Acción 3: Failover manual de Memorystore (impacto: medio · solo con `state=READY` y el primario sin responder)

```bash
# Guarda de idempotencia: un segundo failover devolvería el primario a la zona rota.
ZONA_ANOTADA=<currentLocationId del paso 1>
ZONA_AHORA=$(gcloud redis instances describe oms-redis-prod --region=$REGION --format="value(currentLocationId)")
if [ "$ZONA_AHORA" = "$ZONA_ANOTADA" ]; then
  gcloud redis instances failover oms-redis-prod --region=$REGION --data-protection-mode=limited-data-loss
else
  echo "Ya hubo failover ($ZONA_ANOTADA -> $ZONA_AHORA): no repetir"
fi
```

**Qué hace:** promociona la réplica de la otra zona. `limited-data-loss` solo procede si la réplica está casi al día; si Memorystore lo rechaza por lag, **no** uses `force-data-loss` sin aprobación (se perderían carritos recientes; la SPA los resincroniza, pero solo en el mismo navegador).
**Cuándo:** `state=READY`, sin operaciones en curso (paso 1), `PING` falla o da timeout desde el bastión (paso 2), y han pasado > 5 min. Típico de un nodo primario degradado que Google aún no ha detectado.
**Cuándo NO:** con `FAILING_OVER`/`MAINTENANCE`/`REPAIRING` o con una operación `done=False`; si el problema es de red o de AUTH (paso 3), porque el failover no lo arregla.
**Idempotente:** sí, gracias a la guarda de zona.
**Coste:** 30–60 s más de conexiones cortadas (la Acción 1 ya lo absorbe). Avisa en `#incident-response` antes de lanzarlo.

### Acción 3b: Reconectar la app sin redeploy de código (impacto: medio · para `NOAUTH`/`READONLY` persistente)

```bash
# Fuerza una revisión nueva con la última versión del secreto y conexiones nuevas (mismo código, misma imagen)
gcloud run services update oms-catalog --region=$REGION --update-secrets=REDIS_AUTH=oms-redis-auth:latest
```

**Qué hace:** crea una revisión idéntica que arranca con conexiones nuevas a Redis y el AUTH vigente. Cloud Run mantiene la anterior sirviendo hasta que la nueva está lista. **Esto no es "reiniciar porque sí"**: solo procede cuando el paso 3 dio `NOAUTH`, o `READONLY` persistente, y con la Acción 1 ya activa (si no, las instancias nuevas arrancan con caché fría y sin rate limit).
**Idempotente:** sí. Si el secreto no ha cambiado, la revisión es equivalente.

### Acción 4: Recrear la instancia (impacto: alto · último recurso, requiere aprobación del EM)

Solo si Memorystore lleva > 30 min sin servicio, Google no da plazo en el caso de soporte y la degradación está costando dinero (CPU de Cloud SQL > 85 % sostenida a pesar de la Acción 2).

```bash
gcloud redis instances create oms-redis-prod-b --region=$REGION --tier=STANDARD_HA --size=8 \
  --redis-version=redis_7_2 --zone=europe-west3-a --alternative-zone=europe-west3-b \
  --network=projects/oms-production/global/networks/oms-vpc --connect-mode=PRIVATE_SERVICE_ACCESS \
  --enable-auth --transit-encryption-mode=SERVER_AUTHENTICATION \
  --maintenance-window-day=TUESDAY --maintenance-window-hour=2
NEW_HOST=$(gcloud redis instances describe oms-redis-prod-b --region=$REGION --format="value(host)")
gcloud redis instances get-auth-string oms-redis-prod-b --region=$REGION --format="value(authString)" \
  | gcloud secrets versions add oms-redis-auth --data-file=-
gcloud redis instances describe oms-redis-prod-b --region=$REGION --format="value(serverCaCerts[0].cert)" \
  | gcloud secrets versions add oms-redis-ca --data-file=-
gcloud run services update oms-catalog --region=$REGION --update-env-vars=REDIS_HOST=$NEW_HOST \
  --update-secrets=REDIS_AUTH=oms-redis-auth:latest,REDIS_CA_CERT=oms-redis-ca:latest
```

**Qué hace:** crea una instancia nueva en europe-west3 (REG-GDPR-001), con la misma configuración, y apunta `oms-catalog` a ella. Tarda ~10 min en crearse. **Mantén el bypass en ON** hasta hacer el warm-up (ver Verificación).
**Coste:** se pierden todos los carritos del servidor: la SPA los vuelve a subir desde `localStorage` en la siguiente visita; se pierden los de quien cambie de dispositivo. Deriva de Terraform: hay que importar la instancia nueva (`terraform import`) el siguiente día laborable.
**Idempotente:** sí. Si `oms-redis-prod-b` ya existe, `create` falla con `ALREADY_EXISTS` sin tocar nada, y el resto de comandos repite los mismos valores.
**Quién aprueba:** Engineering Manager de guardia (PagerDuty escalation level 2).

---

## Verificación

**1. Redis de vuelta (antes de quitar ningún flag):**
```promql
max(redis_up{instance_id="oms-redis-prod"}) == 1                    # 10 min seguidos
```
y `bastion 'oms-redis-cli PING'` responde `PONG` tres veces seguidas, con 1 min entre cada una.

**2. Warm-up controlado (NO apagues el bypass de golpe):** con la caché vacía, apagar el bypass en todas las instancias a la vez convierte el 100 % de las lecturas en misses: es la estampida de PM #2.

```bash
gcloud run jobs execute oms-catalog-cache-warmer --region=$REGION --args="--top=2000" --wait   # top listings, categorías y productos
flag_rollout catalog.cache-bypass 50     # espera 5 min y comprueba CPU de Cloud SQL < 70 % y p95 < 150 ms
flag_rollout catalog.cache-bypass 25     # espera 5 min y vuelve a comprobar
flag_off catalog.cache-bypass && flag_rollout catalog.cache-bypass 100   # OFF y deja la estrategia lista para la próxima vez
flag_off cart.local-only                 # la SPA resincroniza su carrito local en el siguiente PUT /api/cart
```
Si en un escalón la CPU de Cloud SQL supera el 70 %, vuelve al escalón anterior y espera 10 min.

**3. Métricas finales:**
```promql
# Errores de Redis por debajo del 0,1 %
sum(rate(cache_requests_total{job="oms-catalog", result="error"}[5m])) / sum(rate(cache_requests_total{job="oms-catalog"}[5m])) < 0.001
# Hit ratio recuperándose (> 80 % a los 15 min del warm-up; > 90 % es la base)
sum(rate(cache_requests_total{job="oms-catalog", cache="catalog", result="hit"}[5m]))
  / sum(rate(cache_requests_total{job="oms-catalog", cache="catalog", result=~"hit|miss"}[5m])) > 0.8
# Catálogo dentro de NFR-PERF-001 y sin errores
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="oms-catalog", endpoint=~"/api/products.*"}[5m]))) < 0.15
sum(rate(http_requests_total{job="oms-catalog", code=~"5.."}[5m])) / sum(rate(http_requests_total{job="oms-catalog"}[5m])) < 0.001
```

**Ventana de observación:** **15 minutos** con todo en verde **después** de apagar el último flag.

**Cómo confirmas:**
1. Paneles "Redis up / breaker" y "Postgres desde el catálogo" en [`oms-cache`](https://grafana.example.com/d/oms-cache).
2. `RedisCacheDown` y `RedisCacheErrorsElevated` en *resolved* en Alertmanager.
3. Comunica en `#incident-response`: *"Redis recuperado: redis_up=1 desde HH:MM, hit ratio XX %, p95 catálogo XX ms, 0 % 5xx. Warm-up hecho. Flags: cache-bypass OFF, cart.local-only OFF, [otros]. Cerrando incidente."*

---

## Rollback

| Acción | Rollback | Cuándo |
|---|---|---|
| 1 (bypass + carrito local) | Escalonado como en Verificación §2: `flag_rollout ... 50 → 25 → flag_off`, después `flag_off cart.local-only` | Solo con Redis estable 10 min. **Nunca** de golpe. |
| 1 (si usaste `serve-stale`) | `flag_off catalog.serve-stale` | Cuando Redis acepte escrituras y el hit ratio pase del 80 %. |
| 2a (batch pausado) | `flag_off orders.batch-jobs-paused` | Con el pool del primario < 70 % durante 30 min. |
| 2b (réplica) | `flag_off catalog.read-from-replica` | En cuanto el bypass esté en OFF, o si el lag de la réplica pasa de 5 s. |
| 3 (failover) | No se deshace. El primario queda en `alternativeLocationId`; si se quiere volver a la zona original, se hace **en horario laboral** con un failover planificado. | — |
| 3b (revisión nueva) | `gcloud run services update-traffic oms-catalog --region=$REGION --to-revisions=<REVISION_ANTERIOR>=100` | Si la revisión nueva da errores que la anterior no daba. |
| 4 (instancia nueva) | Volver a apuntar a `oms-redis-prod` cuando Google la repare (mismo `services update` con su host y secretos) y borrar `oms-redis-prod-b` tras 7 días estable. | Con aprobación del EM y en horario laboral. |

---

## Después del incidente

- [ ] Anota en el ticket: duración (T_alerta → T_verificado), causa (failover / mantenimiento / reparación / red / AUTH / memoria) y qué acciones hicieron falta.
- [ ] Comprueba **todos** los flags tocados con `flag_get`. `CatalogCacheBypassLeftOn` te avisará a las 2 h, pero no esperes a eso.
- [ ] Si fue mantenimiento de Google: ¿cayó dentro de la ventana (martes 02:00 UTC)? Si no, abre caso con soporte. Si hay un pico de negocio previsto, reprograma con `gcloud redis instances reschedule-maintenance oms-redis-prod --region=$REGION --reschedule-type=SPECIFIC_TIME --schedule-time=<ISO8601>`.
- [ ] Si fue red o AUTH: el cambio que lo provocó tiene que pasar por Terraform con plan revisado. Abre ticket al autor.
- [ ] Si llegaste a la Acción 3 o 4: post-mortem en < 48 h.
- [ ] Si `oms-ops-flag-automator` no actuó (no hay mensaje en `#oms-alerts`): es un incidente aparte de la automatización; revisa `FlagAutomatorStale`.
- [ ] Si algún paso no sirvió, actualiza este runbook en el mismo PR del post-mortem.

---

## Enlaces relacionados

- Alerta YAML: [`alert.yaml`](alert.yaml) · Post-mortems: [`post-mortems.md`](post-mortems.md)
- Decisión de arquitectura: [`ADR-005`](../../../anexo-arquitectura/05-adrs/ADR-005-redis-cache-catalogo.md) (read-through, TTL, degradación)
- Disponibilidad del catálogo (NFR-AVAIL-002) y el budget del checkout que se protege: [`../../slos/slos-orders.md`](../../slos/slos-orders.md) (SLO 1) · [`../../slos/error-budget-policy.md`](../../slos/error-budget-policy.md)
- Instrumentación: [`../../observability/instrumentation.md`](../../observability/instrumentation.md) (Memorystore Redis)
- Dashboard: <https://grafana.example.com/d/oms-cache>
- Runbooks relacionados: [`08-redis-cache-hit-ratio-low`](../08-redis-cache-hit-ratio-low/runbook.md) (Redis vivo pero sin aciertos), [`01-db-pool-saturated`](../01-db-pool-saturated/runbook.md) (la estampida llega al pool), [`09-postgres-unavailable`](../09-postgres-unavailable/runbook.md) (cuando lo que falla es Postgres y sí aplica `serve-stale`), [`11-catalog-unavailable`](../11-catalog-unavailable/runbook.md) (triage end-to-end del catálogo), [`07-canary-rollout-failed`](../07-canary-rollout-failed/runbook.md) (si solo falla la revisión nueva)
