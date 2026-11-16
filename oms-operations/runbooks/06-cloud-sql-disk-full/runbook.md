# Runbook: Disco de Cloud SQL llenándose (`oms-pg-prod`)

> **Prevención > reacción.** Con el disco al 100 %, Postgres deja de aceptar escrituras y el OMS entero deja de vender. Y cada GB que se añade al disco de Cloud SQL **es para siempre**: no se puede reducir. Este runbook busca qué ocupa el espacio **antes** de comprar más.
> **Versión:** V3 (ver `post-mortems.md`). **Owner:** equipo SRE. **Última revisión:** 2026-09-28.

---

## Trigger

**Alerta principal:** `CloudSQLDiskUsageHigh` (severity: **critical**, PagerDuty)
**Condición:** `cloudsql_disk_bytes_used / cloudsql_disk_quota_bytes > 0.90` durante **10 minutos** en `oms-pg-prod`. El auto storage increase reacciona en minutos; si tras 10 min sigue > 90 %, o ha llegado a su límite o el crecimiento le gana.

**Alertas asociadas (mismo runbook):**
| Alerta | Severity | Condición | Qué te dice |
|---|---|---|---|
| `CloudSQLDiskUsageHigh` | warning (Slack) | ratio > 0,80 durante 15 min | El auto-increase está a punto de crecer el disco (coste permanente). Mira por qué en horario laboral. |
| `CloudSQLDiskWillFillIn24h` | warning (Slack + ticket) | `predict_linear(cloudsql_disk_bytes_used[6h], 24*3600)` supera el **límite** del auto-increase durante 30 min | Al ritmo de las últimas 6 h, en < 24 h no queda dónde crecer. La alerta preventiva principal. |
| `CloudSQLDiskAutoIncreaseNearLimit` | warning | el disco aprovisionado ya está al ≥ 90 % del límite del auto-increase | La red de seguridad está casi gastada: el siguiente "lleno" es real. |
| `CloudSQLReplicationSlotInactive` | warning | slot inactivo reteniendo > 5 GB de WAL durante 30 min | La causa de PM #1 vuelve a empezar. |
| `DBHousekeepingJobStale` | warning | el job semanal de limpieza lleva > 8 días sin éxito | La purga programada (V3) no corre. |

