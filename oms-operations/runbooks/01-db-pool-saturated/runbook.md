# Runbook: DB Connection Pool Saturado (Cloud SQL Postgres)

> **OBLIGATORIO.** Es el ejemplo canónico de "incidente recurrente con causa identificable y mitigación reversible".
> **Versión:** V3 (ver `post-mortems.md`). **Owner:** equipo SRE. **Última revisión:** 2026-05-11 (tras PM #3). **Validación pendiente:** GameDay #1, 25/11/2026.

---

## Trigger

**Alerta:** `DBConnectionPoolSaturated` (severity: **critical**, PagerDuty)
**Condición:** `max by (job, instance) (db_pool_in_use / db_pool_size) > 0.9` durante **3 minutos** consecutivos en cualquier instancia de `oms-orders` u `oms-catalog`.
**Alerta temprana asociada:** `DBConnectionPoolPressure` (severity: warning, Slack `#oms-alerts`): ratio > 0,75 durante 10 min, o peticiones esperando conexión (`db_pool_pending_acquires > 0`) durante 5 min.

**Cuándo dispara (patrones conocidos):**
1. **Pico de tráfico** (campaña de email del lunes 09:00, Black Friday ×5): todas las instancias suben a la vez y el pool se reparte entre todas las apps.
2. **Sesiones zombi**: conexiones `idle` o `idle in transaction` que la app no devuelve (bug OMS-1234, ver post-mortems).
3. **Job batch mal dimensionado**: export CSV (FR-038) o reconciliación nocturna, que abren conexiones en paralelo sin límite. Un único `application_name` domina el pool.
4. **Query lenta o lock**: las conexiones se ocupan más tiempo de lo normal porque esperan a un lock (típico en `inventory.stock_items`).

> **Límite físico:** Cloud SQL `oms-pg-prod` (4 vCPU / 16 GB) tiene `max_connections = 400`, con 20 reservadas para superusuario y operación.
> Cada instancia de Cloud Run abre un pool de `DB_POOL_SIZE` (por defecto 10 en `oms-orders` y 5 en `oms-catalog`).
> **Fórmula de capacidad:** `max-instances × DB_POOL_SIZE` de todos los servicios tiene que quedar **< 380**. Hoy: `oms-orders` 20×10 + `oms-catalog` 20×5 + `oms-outbox-relay` 2×5 = **310**.

---

## Contexto

**Qué impacto tiene:**
Cuando el pool de una instancia llega al 100 %, las peticiones que necesitan BD se quedan esperando una conexión hasta `DB_POOL_ACQUIRE_TIMEOUT` (2 s) y después fallan con **503**. Para el cliente:
- **Checkout**: "no hemos podido crear tu pedido, inténtalo de nuevo". Es pérdida directa de ingreso y consume el budget del SLO 1 de Orders (99,9 %, 40 min/28 d): **2 minutos con el 20 % de errores ya gastan el ~1 % del budget mensual**.
- **"Mis pedidos"** (`GET /api/orders`): lento o con error.
- **Backoffice** (Madrid y Barcelona): no puede gestionar pedidos.

**Qué NO afecta (todavía):**
- **Navegación del catálogo**: se sirve desde Redis (`oms-redis-prod`, ADR-005). Mientras haya hit (> 90 % normalmente), el catálogo no toca la BD. Si dura más que el TTL lógico (ADR-005: 5 min en listados, 30 s en ficha de producto) y empiezan los misses, el catálogo se degrada también. En ese caso se activa `catalog.serve-stale` (Acción 4), que sirve la copia en Redis hasta 1 h después de caducar.
- **Stripe y los pagos ya capturados**: no se pierden. Si el pedido falla después de autorizar, la autorización caduca sola en Stripe a los 7 días sin cobrar.
- **Outbox**: los eventos ya escritos no se pierden. El relay va más lento, pero recupera cuando vuelve el pool (es at-least-once, ADR-009).
- **Datos**: no hay riesgo de corrupción. Las transacciones que fallan hacen rollback completo (ACID).

**Pre-requisitos para diagnosticar:**
- Rol IAM `roles/cloudsql.client` + `roles/iap.tunnelResourceAccessor` (bastión) + `roles/run.developer` sobre `oms-production` (grupo `oms-oncall@`).
- Secretos `oms-db-ops-password` y `oms-unleash-admin-token` en Secret Manager (lectura para `oms-oncall@`).
- Dashboard Grafana [`oms-db-overview`](https://grafana.example.com/d/oms-db-overview), panel "Pool usage por instancia".
- **Setup de sesión** ejecutado (ver [`../README.md`](../README.md#setup-de-sesión-del-on-call-copiar-una-vez-al-empezar-el-incidente)): define `promq`, `flag_on`, `flag_off` y el túnel `psql`.

> **Atajo V3:** `../../scripts/db-pool-saturated.sh --dry-run` ejecuta los pasos de diagnóstico 1–3 y te dice qué acción toca. Sin `--dry-run` ejecuta solo la **Acción 1**. Las demás acciones siguen siendo manuales y con criterio humano.

---

## Diagnóstico (en orden, ejecuta y mira)

> Los comandos están **copiables**. No los reescribas a las 3 AM.

### 0. ¿Es real y en cuántas instancias? (30 s)

```bash
promq 'max by (job, instance) (db_pool_in_use / db_pool_size)' | sort -k2 -nr | head
promq 'sum by (job) (db_pool_pending_acquires)'
promq 'sum(rate(http_requests_total{job="oms-orders", code=~"5.."}[5m])) / sum(rate(http_requests_total{job="oms-orders"}[5m]))'
```

**Si solo 1–2 instancias están saturadas y el resto < 50 %:** el problema es local a esas instancias (una petición larga o un worker atascado). Ve al paso 3 y a la Acción 1.
**Si todas están > 80 % y el error rate de `oms-orders` es > 1 %:** hay impacto en el usuario. Anuncia en `#incident-response`: *"Investigando DBConnectionPoolSaturated, impacto en checkout"*, y sigue con el paso 1.

### 1. ¿Quién está consumiendo las conexiones de Postgres ahora mismo?

```bash
psql -X -c "
  SELECT coalesce(nullif(application_name,''),'(sin nombre)') AS app,
         state,
         count(*) AS conexiones,
         round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
  FROM pg_stat_activity
  WHERE datname = 'oms' AND backend_type = 'client backend'
  GROUP BY 1, 2
  ORDER BY conexiones DESC;"
```

**Si una sola `app` acumula > 70 % de las conexiones y no es `oms-orders`** (por ejemplo `oms-batch-export` o `oms-reconcile`): un job batch se está comiendo el pool. Ve a la **Acción 2**.
**Si domina `state = idle` o `idle in transaction`:** son sesiones zombi. Ve al paso 2.
**Si está repartido entre apps y casi todo `active`:** es capacidad (pico de tráfico). Ve al paso 3 para descartar locks y luego a la **Acción 3**.

### 2. ¿Hay conexiones idle largas (zombis)?

```bash
psql -X -c "
  SELECT application_name, state, count(*) AS n,
         max(now() - state_change) AS max_idle
  FROM pg_stat_activity
  WHERE datname = 'oms'
    AND state IN ('idle', 'idle in transaction', 'idle in transaction (aborted)')
    AND state_change < now() - interval '10 minutes'
  GROUP BY 1, 2
  ORDER BY n DESC;"
```

**Si hay > 20 conexiones idle con `max_idle` > 10 min:** ejecuta la **Acción 1**. Es el patrón del bug OMS-1234.
**Si hay `idle in transaction` > 1 min:** son aún más graves, porque **retienen locks**. Entran también en la Acción 1.
**Si no hay ninguna:** los zombis no son la causa. Pasa al paso 3.

### 3. ¿Hay alguna query bloqueando a otras?

```bash
psql -X -c "
  SELECT blocked.pid                 AS blocked_pid,
         left(blocked.query, 60)     AS blocked_query,
         blocking.pid                AS blocking_pid,
         blocking.application_name   AS blocking_app,
         blocking.state              AS blocking_state,
         now() - blocking.xact_start AS blocking_xact_age,
         left(blocking.query, 80)    AS blocking_query
  FROM pg_stat_activity blocked
  JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS b(pid) ON true
  JOIN pg_stat_activity blocking ON blocking.pid = b.pid
  WHERE blocked.datname = 'oms'
  ORDER BY blocking_xact_age DESC
  LIMIT 20;"
```

**Si aparece un mismo `blocking_pid` bloqueando a ≥ 5 sesiones con `blocking_xact_age` > 2 min:** ese es el tapón. Ejecuta la **Acción 1b** (terminar el bloqueante). Antes, anota en el ticket `blocking_query` y `blocking_app`: es la pista para el post-mortem.
**Si la tabla está vacía:** no hay locks. El pool se llena por volumen; ve a la **Acción 3**.

### 4. ¿Tiene margen Cloud SQL para aceptar más conexiones?

```bash
psql -X -At -c "SELECT count(*) || ' / ' || current_setting('max_connections') FROM pg_stat_activity;"
promq 'max(cloudsql_cpu_utilization{database_id="oms-production:oms-pg-prod"})'
```

**Si las conexiones totales son < 300/400 y la CPU de Cloud SQL < 70 %:** la BD tiene margen. Ampliar el pool de la app (Acción 3) es seguro.
**Si la CPU de Cloud SQL > 85 %:** **NO amplíes el pool**. Más conexiones contra una BD saturada solo meten más cola y empeoran la latencia de todos. Haz las Acciones 2 y 4 y escala el incidente (ver Acción 4).

---

## Acciones (de menor a mayor impacto, idempotentes)

> Empieza por la acción de menor coste y riesgo. Solo escalas si la anterior no resolvió en 5 minutos.

### Acción 1: Terminar conexiones idle > 10 min (impacto: bajo · reversible por sí sola)

```bash
psql -X -c "
  SELECT pid, application_name, state, now() - state_change AS idle_for,
         pg_terminate_backend(pid) AS terminated
  FROM pg_stat_activity
  WHERE datname = 'oms'
    AND state IN ('idle', 'idle in transaction', 'idle in transaction (aborted)')
    AND state_change < now() - interval '10 minutes'
    AND usename = 'oms_app'                 -- nunca toca sesiones de oms_ops, replicación ni cloudsqladmin
    AND pid <> pg_backend_pid();"
```

**Qué hace:** termina solo las sesiones de la app que llevan más de 10 minutos sin hacer nada y libera esos huecos en Postgres y en el pool de la app. El pool detecta la conexión rota y abre otra al pedirla.
**Por qué primero:** no requiere redeploy, no afecta a ninguna petición en curso (solo toca sesiones inactivas) y tarda < 1 s.
**Idempotente:** sí. La segunda ejecución no encuentra sesiones que cumplan el filtro y devuelve 0 filas.
**Cuándo NO ejecutarla:** si el paso 2 devolvió 0 filas (no tiene efecto), o si ya se ejecutó hace < 5 min y el pool sigue > 90 %. En ese caso salta a la Acción 2 o 3.

### Acción 1b: Terminar la sesión bloqueante (impacto: bajo-medio · solo si el paso 3 encontró un tapón)

```bash
# Sustituye <BLOCKING_PID> por el pid del paso 3. Primero cancela la query (suave), después termina (fuerte).
psql -X -c "SELECT pg_cancel_backend(<BLOCKING_PID>);"
sleep 10
psql -X -c "SELECT pid, state, pg_terminate_backend(pid) FROM pg_stat_activity
            WHERE pid = <BLOCKING_PID> AND now() - xact_start > interval '2 minutes';"
```

**Qué hace:** cancela la query que tiene el lock y, si la transacción sigue abierta 10 s después, la termina. Su transacción hace rollback y las sesiones bloqueadas continúan.
**Coste:** la operación de esa sesión falla y hace rollback. Si era un pedido, el cliente ve un error y reintenta. Si era un job, se reanuda en su siguiente ejecución.
**Cuándo NO ejecutarla:** si `blocking_app` es `cloudsqladmin` o la sesión es un `VACUUM`/`CREATE INDEX CONCURRENTLY` lanzado por un humano. En ese caso, pregunta en `#oms-sre` quién lo lanzó antes de tocarlo.

### Acción 2: Pausar los jobs batch con feature flag (impacto: bajo · reversible en 15 s)

```bash
flag_on orders.batch-jobs-paused
flag_get orders.batch-jobs-paused
# Si el job ya estaba en marcha, el flag impide que tome lotes nuevos (lo comprueba en cada lote, ≤ 15 s).
# Para cortar en seco un Cloud Run Job en ejecución:
gcloud run jobs executions list --job=oms-batch-export --region=$REGION --filter="status.completionTime=null" --format="value(name)" \
  | xargs -r -I{} gcloud run jobs executions cancel {} --region=$REGION --quiet
```

**Qué hace:** los jobs `oms-batch-export` (FR-038) y `oms-reconcile` comprueban el flag antes de cada lote. Con el flag en ON terminan el lote en curso y se paran limpiamente. Cancelar la ejecución es el corte inmediato.
**Por qué antes que escalar el pool:** el batch no tiene usuario esperando, el checkout sí. Es una cuestión de prioridad, no de capacidad.
**Coste:** los exports CSV pendientes de usuarios (derecho de acceso GDPR, plazo legal de 30 días) se retrasan unas horas. No se pierden: quedan en la cola `export_requests`.
**Comunicación:** avisa en `#oms-backoffice` de que el export y la reconciliación quedan pausados.
**Cuándo NO:** si el paso 1 no mostró ninguna app batch entre los consumidores, este flag no cambia nada.

### Acción 3: Mover lecturas a la réplica y, si no basta, ampliar el pool (impacto: medio · la 3b requiere rolling update)

**3a. Feature flag (sin redeploy):**
```bash
flag_on orders.read-from-replica
```
Desvía `GET /api/orders*` y el backoffice a la réplica de lectura (`oms-pg-prod-replica`), que tiene **su propio** límite de conexiones. Con el perfil de tráfico típico libera ~40 % del pool del primario.
**Coste:** las lecturas pueden ver datos con hasta ~1 s de retraso (un pedido recién creado puede tardar 1 s en aparecer en "Mis pedidos"). Es aceptable. Comprueba el lag antes: `promq 'cloudsql_replication_lag_seconds{replica="oms-pg-prod-replica"}'` tiene que ser < 5 s.

**3b. Ampliar el pool (solo si el paso 4 confirmó margen en Cloud SQL):**
```bash
# Mira primero el valor actual (es lo que necesitarás para el rollback)
gcloud run services describe oms-orders --region=$REGION \
  --format="value(spec.template.spec.containers[0].env)" | tr ';' '\n' | grep DB_POOL_SIZE

# Sube de 10 a 15 (20 × 15 = 300 + 110 del resto = 410 > 380, así que bajamos el techo de instancias a la vez)
gcloud run services update oms-orders --region=$REGION \
  --update-env-vars=DB_POOL_SIZE=15 \
  --max-instances=17
```
**Qué hace:** cada instancia admite más conexiones concurrentes. El techo de instancias se reajusta para que la suma (17 × 15 + 110 = 365) siga por debajo de 380. **Nunca subas `DB_POOL_SIZE` sin recalcular la fórmula de capacidad.**
**Idempotente:** sí. Repetir el mismo `update` con los mismos valores no crea una revisión nueva.
**Coste:** rolling update de ~60 s. Cloud Run mantiene la revisión anterior sirviendo hasta que la nueva está lista, así que no hay downtime. **Desplegar fuera de ventana (OPS-002) está permitido para mitigar un incidente**, pero hay que anotarlo en el ticket.

### Acción 4: Proteger el checkout recortando el resto (impacto: alto · último recurso, requiere aprobación)

Si después de las Acciones 1–3 el pool sigue > 90 % y la CPU de Cloud SQL es > 85 %, la BD está al límite. Se prioriza crear pedidos sobre todo lo demás:

```bash
flag_on catalog.serve-stale                     # catálogo 100 % desde Redis, aunque el TTL haya expirado
flag_on catalog.search-suggestions-disabled     # quita la parte cara de la búsqueda
flag_on billing.pdf-generation-paused           # las facturas se generan después (el evento espera en el outbox)
```

**Qué hace:** quita carga de la BD a todo lo que no sea crear o pagar pedidos.
**Coste:** el catálogo puede mostrar precio o stock de hace unos minutos (el checkout **siempre** revalida precio y stock contra la BD, así que no se vende nada a precio incorrecto) y las facturas llegan con retraso.
**Quién aprueba:** Engineering Manager de guardia (PagerDuty escalation level 2). Si no responde en 10 min, el on-call decide y lo documenta.
**Siguiente paso si esto tampoco basta:** escalar Cloud SQL de tier (`gcloud sql instances patch oms-pg-prod --tier=db-custom-8-32768`). Eso reinicia la instancia y provoca **~60–120 s de indisponibilidad** con failover HA, así que **requiere aprobación del EM** y se coordina con el runbook [`09-postgres-unavailable`](../09-postgres-unavailable/runbook.md).

---

## Verificación

> Sin verificación medible, declarar "ya está arreglado" es esperanza, no diagnóstico.

**Métricas que tienen que volver a la normalidad:**
```promql
# 1. Pool por debajo del 70 % en TODAS las instancias
max(db_pool_in_use / db_pool_size) < 0.7

# 2. Nadie esperando conexión
sum(db_pool_pending_acquires) == 0

# 3. Checkout sin errores: ratio de 5xx de POST /api/orders por debajo del 0,1 % (objetivo del SLO)
sum(rate(http_requests_total{job="oms-orders", endpoint="/api/orders", method="POST", code=~"5.."}[5m]))
  / sum(rate(http_requests_total{job="oms-orders", endpoint="/api/orders", method="POST", code!~"4.."}[5m])) < 0.001
```

**Ventana de observación:** **10 minutos consecutivos** con las 3 condiciones en verde.

**Cómo confirmas:**
1. Abre el panel "Pool usage por instancia" en [`oms-db-overview`](https://grafana.example.com/d/oms-db-overview) y el panel "SLO Orders" en [`oms-overview`](https://grafana.example.com/d/oms-overview).
2. Espera los 10 min. Si en ese tiempo el pool vuelve a superar el 85 %, **no cierres**: la causa sigue activa. Vuelve al paso 1 del diagnóstico.
3. Comprueba que la alerta `DBConnectionPoolSaturated` está en estado *resolved* en Alertmanager.
4. Comunica en `#incident-response`: *"DB pool back to normal: max XX % utilization stable for 10 min, 5xx en POST /orders < 0,1 %. Acciones aplicadas: N. Flags activos: [lista]. Cerrando incidente."*

---

## Rollback

> ¿Qué se deshace si la mitigación empeoró las cosas o cuando ya no hace falta?

| Acción | Rollback | Cuándo |
|---|---|---|
| 1 (kill idle) | No hace falta. Las apps reconectan solas al pedir la siguiente conexión. | — |
| 1b (kill bloqueante) | No hace falta. Si era un job, se relanza solo en su siguiente ventana. Si era una migración, **avisa al autor**. | — |
| 2 (pausar batch) | `flag_off orders.batch-jobs-paused` | Cuando el pool lleve 30 min < 70 %, y preferiblemente fuera de horario punta. Los jobs retoman desde su último lote. |
| 3a (réplica) | `flag_off orders.read-from-replica` | Si `cloudsql_replication_lag_seconds` > 5 s (los clientes verían pedidos desactualizados) o tras 24 h estable. |
| 3b (pool ampliado) | `gcloud run services update oms-orders --region=$REGION --update-env-vars=DB_POOL_SIZE=10 --max-instances=20` | Si la CPU de Cloud SQL sube > 85 % tras el cambio (la ampliación está saturando la BD), o en el siguiente día laborable si el pico ya pasó. Coste: otro rolling update de ~60 s. Alternativa más rápida: `gcloud run services update-traffic oms-orders --region=$REGION --to-revisions=<REVISION_ANTERIOR>=100`. |
| 4 (degradación) | `flag_off billing.pdf-generation-paused && flag_off catalog.search-suggestions-disabled && flag_off catalog.serve-stale`, **en este orden** (de lo que menos carga a lo que más). | Uno a uno, con 5 min entre cada uno, mirando el pool. Si al apagar uno el pool vuelve a superar el 85 %, vuelve a encenderlo y deja el resto para el día siguiente. |

---

## Después del incidente

- [ ] Anota en el ticket la duración total (T_alerta → T_verificado) y qué acción fue suficiente (1, 1b, 2, 3 o 4).
- [ ] **Antes de cerrar, deja todos los flags como estaban** (`flag_get` de cada uno). Un flag olvidado en ON es un incidente futuro.
- [ ] Si el paso 1 mostró un job batch: abre un ticket al dueño del job para que use un pool propio limitado (`DB_POOL_SIZE=3`).
- [ ] Si llegaste a la Acción 3b o 4: post-mortem en < 48 h, porque la capacidad base no basta y hay que revisar el capacity planning (NFR-SCAL-001).
- [ ] Si la Acción 1 encontró zombis: suma el recuento a OMS-1234. El cron preventivo (V3) no debería dejar ninguno; si los hay, revisa `ops_job_last_success_timestamp_seconds{job="kill-idle-connections"}`.
- [ ] Si el pool sube de nuevo en < 1 h: **no** cierres el incidente. Escálalo.
- [ ] Si algún paso del runbook no sirvió o faltó: actualiza este documento en el mismo PR del post-mortem.

---

## Enlaces relacionados

- Alerta YAML: [`alert.yaml`](alert.yaml)
- Post-mortems históricos: [`post-mortems.md`](post-mortems.md)
- Script de automatización (V3): [`../../scripts/db-pool-saturated.sh`](../../scripts/db-pool-saturated.sh)
- SLO que protege este runbook: [`../../slos/slos-orders.md`](../../slos/slos-orders.md) (SLO 1, disponibilidad de `POST /api/orders`)
- GameDay que lo valida: [`../../gameday/gameday-plan.md`](../../gameday/gameday-plan.md)
- Dashboard: <https://grafana.example.com/d/oms-db-overview>
- Runbooks relacionados: [`06-cloud-sql-disk-full`](../06-cloud-sql-disk-full/runbook.md) (si el origen es escritura excesiva), [`09-postgres-unavailable`](../09-postgres-unavailable/runbook.md) (si la BD deja de responder), [`02-api-p95-high`](../02-api-p95-high/runbook.md) (si hay latencia sin errores)
