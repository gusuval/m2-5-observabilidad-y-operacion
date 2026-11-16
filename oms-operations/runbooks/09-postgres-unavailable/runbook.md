# Runbook: Postgres no disponible (Cloud SQL `oms-pg-prod`)

> **Fuera del catálogo.** Es el modo de fallo con más blast radius del OMS: todos los módulos comparten una única instancia (ADR-004). La mayoría de las veces se resuelve solo (failover HA en ~60 s) y lo peor que puede hacer el on-call es estorbar. Este runbook dice cuándo esperar, cuándo actuar y cuándo pedir permiso.
> **Versión:** V3 (ver `post-mortems.md`). **Owner:** equipo SRE. **Última revisión:** 2026-10-21.

---

## Trigger

**Alerta principal:** `PostgresUnavailable` (severity: **critical**, PagerDuty)
**Condición:** durante **1 minuto**, Cloud SQL informa la instancia caída (`cloudsql_up == 0`), **o** `postgres_exporter` no conecta (`pg_up == 0`) **y a la vez** la app registra errores de conexión (`db_connection_errors_total` > 0,1/s). La segunda rama evita páginas falsas cuando lo que falla es el exporter del bastión.

**Alertas asociadas:**
| Alerta | Severity | Condición | Qué te dice |
|---|---|---|---|
| `PostgresUnavailableFast` | `automation` (solo webhook, no pagina) | lo mismo durante 20 s | Dispara la degradación automática (V3): `checkout.maintenance-page` + `catalog.serve-stale`. |
| `CloudSQLFailoverInProgress` | warning | `cloudsql_available_for_failover == 0` durante 2 min | La HA no está disponible: failover en curso o el standby se está reconstruyendo. **Mientras dure, un segundo fallo no tiene failover.** |
| `DRReplicaLagHigh` | warning | lag de `oms-pg-dr` > 300 s durante 15 min | El RPO real de un DR ahora mismo sería ese lag. |
| `CloudSQLBackupMissing` | warning | > 26 h sin backup correcto | PITR y restore en riesgo (OPS-005). |