**Cuándo dispara (patrones conocidos):**
1. **Slot de replicación huérfano** (PM #1): un slot lógico de un CDC/POC abandonado retiene WAL sin límite. Crece 2–4 GB/día durante semanas.
2. **Ficheros temporales** (PM #2): una query batch con un plan malo (hash join o sort que no cabe en `work_mem`) escribe decenas de GB en minutos. Se liberan solos cuando la query termina.
3. **Crecimiento legítimo sin limpieza**: `outbox.events` publicado sin archivar (el job semanal no corre), bloat por dead tuples que autovacuum no alcanza.
4. **Bug que escribe de más**: el middleware de auditoría registrando cada fila de un listado en `shared.audit_log`. **`audit_log` no se borra** (ver Contexto).

> **Estado de referencia:** disco SSD de **230 GB** (era de 100 GB; subió a 200 GB en PM #1 y a 230 GB en PM #2, y no se puede bajar), auto storage increase **activado** con límite `--storage-auto-increase-limit=300` (GB). Uso normal: ~75 GB (≈ 33 %), crecimiento ~0,3 GB/día.

---

## Contexto

**Qué impacto tiene:**
- **Hasta el 100 %: ninguno visible.** Por eso es fácil ignorarlo, y por eso las alertas son warning salvo la de 90 %.
- **Al 100 %:** Postgres no puede escribir WAL. Fallan **todas las escrituras**: checkout (5xx en `POST /api/orders`, consume el SLO 1 de Orders: a ~100 pedidos/h, 10 min de disco lleno son ~17 fallos, **~25 % del budget de 28 días**), login (IAM escribe en `audit_log`), backoffice, outbox. Si llega a parar la instancia → runbook [`09-postgres-unavailable`](../09-postgres-unavailable/runbook.md).
- **Coste:** cada auto-increase es permanente. SSD en `europe-west3` ≈ 0,22 €/GB/mes: **+100 GB ≈ 22 €/mes para siempre** (~3 % del presupuesto OPS-003). Reducirlo exige migrar a una instancia nueva (dump/restore o Database Migration Service) con ventana de corte.

**Qué NO afecta:**
- **Lecturas**: siguen funcionando aunque el disco esté lleno. El catálogo se sirve desde Redis.
- **Réplicas** (`oms-pg-prod-replica`, `oms-pg-dr`): tienen disco propio, pero si el primario se para, dejan de recibir cambios.
- **Logs del servidor Postgres**: en Cloud SQL van a Cloud Logging y **no ocupan disco de la instancia**. Los backups y los logs de PITR tampoco cuentan para `cloudsql_disk_bytes_used`.

**Qué NO se toca nunca en este runbook:**
- **`shared.audit_log`**: append-only (ADR-010), retención mínima de 3 años (REG-GDPR-003). Ni `DELETE` ni `TRUNCATE`, aunque sea la tabla más grande. Si crece de más, se arregla **lo que escribe** (bug), no lo escrito.
- **`VACUUM FULL`** en producción fuera de ventana: bloquea la tabla entera (`ACCESS EXCLUSIVE`) mientras la reescribe y necesita espacio libre para la copia. Con el disco al 90 % puede ser lo que lo llene.
- **Slots físicos de las réplicas gestionadas** por Cloud SQL.

**Pre-requisitos para diagnosticar:**
- Rol IAM `roles/cloudsql.viewer` (ver configuración) + `roles/cloudsql.client` + `roles/iap.tunnelResourceAccessor` + `roles/run.developer`. **Subir disco o límite (`roles/cloudsql.editor`) solo con aprobación del EM.**
- `oms_ops` tiene `pg_monitor` (para `pg_ls_waldir()`, `pg_ls_tmpdir()`, `pg_replication_slots`).
- Dashboard [`oms-db-overview`](https://grafana.example.com/d/oms-db-overview), paneles "Disco" y "WAL / slots".
- **Setup de sesión** ejecutado (ver [`../README.md`](../README.md#setup-de-sesión-del-on-call-copiar-una-vez-al-empezar-el-incidente)).

---

## Diagnóstico (en orden, ejecuta y mira)

> Los comandos están **copiables**. No los reescribas a las 3 AM.

### 0. ¿Cuánto queda y a qué velocidad crece? (30 s)

```bash
DB='database_id="oms-production:oms-pg-prod"'
promq "max(cloudsql_disk_bytes_used{$DB} / cloudsql_disk_quota_bytes{$DB})"
promq "max(cloudsql_disk_bytes_used{$DB}) / 1e9"                                # GB usados
promq "max(cloudsql_disk_quota_bytes{$DB}) / 1e9"                               # GB aprovisionados
promq "deriv(max(cloudsql_disk_bytes_used{$DB})[1h:1m]) * 3600 / 1e9"            # GB/h (últimos 60 min)
```

**Si crece > 10 GB/h:** algo escribe muy rápido ahora mismo (casi siempre ficheros temporales). Ve directo al paso 4.
**Si crece poco a poco (< 1 GB/h) pero sin parar:** slot, bloat o tabla creciendo. Pasos 2, 3 y 5.
**Si el ratio > 95 % y crece > 10 GB/h:** quedan minutos. Anuncia en `#incident-response`, haz el paso 4 y prepara la Acción 6 en paralelo.

### 1. ¿Qué tiene configurado el disco y qué ha hecho el auto-increase?

```bash
gcloud sql instances describe oms-pg-prod \
  --format="table(settings.dataDiskSizeGb,settings.dataDiskType,settings.storageAutoResize,settings.storageAutoResizeLimit)"
gcloud sql operations list --instance=oms-pg-prod --limit=10 \
  --format="table(operationType,status,startTime,endTime,user)"
```

**Si `storageAutoResize` es `False`:** alguien lo ha desactivado (¿Terraform?). Es la primera causa a corregir, pero **no lo actives sin entender qué crece** (Acción 5).
**Si `dataDiskSizeGb` ≥ 90 % de `storageAutoResizeLimit`:** el auto-increase ya no te salva. Lo que encuentres en los pasos 2–5 hay que liberarlo hoy.
**Si hay operaciones `UPDATE` recientes de `cloud-sql-service`:** el auto-increase ya ha crecido el disco. Anota en el ticket cuántos GB: ese coste ya es permanente.

### 2. ¿Dónde está el espacio: datos, WAL o temporales?

```bash
psql -X -c "
  SELECT pg_size_pretty(pg_database_size('oms'))                              AS bd_oms,
         (SELECT pg_size_pretty(sum(size)) FROM pg_ls_waldir())                AS wal,
         (SELECT pg_size_pretty(coalesce(sum(size), 0)) FROM pg_ls_tmpdir())   AS temporales_ahora;"
```

**Si `wal` > 10 GB** (lo normal es 1–3 GB): algo retiene WAL. Paso 3.
**Si `temporales_ahora` > 5 GB:** una query está volcando a disco. Paso 4.
**Si `bd_oms` explica casi todo el uso:** son datos. Paso 5.

### 3. ¿Hay slots de replicación reteniendo WAL?

```bash
psql -X -c "
  SELECT slot_name, slot_type, active, database, plugin,
         pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS wal_retenido,
         wal_status
  FROM pg_replication_slots
  ORDER BY pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) DESC NULLS LAST;"
```

**Si hay un slot `logical` con `active = f` y GB retenidos:** es un consumidor CDC muerto o abandonado (PM #1). Pregunta en `#oms-sre` de quién es y ve a la **Acción 4**.
**Si el que retiene es `physical`:** es una réplica gestionada que va retrasada. **No lo toques.** Mira `promq 'cloudsql_replication_lag_seconds'` y sigue el runbook [`09`](../09-postgres-unavailable/runbook.md) (`DRReplicaLagHigh`).
**Si no hay slots o retienen < 1 GB:** el WAL alto viene de mucha escritura reciente. Vuelve al paso 2 en 10 min: debería bajar tras el siguiente checkpoint.

### 4. ¿Hay una query generando ficheros temporales?

```bash
psql -X -c "
  SELECT pid, application_name, usename, now() - query_start AS dura,
         wait_event_type, left(query, 90) AS query
  FROM pg_stat_activity
  WHERE datname = 'oms' AND state = 'active' AND now() - query_start > interval '2 minutes'
  ORDER BY dura DESC;"
psql -X -c "SELECT temp_files, pg_size_pretty(temp_bytes) AS temp_acumulado, stats_reset FROM pg_stat_database WHERE datname = 'oms';"
gcloud logging read 'resource.type="cloudsql_database" AND resource.labels.database_id="oms-production:oms-pg-prod" AND textPayload:"temporary file"' \
  --freshness=1h --limit=10 --format="value(timestamp,textPayload)"
```

**Si una query de `oms-reconcile`, `oms-batch-export` o un usuario humano lleva minutos y el log muestra `temporary file: ... size` grandes:** es ella. **Acción 1**.
**Si no hay ninguna:** los temporales eran de algo que ya terminó. Comprueba el paso 2 otra vez.

### 5. ¿Qué tablas e índices ocupan más y cuánto es basura?

```bash
psql -X -c "
  SELECT n.nspname AS esquema, c.relname AS relacion,
         pg_size_pretty(pg_total_relation_size(c.oid)) AS total,
         pg_size_pretty(pg_indexes_size(c.oid))        AS indices,
         s.n_live_tup, s.n_dead_tup,
         round(100.0 * s.n_dead_tup / nullif(s.n_live_tup + s.n_dead_tup, 0), 1) AS pct_muertas,
         s.last_autovacuum
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
  WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  ORDER BY pg_total_relation_size(c.oid) DESC
  LIMIT 15;"
psql -X -c "
  SELECT count(*) FILTER (WHERE published_at < now() - interval '7 days') AS outbox_publicados_7d,
         pg_size_pretty(pg_total_relation_size('outbox.events'))           AS tamano_outbox
  FROM outbox.events;"
psql -X -c "
  SELECT date_trunc('day', occurred_at)::date AS dia, count(*) AS filas_audit
  FROM shared.audit_log WHERE occurred_at > now() - interval '8 days'
  GROUP BY 1 ORDER BY 1;"
promq 'time() - max(ops_job_last_success_timestamp_seconds{job="db-housekeeping"})'
```

| Si ves… | Significa | Ve a |
|---|---|---|
| `outbox_publicados_7d` > 100.000 o el job de limpieza sin éxito > 8 días | La purga programada no corre | **Acción 2** |
| `pct_muertas` > 20 % y `last_autovacuum` de hace días | Autovacuum no alcanza: bloat | **Acción 3**. Antes, busca transacciones largas (abajo) |
| `audit_log` con un salto de filas/día (×5 o más) desde una fecha | Bug o release que audita de más | Avisa al equipo dueño (revertir con flag o rollback, [`07`](../07-canary-rollout-failed/runbook.md)). **No borres filas** |
| Crecimiento repartido, sin anomalías | Crecimiento legítimo | **Acción 5** (capacidad, en horario laboral) |

Transacciones viejas que impiden a autovacuum limpiar:
```bash
psql -X -c "SELECT pid, application_name, state, now() - xact_start AS xact_age FROM pg_stat_activity
            WHERE backend_xmin IS NOT NULL AND now() - xact_start > interval '30 minutes' ORDER BY xact_age DESC;"
```
Si hay alguna de horas: es la que bloquea la limpieza. Trátala con la Acción 1b del runbook [`01`](../01-db-pool-saturated/runbook.md#acción-1b-terminar-la-sesión-bloqueante-impacto-bajo-medio--solo-si-el-paso-3-encontró-un-tapón).

---

## Acciones (de menor a mayor impacto, idempotentes)

> Primero se libera, después se compra. **Borrar filas no baja `cloudsql_disk_bytes_used`**: deja hueco que la tabla reutiliza tras un `VACUUM`, así que **frena** el crecimiento. Lo que libera disco al momento es terminar una query con temporales, eliminar un slot o `DROP`/`TRUNCATE` (que aquí no se usan sobre datos de negocio).

### Acción 1: Parar la query que llena el disco y pausar los batch (impacto: bajo · reversible en 15 s)

```bash
flag_on orders.batch-jobs-paused                       # los jobs no toman lotes nuevos (≤ 15 s)
psql -X -c "SELECT pg_cancel_backend(<PID>);"          # <PID> del paso 4; si en 10 s sigue: pg_terminate_backend(<PID>)
```
**Qué hace:** la query se cancela, hace rollback y Postgres borra sus temporales al instante. El flag impide que el job la relance.
**Idempotente:** sí. Cancelar un pid que ya no existe devuelve `f` y no hace nada; encender un flag encendido tampoco.
**Coste:** el export o la reconciliación de esa noche se retrasan (quedan en cola). Avisa en `#oms-backoffice`.
**Cuándo NO:** si la query es un `CREATE INDEX CONCURRENTLY` o un `pg_repack` de una ventana planificada. Pregunta en `#oms-sre` antes.

### Acción 2: Ejecutar la purga programada del outbox ahora (impacto: bajo · frena el crecimiento)

```bash
gcloud run jobs execute oms-ops-db-housekeeping --region=$REGION --wait \
  --args="--task=outbox-archive,--older-than=7d,--batch-size=5000"
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="oms-ops-db-housekeeping"' \
  --freshness=30m --limit=20 --format="value(timestamp,jsonPayload.message)"
```
**Qué hace:** exporta los eventos con `published_at` de hace > 7 días a `gs://oms-prod-outbox-archive/` (`europe-west3`, retención de 18 meses, ADR-009) y los borra de `outbox.events` en lotes de 5.000 con `COMMIT` por lote y una pausa de 0,5 s entre lotes (sin locks largos, sin pico de WAL). Termina con `VACUUM (ANALYZE) outbox.events`.
**Idempotente:** sí. Solo borra lo que ya está exportado (comprueba el objeto en GCS antes de cada lote); la segunda ejecución no encuentra filas > 7 días.
**Cuándo NO:** si el relay está atrasado (runbook [`04`](../04-outbox-backlog-creciente/runbook.md)). La purga nunca toca pendientes, pero compite por E/S.

### Acción 3: `VACUUM` de las tablas con bloat (impacto: bajo · sin bloqueo de escrituras)

```bash
psql -X -c "VACUUM (VERBOSE, ANALYZE) <esquema>.<tabla>;"    # tablas del paso 5 con pct_muertas > 20 %
```
**Qué hace:** marca como reutilizable el espacio de las filas muertas. No devuelve disco al sistema, pero la tabla deja de crecer hasta llenar ese hueco.
**Idempotente:** sí. Un segundo `VACUUM` no encuentra nada que limpiar.
**Cuándo NO:** `VACUUM FULL` **nunca** en este runbook. Para recuperar espacio de verdad se usa `pg_repack` en una ventana (09:00–18:00, no viernes, OPS-002), con aprobación del EM y comprobando antes que hay espacio libre ≥ 2 × el tamaño de la tabla.

### Acción 4: Eliminar un slot de replicación huérfano (impacto: medio · irreversible para su consumidor)

```bash
SLOT=<slot_name del paso 3>
psql -X -v slot="$SLOT" <<'SQL'
SELECT slot_name, pg_drop_replication_slot(slot_name) AS eliminado
FROM pg_replication_slots
WHERE slot_name = :'slot' AND slot_type = 'logical' AND NOT active;
SQL
```
**Qué hace:** borra el slot y Postgres libera el WAL retenido en el siguiente checkpoint (minutos). Es la acción que más disco libera.
**Idempotente:** sí. Si el slot ya no existe o está activo, devuelve 0 filas. El filtro impide tocar slots físicos o en uso.
**Coste:** el consumidor (Datastream, Debezium…) pierde su posición y tendrá que resincronizar desde cero. Avísale.
**Quién aprueba:** el dueño del slot. Si nadie lo reclama en 15 min y el disco está > 90 %, el on-call lo decide y lo documenta.

### Acción 5: Subir el límite del auto-increase (impacto: medio · coste permanente si se usa · aprueba el EM)

```bash
gcloud sql instances patch oms-pg-prod --storage-auto-increase --storage-auto-increase-limit=400
```
**Qué hace:** da más margen a la red de seguridad. Cambiar el límite no cuesta nada; **cada GB que el auto-increase use después, sí, y para siempre.** No reinicia la instancia.
**Idempotente:** sí, repetirlo con el mismo valor no cambia nada.
**Cuándo:** solo si el crecimiento es legítimo (paso 5, última fila) o mientras se corrige la causa y las Acciones 1–4 no bastan. Abre ticket de capacity planning (NFR-SCAL-002).

### Acción 6: Subir el disco a mano (impacto: alto · **irreversible** · aprueba el EM)

```bash
gcloud sql instances describe oms-pg-prod --format="value(settings.dataDiskSizeGb)"   # anota el valor actual
gcloud sql instances patch oms-pg-prod --storage-size=280GB         # hoy 230 GB → +50 GB; nunca por encima del límite sin la Acción 5
```
**Qué hace:** amplía el disco en caliente, sin reinicio (el redimensionado del sistema de ficheros tarda unos minutos).
**Cuándo:** solo si el disco está > 97 % o ya lleno (escrituras fallando) y las Acciones 1–4 no liberan a tiempo. Primero recuperas el servicio, después buscas la causa.
**Coste:** el disco de Cloud SQL **no se puede reducir**. +50 GB ≈ 11 €/mes para siempre, o una migración a instancia nueva para recuperarlos.
**Idempotente:** sí con el mismo tamaño (un tamaño menor que el actual da error y no hace nada).
**Quién aprueba:** Engineering Manager de guardia. Si el disco ya está lleno y no responde en 10 min, el on-call decide y lo documenta.

---

## Verificación

**Métricas que tienen que volver a la normalidad:**
```promql
max(cloudsql_disk_bytes_used{database_id="oms-production:oms-pg-prod"} / cloudsql_disk_quota_bytes{database_id="oms-production:oms-pg-prod"}) < 0.80
deriv(max(cloudsql_disk_bytes_used{database_id="oms-production:oms-pg-prod"})[1h:1m]) * 86400 / 1e9 < 2   # < 2 GB/día
max(predict_linear(cloudsql_disk_bytes_used{database_id="oms-production:oms-pg-prod"}[6h], 7 * 86400))
  < max(oms:cloudsql_disk_autoincrease_limit_bytes)                                                          # no llega al límite en 7 días
max(pg_replication_slot_retained_wal_bytes) < 5e9
```
**Ventana de observación:** **1 hora** (las métricas de Cloud SQL llegan con ~3 min de retraso y el crecimiento se ve en horas, no en segundos).

**Cómo confirmas:**
1. Panel "Disco" de [`oms-db-overview`](https://grafana.example.com/d/oms-db-overview): la curva se aplana.
2. Paso 2 del diagnóstico otra vez: WAL < 5 GB y temporales a 0.
3. `CloudSQLDiskUsageHigh` en *resolved*.
4. `#incident-response`: *"Disco de oms-pg-prod estable en XX % (YY GB de ZZ GB), crecimiento N GB/día. Causa: [slot/temporales/outbox/…]. Acciones: […]. GB añadidos al disco (permanentes): N. Flags activos: [lista]. Cerrando."*

---

## Rollback

| Acción | Rollback | Cuándo |
|---|---|---|
| 1 (cancelar + pausar batch) | `flag_off orders.batch-jobs-paused`. Arregla antes el plan de la query (o su `work_mem`), o volverá a llenar el disco. | Cuando el disco esté < 80 % y la query corregida. |
| 2 (purga outbox) | No aplica: lo purgado está en `gs://oms-prod-outbox-archive/` y se puede consultar o reimportar con `\copy`. | — |
| 3 (`VACUUM`) | No aplica. | — |
| 4 (drop slot) | **Irreversible.** El consumidor crea un slot nuevo y resincroniza (snapshot inicial). | — |
| 5 (límite) | `gcloud sql instances patch oms-pg-prod --storage-auto-increase-limit=300`. El límite no puede quedar por debajo del tamaño actual. | Cuando la causa esté corregida. |
| 6 (disco) | **No hay rollback.** El disco no se reduce. La única vuelta atrás es migrar a una instancia nueva más pequeña (proyecto planificado, con ventana). | — |

---

## Después del incidente

- [ ] Anota en el ticket: causa, GB liberados y **GB añadidos de forma permanente** (con su coste mensual).
- [ ] `flag_get orders.batch-jobs-paused`: déjalo como estaba.
- [ ] Slot eliminado: el dueño documenta por qué quedó huérfano (los POC con CDC tienen fecha de caducidad y owner).
- [ ] Query con temporales: ticket al dueño del job con el plan (`EXPLAIN`) y el pico de temporales.
- [ ] Si se tocó el disco o el límite: actualiza Terraform (`disk_size`, `disk_autoresize_limit`) y la recording rule `oms:cloudsql_disk_autoincrease_limit_bytes` en `alert.yaml`, o el próximo `terraform apply` intentará deshacerlo (y fallará al reducir).
- [ ] Si `audit_log` creció por un bug: ticket de capacidad con legal/DPO. El archivado a almacenamiento frío es un proyecto, no una acción de incidente.

---

## Enlaces relacionados

- Alerta YAML: [`alert.yaml`](alert.yaml) · Post-mortems: [`post-mortems.md`](post-mortems.md)
- SLO que protege (cuando el disco se llena): [`../../slos/slos-orders.md`](../../slos/slos-orders.md) (SLO 1)
- ADRs: [`ADR-009 Outbox`](../../../anexo-arquitectura/05-adrs/ADR-009-outbox-pattern.md) (archivado), [`ADR-010 Audit trail`](../../../anexo-arquitectura/05-adrs/ADR-010-audit-trail-gdpr.md) (lo que no se borra)
- Dashboard: <https://grafana.example.com/d/oms-db-overview>
- Runbooks relacionados: [`09-postgres-unavailable`](../09-postgres-unavailable/runbook.md) (si la instancia se para), [`01-db-pool-saturated`](../01-db-pool-saturated/runbook.md) (transacciones largas), [`04-outbox-backlog-creciente`](../04-outbox-backlog-creciente/runbook.md) (el outbox y su purga)