**Cuándo dispara (patrones conocidos):**
1. **Mantenimiento de Cloud SQL** (PM #1, PM #3): actualización del motor o del SO. Con HA, corte de 30–60 s. Desde PM #1 solo ocurre en la ventana del domingo 03:00 (y nunca en el periodo de exclusión de Black Friday).
2. **Fallo zonal** (PM #2): cae la zona del primario y Cloud SQL hace failover automático a la otra zona en ~60 s (NFR-AVAIL-001).
3. **Cambio que reinicia la instancia**: `gcloud sql instances patch` de tier o de un flag que requiere reinicio (runbook 01, Acción 4).
4. **Causas que parecen caída pero no lo son**: disco lleno (escrituras fallan, → [`06`](../06-cloud-sql-disk-full/runbook.md)), `max_connections` agotado (`too many clients`, → [`01`](../01-db-pool-saturated/runbook.md)), credenciales rotadas, red (Private Service Access) o el propio bastión.

> **Topología:** `oms-pg-prod` (Postgres 16, HA regional: primario y standby síncrono en dos zonas de `europe-west3`; cada failover las intercambia, la zona actual está en el panel "HA / réplicas"), réplica de lectura `oms-pg-prod-replica` (en una tercera zona) y réplica DR asíncrona `oms-pg-dr` en `europe-west1`. Las apps conectan por IP privada (`DB_HOST`); **un failover HA mantiene la IP**, así que no hay que tocar la app.

---

## Contexto

**Qué impacto tiene:**
- **Checkout parado**: `POST /api/orders` devuelve 503. Consume el SLO 1 de Orders: un failover normal (~60 s) cuesta 2–5 peticiones (3–7 % del budget de 28 d). **Cada minuto más en hora punta son ~3 pedidos perdidos (~4 % del budget).**
- **Login, "Mis pedidos", backoffice**: no funcionan.
- **Outbox**: parado. Al volver la BD, los emails y confirmaciones salen con retraso (runbook [`04`](../04-outbox-backlog-creciente/runbook.md), se drena solo).
- **Pagos con Stripe en curso**: si la autorización se hizo y el pedido no se guardó, la autorización caduca sola a los 7 días sin cobrar. No hay cobros sin pedido.

**Qué NO afecta (degradación elegante, OPS-007):**
- **Catálogo**: se sirve desde Redis. Con `catalog.serve-stale` sigue respondiendo aunque el TTL expire (precios de hace minutos; el checkout siempre revalida).
- **SPA y estáticos**: Cloud CDN.
- **Feature flags**: Unleash tiene su propia BD (no `oms-pg-prod`) y el SDK cachea el último estado. Los flags funcionan con Postgres caído.
- **Datos ya confirmados**: el commit del primario es síncrono con el standby. **Un failover HA no pierde transacciones confirmadas (RPO 0).** Solo PITR y DR implican pérdida de datos (ver Acciones 5 y 6).

**Pre-requisitos para diagnosticar:**
- Rol IAM `roles/cloudsql.viewer` + `roles/cloudsql.client` + `roles/iap.tunnelResourceAccessor` + `roles/run.developer` + `roles/logging.viewer` (grupo `oms-oncall@`).
- **Acciones 4–6** requieren `roles/cloudsql.admin`, que se obtiene por elevación temporal (PAM, 4 h) con aprobación: Acción 4 la aprueba el propio on-call (avisa al EM); Acción 5, el **EM**; Acción 6, el **CTO**.
- Dashboard [`oms-db-overview`](https://grafana.example.com/d/oms-db-overview), paneles "Disponibilidad" y "HA / réplicas"; [`oms-overview`](https://grafana.example.com/d/oms-overview), panel "SLO Orders".
- **Setup de sesión** ejecutado (ver [`../README.md`](../README.md#setup-de-sesión-del-on-call-copiar-una-vez-al-empezar-el-incidente)). Si el túnel `psql` falla, el resto del runbook sigue funcionando: casi todo es `gcloud` y `promq`.

---

## Diagnóstico (en orden, ejecuta y mira)

> Los comandos están **copiables**. Mira el reloj al empezar: el failover automático debería terminar en < 2 min.

### 0. ¿Es real? ¿La BD, el exporter o la app? (30 s)

```bash
promq 'max(cloudsql_up{database_id="oms-production:oms-pg-prod"})'
promq 'max(pg_up)'
promq 'sum by (job, error) (rate(db_connection_errors_total[2m]))'
promq 'sum by (job) (rate(http_requests_total{job=~"oms-orders|oms-catalog", code=~"5.."}[2m])) / sum by (job) (rate(http_requests_total{job=~"oms-orders|oms-catalog"}[2m]))'
promq 'avg_over_time(probe_success{probe="checkout-e2e"}[5m])'
```

**Si `cloudsql_up` = 0 o hay errores de conexión en todas las apps:** caída real. Anuncia en `#incident-response`: *"PostgresUnavailable: oms-pg-prod no responde, checkout afectado. Investigando."* y sigue con el paso 1. Comprueba que la degradación automática está activa (Acción 1).
**Si solo `pg_up` = 0 y las apps no tienen errores:** falla el exporter o el túnel del bastión, no la BD. No es este runbook: revisa `oms-ops-bastion` en horario laboral.
**Si los errores son de un solo `job`:** el problema es de esa app (credenciales, pool). Paso 4.

### 1. ¿En qué estado está la instancia y en qué zona?

```bash
gcloud sql instances describe oms-pg-prod \
  --format="table(state,gceZone,secondaryGceZone,settings.availabilityType,settings.activationPolicy,settings.tier)"
gcloud sql instances describe oms-pg-prod --format="yaml(scheduledMaintenance,settings.maintenanceWindow,settings.denyMaintenancePeriods)"
```

| `state` | Significa | Ve a |
|---|---|---|
| `RUNNABLE` con `gceZone` distinta a la del panel "HA / réplicas" antes del incidente | Ya hubo failover. La BD debería responder | Paso 3 y verificación |
| `MAINTENANCE` o `scheduledMaintenance` con hora de ahora | Mantenimiento en curso | **Acción 2** (esperar) |
| `RUNNABLE` pero no conecta nadie | Red, credenciales o la BD colgada | Paso 3 |
| `FAILED` o `SUSPENDED` | La instancia no se recupera sola | Paso 2 y **Acción 4/5** |

### 2. ¿Hay alguna operación en curso? (no actúes encima de ella)

```bash
gcloud sql operations list --instance=oms-pg-prod --limit=10 \
  --format="table(name,operationType,status,startTime,endTime,error.errors[0].code)"
gcloud logging read 'resource.type="cloudsql_database" AND resource.labels.database_id="oms-production:oms-pg-prod" AND severity>=WARNING' \
  --freshness=15m --limit=30 --format="value(timestamp,severity,textPayload)"
```

**Si hay una operación `FAILOVER`, `MAINTENANCE`, `UPDATE` o `RESTART` en `RUNNING`:** Cloud SQL está trabajando. **No lances otra operación**: o fallará con "operation in progress", o se pondrá en cola detrás. **Acción 2**.
**Si los logs muestran `No space left on device`:** disco lleno → [`06`](../06-cloud-sql-disk-full/runbook.md), Acción 6.
**Si muestran `remaining connection slots are reserved` o `too many clients`:** la BD está viva y sin conexiones libres → [`01`](../01-db-pool-saturated/runbook.md).
**Si no hay operaciones ni errores y la instancia no responde desde hace > 3 min:** mira <https://status.cloud.google.com> (Cloud SQL, `europe-west3`) y ve a la **Acción 4**.

### 3. ¿Se llega a la BD por la red privada?

```bash
DB_IP=$(gcloud sql instances describe oms-pg-prod --format="value(ipAddresses[0].ipAddress)")
gcloud compute ssh oms-ops-bastion --zone=europe-west3-a --tunnel-through-iap \
  --command="pg_isready -h $DB_IP -p 5432 -t 5"
gcloud services vpc-peerings list \
  --network="$(gcloud sql instances describe oms-pg-prod --format='value(settings.ipConfiguration.privateNetwork)' | awk -F/ '{print $NF}')"
```

**Si `pg_isready` dice `accepting connections`:** la BD está bien. Si la app sigue fallando, el problema está en la app (paso 4).
**Si dice `no response` y la instancia está `RUNNABLE` sin operaciones:** la BD está colgada o hay un problema de red. Si el peering `servicenetworking-googleapis-com` no aparece `ACTIVE`, es red (escala a plataforma). Si está activo → **Acción 4**.

### 4. Si la BD responde: ¿por qué la app no conecta?

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND jsonPayload.event="db.connection.failed"' \
  --freshness=10m --limit=20 --format="value(timestamp,resource.labels.service_name,jsonPayload.error_code)"
gcloud secrets versions list oms-db-app-password --limit=3 --format="table(name,state,createTime)"
promq 'max by (job) (db_pool_in_use / db_pool_size)'
```

**Si `error_code` = `auth_failed`:** credenciales. ¿Se ha rotado `oms-db-app-password` sin cambiar la contraseña de `oms_app` (o al revés)? Escala al EM: no se cambian contraseñas a ciegas de madrugada.
**Si `error_code` = `connection_refused`/`timeout` y la BD responde desde el bastión desde hace > 3 min:** los pools de la app no se han recuperado. **Acción 3**.

---

## Acciones (de menor a mayor impacto, idempotentes)

> Regla de oro: **un failover automático en curso no se toca.** Las Acciones 1 y 2 son casi siempre suficientes. Las 5 y 6 implican pérdida de datos y requieren aprobación.

### Acción 1: Degradación elegante con feature flags (impacto: bajo · reversible en 15 s · automática desde V3)

```bash
flag_get checkout.maintenance-page; flag_get catalog.serve-stale      # V3: el automatizador ya debería haberlos encendido
flag_on checkout.maintenance-page
flag_on catalog.serve-stale
```
**Qué hace:** el checkout muestra "estamos teniendo problemas, tu carrito está guardado" y la API responde 503 con `Retry-After` (el fallo **sigue contando en el SLO**; lo que cambia es que el cliente sabe qué pasa y no reintenta en bucle). El catálogo sirve desde Redis aunque el TTL haya expirado.
**Por qué primero:** no toca la BD, tarda ≤ 15 s y protege la experiencia mientras se diagnostica.
**Idempotente:** sí. Si `oms-ops-flag-automator` ya los encendió, no cambia nada.
**Cuándo NO:** si el paso 0 demostró que no es una caída real (exporter).

### Acción 2: Esperar al failover o al mantenimiento (impacto: ninguno · hasta 3 min)

```bash
watch -n 10 'gcloud sql operations list --instance=oms-pg-prod --limit=3 --format="table(operationType,status,startTime,endTime)"; \
  gcloud sql instances describe oms-pg-prod --format="value(state,gceZone)"'
```
**Qué hace:** nada, a propósito. El failover HA tarda ~60 s (NFR-AVAIL-001) y un mantenimiento con HA, 30–60 s. Lanzar otra operación encima no lo acelera.
**Cuándo dejar de esperar:** si a los **3 minutos** desde el primer error la instancia no está `RUNNABLE` o no hay ninguna operación en curso. Entonces, **Acción 4**.

### Acción 3: Renovar los pools de la app si no se recuperan solos (impacto: medio · solo tras los pasos 3 y 4)

```bash
for svc in oms-orders oms-catalog oms-outbox-relay; do
  gcloud run services update "$svc" --region=$REGION --update-env-vars=DB_RECONNECT_EPOCH="$(date -u +%Y%m%dT%H%M)"
done
```
**Qué hace:** crea una revisión idéntica de cada servicio con instancias nuevas y pools limpios. Desde PM #1, los pools validan conexiones y descartan sockets muertos en ≤ 30 s, así que esto solo hace falta si ese mecanismo falla. **No es un "reinicia el servicio" a ciegas:** solo se ejecuta cuando el paso 3 demostró que la BD acepta conexiones y el paso 4 que la app no.
**Idempotente:** con el mismo valor del minuto, sí. Rolling update sin downtime (la revisión anterior sirve hasta que la nueva está lista). Anota en el ticket que hubo despliegue fuera de ventana (OPS-002).

### Acción 4: Failover manual (impacto: medio · ~60 s de corte · aprueba el on-call, avisa al EM)

```bash
# Guarda de idempotencia: solo si NO hay ninguna operación en curso
gcloud sql operations list --instance=oms-pg-prod --filter="status!=DONE" --format="value(name,operationType)"
gcloud sql instances failover oms-pg-prod
```
**Qué hace:** promueve el standby de la otra zona a primario. La IP no cambia.
**Cuándo:** la instancia lleva > 3 min sin responder, no hay operación en curso, el standby está disponible (`promq 'cloudsql_available_for_failover'` = 1 antes del fallo) y Google no informa de incidencia regional.
**Idempotente:** el comando **no** lo es (cada ejecución es un failover nuevo). La guarda sí: si el primer comando devuelve alguna operación, no lo lances.
**Cuándo NO:** con una operación en curso, o si el standby ya estaba caído (failover sin destino) → Acción 5.

### Acción 5: Restaurar con PITR en una instancia nueva (impacto: alto · **pérdida de datos** · aprueba el EM)

```bash
# Punto en el tiempo: el último momento sano (UTC). PITR cubre 14 días (OPS-005).
PITR_TS="2026-10-21T03:14:00Z"
NEW=oms-pg-pitr-$(date -u +%Y%m%d%H%M)
gcloud sql instances clone oms-pg-prod "$NEW" --point-in-time="$PITR_TS"
gcloud sql instances describe "$NEW" --format="value(state,ipAddresses[0].ipAddress)"
# Con la instancia RUNNABLE y validada (conteo de pedidos y último order_id), apuntar las apps:
for svc in oms-orders oms-catalog oms-outbox-relay; do
  gcloud run services update "$svc" --region=$REGION --update-env-vars=DB_HOST=<IP_NUEVA>
done
```
**Cuándo:** la instancia está `FAILED` y no se recupera, o los datos están corruptos o se ejecutó un cambio destructivo (un `DELETE` sin `WHERE`, una migración rota). **No** para una caída que el failover resuelve.
**Qué se pierde:** todo lo escrito después de `PITR_TS`. Antes de aprobar, el EM recibe: minutos de datos perdidos, nº estimado de pedidos (≈ 100/h en horario diurno) y el plan de reconciliación con Stripe (pagos capturados sin pedido en la BD restaurada → reembolso o recreación manual).
**Coste:** el clon tarda 30–60 min con ~75 GB. Hay que recrear después la réplica de lectura y la DR contra la instancia nueva. Si el nombre de instancia `NEW` ya existe, `clone` falla y no hace nada (idempotente).
**Quién aprueba:** Engineering Manager. Con pérdida estimada > 15 min de pedidos, también el CTO.

### Acción 6: Activar el DR en `europe-west1` (impacto: muy alto · **irreversible** · aprueba el CTO)

```bash
promq 'cloudsql_replication_lag_seconds{replica="oms-pg-dr"}'      # RPO real = lag en el momento del fallo (objetivo ≤ 1 h)
gcloud sql instances promote-replica oms-pg-dr
# Capacidad de app en DR (cold standby, OPS-005): Terraform con la imagen de la última release
cd infra/envs/dr && terraform apply -var="dr_active=true" -var="db_host=$(gcloud sql instances describe oms-pg-dr --format='value(ipAddresses[0].ipAddress)')"
flag_on catalog.cache-bypass                                          # en DR no hay Memorystore
```
**Cuándo:** la región `europe-west3` está caída (incidencia de Google confirmada) y la previsión supera ~1 h, o el primario y el PITR no son viables. RTO objetivo ≤ 4 h, RPO ≤ 1 h (OPS-005). `europe-west1` está en la UE (REG-GDPR-001).
**Qué hace:** la réplica DR pasa a ser primaria independiente; Terraform despliega `oms-catalog`, `oms-orders` y `oms-outbox-relay` en `europe-west1` y añade sus NEG serverless a los backends del Load Balancer global (el dominio y la IP pública no cambian).
**Qué se pierde:** las transacciones que no llegaron a replicarse (el lag del primer comando).
**Irreversible:** una réplica promovida no vuelve a ser réplica. Volver a `europe-west3` es un proyecto planificado (nueva réplica desde DR + switchover en ventana, horas).
**Idempotente:** `promote-replica` sobre una instancia ya promovida falla sin efecto; `terraform apply` es idempotente.

---

## Verificación

**Métricas que tienen que volver a la normalidad:**
```promql
min_over_time(max(cloudsql_up{database_id="oms-production:oms-pg-prod"})[15m:30s]) == 1      # 15 min sin caídas
sum(rate(db_connection_errors_total[5m])) == 0
sum(rate(http_requests_total{job="oms-orders", endpoint="/api/orders", method="POST", code=~"5.."}[5m]))
  / sum(rate(http_requests_total{job="oms-orders", endpoint="/api/orders", method="POST", code!~"4.."}[5m])) < 0.001
avg_over_time(probe_success{probe="checkout-e2e"}[10m]) == 1
max(cloudsql_available_for_failover{database_id="oms-production:oms-pg-prod"}) == 1             # HA protegida otra vez
```
**Ventana de observación:** **15 minutos** con todo en verde. La última condición (HA) puede tardar 30–120 min en volver tras un failover: **el incidente se cierra igual, pero `CloudSQLFailoverInProgress` sigue abierto** y nadie toca la instancia hasta que vuelva a 1.

**Cómo confirmas:**
1. Paneles "Disponibilidad" y "HA / réplicas" de [`oms-db-overview`](https://grafana.example.com/d/oms-db-overview).
2. Con la BD estable 5 min: `flag_off checkout.maintenance-page`; 5 min después, `flag_off catalog.serve-stale` (o lo hace el automatizador). Comprueba con `flag_get`.
3. Outbox drenando (`max(outbox_oldest_pending_age_seconds)` bajando; si no, runbook [`04`](../04-outbox-backlog-creciente/runbook.md)) y réplicas al día (`cloudsql_replication_lag_seconds` < 5 s en `oms-pg-prod-replica`).
4. `#incident-response`: *"oms-pg-prod disponible desde HH:MM (zona X). Causa: [failover/mantenimiento/…]. Corte total: N s. Acciones: […]. Datos perdidos: ninguno / [PITR/DR: N min]. HA: [protegida/reconstruyendo]. Flags: todos OFF. Cerrando."*

---

## Rollback

| Acción | Rollback | Cuándo / coste |
|---|---|---|
| 1 (flags) | `flag_off checkout.maintenance-page` y luego `flag_off catalog.serve-stale` | Con la BD estable 5 min. Si se apagan antes y la BD vuelve a caer, se encienden otra vez. |
| 2 (esperar) | — | — |
| 3 (renovar pools) | `gcloud run services update-traffic <svc> --region=$REGION --to-revisions=<REVISION_ANTERIOR>=100` | Solo si la revisión nueva no arranca. |
| 4 (failover manual) | **No hay "des-failover".** Volver a la zona original es **otro failover** (otro corte de ~60 s). Las zonas son equivalentes: solo se hace si hay motivo (latencia, la réplica de lectura en la otra zona), en ventana y con el standby ya reconstruido. | Planificado, nunca en caliente. |
| 5 (PITR) | Volver a apuntar `DB_HOST` a `oms-pg-prod` si sigue viva, **perdiendo lo escrito en el clon**. La instancia original **no se borra** en 7 días (análisis forense). | Decide el EM. |
| 6 (DR) | **Irreversible.** Retorno a `europe-west3` = proyecto de switchover planificado en ventana (horas), con su propio plan y aprobación del CTO. | — |

---

## Después del incidente

- [ ] Ticket con: T_primer_error → T_verificado, corte total en segundos, acción que lo resolvió, peticiones fallidas y budget consumido (SLO 1 Orders y NFR-AVAIL-002).
- [ ] **Todos los flags en OFF** (`flag_get` de `checkout.maintenance-page`, `catalog.serve-stale`, `catalog.cache-bypass`, `orders.read-from-replica`). En PM #2 un flag olvidado de otro incidente alargó el impacto 40 min.
- [ ] Si fue mantenimiento fuera de la ventana: revisa `settings.maintenanceWindow` y los `denyMaintenancePeriods`.
- [ ] Si hubo failover: confirma que `cloudsql_available_for_failover` volvió a 1 y anota en qué zona queda el primario.
- [ ] Si se usaron las Acciones 5 o 6: post-mortem en < 24 h, reconciliación con Stripe y finanzas, y recrear réplicas.
- [ ] Si la Acción 3 hizo falta: bug al equipo de plataforma de la app (los pools tienen que recuperarse solos).

---

## Enlaces relacionados

- Alerta YAML: [`alert.yaml`](alert.yaml) · Post-mortems: [`post-mortems.md`](post-mortems.md)
- SLO que protege: [`../../slos/slos-orders.md`](../../slos/slos-orders.md) (SLO 1; el failover HA está presupuestado ahí) · política: [`../../slos/error-budget-policy.md`](../../slos/error-budget-policy.md)
- ADRs: [`ADR-004 PostgreSQL multi-AZ`](../../../anexo-arquitectura/05-adrs/ADR-004-postgresql-multi-az.md), [`ADR-007 Regiones y DR`](../../../anexo-arquitectura/05-adrs/ADR-007-regiones-despliegue.md)
- Dashboard: <https://grafana.example.com/d/oms-db-overview>
- Runbooks relacionados: [`10-checkout-unavailable`](../10-checkout-unavailable/runbook.md) (triage que trae aquí), [`01-db-pool-saturated`](../01-db-pool-saturated/runbook.md), [`06-cloud-sql-disk-full`](../06-cloud-sql-disk-full/runbook.md), [`04-outbox-backlog-creciente`](../04-outbox-backlog-creciente/runbook.md) (lo que queda después), [`11-catalog-unavailable`](../11-catalog-unavailable/runbook.md)
