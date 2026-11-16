# Post-mortems del runbook: postgres-unavailable

> *Escenarios simulados para el trabajo del Bloque 5, construidos sobre la arquitectura real del OMS.*
> Evolución del runbook a lo largo de 3 ocurrencias: V1 manual → V2 con comandos → V3 automatizada y preventiva.
> Formato blameless: se analizan sistemas y procesos, no personas. Los on-call se identifican por rol.

---

## Versión 1: mitigación manual (PM #1)

**Fecha del incidente:** jueves 22 de enero de 2026, 11:02 → 11:23 CET
**Duración del incidente:** 21 minutos (primer 5xx 11:02 → checkout sano 11:23). La base de datos solo estuvo caída **48 segundos**.
**Detectado por:** la probe sintética `checkout-e2e` y la alerta genérica de error rate > 5 % (11:05). Al mismo tiempo, quejas en el chat de soporte.
**On-call:** SRE de guardia (semana 4).
**Impacto:** ~50 `POST /api/orders` con 5xx en hora punta: **~71 % del error budget** del SLO 1 de Orders (política en 🟡, 29 % restante a 28 días). El catálogo devolvió 5xx en los misses de caché durante ~6 min (≈ 6 de los 22 min mensuales de NFR-AVAIL-002).

### Lo que ocurrió
Google aplicó una actualización de mantenimiento a `oms-pg-prod`. La instancia **no tenía ventana de mantenimiento configurada**, así que Cloud SQL eligió un jueves a las 11:02. Con HA, el corte fue de 48 s. Pero los pools de conexiones de la app no validaban las conexiones ni tenían keepalive: se quedaron con sockets muertos y cada petición esperaba al `statement_timeout` (30 s) antes de fallar. La BD estaba sana a las 11:03; el checkout siguió fallando 20 minutos más.

### Cómo se mitigó (improvisado, sin runbook)
El on-call vio la instancia `RUNNABLE` en la consola y pensó que el problema era otro. Revisó el pool (runbook 01), Stripe y el último deploy. A las 11:18, sin más hipótesis, forzó una revisión nueva de `oms-orders` y `oms-catalog` desde la consola. Los pools nuevos conectaron y a las 11:23 el checkout estaba sano. Nadie supo hasta la tarde que había habido un mantenimiento (estaba en `gcloud sql operations list`).

### Aprendizajes y acciones
- **El problema no fue la caída de 48 s, sino que la app no se recuperó.** → Ticket **OMS-1311**: validación de conexiones, `keepAlive` TCP e `idleTimeout` de 30 s en los pools; reintento con backoff de las transacciones que fallan por conexión. *(Hecho, 05/02)*
- **El mantenimiento cayó en hora punta.** → Ventana de mantenimiento el domingo a las 03:00 (Europe/Madrid). *(Hecho, 23/01)*
- **Nadie miró las operaciones de Cloud SQL.** → Alerta `PostgresUnavailable` y runbook con `gcloud sql operations list` como paso fijo.
- El catálogo cayó en los misses de caché. → Flag `catalog.serve-stale` (stale-while-revalidate) y `checkout.maintenance-page` para no mostrar un 5xx crudo.
- Nueva métrica `db_connection_errors_total` en la app: distinguir "la BD no responde" de "la app no conecta".

### Versión 1 del runbook tras este post-mortem

> **Runbook v1 (texto literal):**
> "Si dispara PostgresUnavailable: comprobar en la consola el estado de `oms-pg-prod` y las operaciones recientes. Si hay un failover o mantenimiento, esperar. Si la BD está arriba y la app sigue fallando, desplegar una revisión nueva de `oms-orders` y `oms-catalog`. Si no vuelve, failover manual."

(Dice qué mirar, pero no cuánto esperar, ni cómo saber si el failover ya está en marcha, ni qué hacer si el failover no basta. Por eso hay V2.)

---

## Versión 2: diagnóstico y comandos exactos (PM #2)

**Fecha del incidente:** jueves 9 de julio de 2026, 04:12 → 04:58 CEST
**Duración del incidente:** 74 s de BD caída (04:12:10 → 04:13:24) + 44 min de "Mis pedidos" fallando por una causa secundaria.
**Detectado por:** `PostgresUnavailable` a las 04:13:30. Respuesta del on-call a las 04:15.
**On-call:** SRE de guardia (semana 28).
**Impacto:** 3 `POST /api/orders` fallidos durante el failover (madrugada): **~4 % del budget** del SLO 1, dentro de lo presupuestado para un failover HA. Además, **~35 `GET /api/orders` con 5xx** durante 44 min (backoffice del turno de noche y clientes consultando su pedido).

### Qué se vio durante esta segunda ocurrencia
Fallo de infraestructura en la zona `europe-west3-a`. Cloud SQL hizo failover automático a `europe-west3-b` en 74 s y, gracias a OMS-1311, los pools se recuperaron solos en ~15 s. El V1 sirvió, pero:
1. **El V1 no decía cuánto esperar.** A las 04:15 el on-call lanzó `gcloud sql instances failover` mientras el failover automático acababa. Falló con "operation in progress", sin daño, pero pudo haber encolado un segundo corte.
2. **Un flag olvidado.** `orders.read-from-replica` seguía en ON desde un incidente del runbook 01 tres días antes, y la réplica de lectura `oms-pg-prod-replica` estaba en la zona caída. "Mis pedidos" falló hasta que el on-call encontró el flag a las 04:58.
3. **Nadie sabía que la HA no estaba disponible.** El standby se reconstruyó durante 1 h 50 min; un segundo fallo en ese rato habría sido una caída sin failover.
4. El V1 terminaba en "failover manual". No había nada para un primario que no vuelve (PITR, DR) ni sobre quién aprueba qué.

### Qué cambió en el runbook (V1 → V2)
- **Diagnóstico 0** que separa caída real, exporter caído y problema de una sola app, con `cloudsql_up` + `pg_up` + `db_connection_errors_total`.
- **Paso 2 "¿hay una operación en curso?"** con la regla explícita: un failover automático no se toca. **Acción 2 "esperar"** con límite de 3 min y la guarda de idempotencia antes de un failover manual.
- **Acciones de alto impacto con su coste en datos:** PITR (Acción 5, EM) y DR (Acción 6, CTO), con el RPO calculado antes de aprobar y la advertencia de que no hay vuelta atrás.
- **Verificación con HA incluida** (`cloudsql_available_for_failover`) y checklist que obliga a revisar **todos** los flags al cerrar.

### Acciones derivadas
- Alerta `CloudSQLFailoverInProgress`: HA no disponible. *(Hecho, 13/07)*
- Réplica de lectura recreada en `europe-west3-c` (otra zona que el primario y el standby). *(Hecho, 20/07)*
- Política de flags: todo flag operativo encendido abre un recordatorio en el ticket del incidente; el informe diario de `#oms-sre` lista los flags en ON de más de 24 h. *(Hecho)*
- Automatizar la Acción 1 (los flags de degradación) para que no dependan de que alguien se despierte.
- Probar de verdad el PITR: OPS-005 pide un ejercicio mensual que no se estaba haciendo.

---

## Versión 3: automatización y prevención (PM #3)

**Fecha:** domingo 18 de octubre de 2026, 03:00 CEST. **El incidente no llegó a paginar a nadie.**

### Qué pasó (o, mejor, qué NO pasó)
Google aplicó un mantenimiento a `oms-pg-prod`, esta vez **dentro de la ventana del domingo a las 03:00**. Corte de 41 s (03:00:42 → 03:01:23). A las 03:01:02, `PostgresUnavailableFast` (20 s) llamó al webhook de `oms-ops-flag-automator`, que encendió `checkout.maintenance-page` y `catalog.serve-stale` y lo anunció en `#oms-alerts`. Los pools se recuperaron solos en 12 s. `PostgresUnavailable` (1 min) nunca llegó a disparar. A las 03:06:30, con la BD estable 5 min, el automatizador apagó los dos flags. Dos clientes vieron "tu carrito está guardado" en vez de un error; uno de ellos completó el pedido a las 03:04. `CloudSQLFailoverInProgress` siguió abierta 52 min en Slack, mientras se reconstruía el standby, sin que nadie tuviera que hacer nada.

### Qué cambió en el runbook (V2 → V3)
- **La Acción 1 es automática:** `PostgresUnavailableFast` → webhook → flags de degradación en ≤ 35 s, y apagado automático tras 5 min estables. El on-call solo la comprueba.
- **Prevención del mantenimiento:** la ventana del domingo a las 03:00 (`--maintenance-window-day=SUN --maintenance-window-hour=1`, en UTC, configurada tras PM #1) se completa con un periodo de exclusión para Black Friday (`--deny-maintenance-period-start-date=2026-11-20 --deny-maintenance-period-end-date=2026-12-02 --deny-maintenance-period-time=00:00:00`) y con aviso previo de Google por email al grupo `oms-oncall@`.
- **PITR probado cada mes:** Cloud Scheduler `oms-ops-pitr-drill` (`0 10 1 * *`) → Cloud Run Job que clona `oms-pg-prod` a una hora de antigüedad, valida conteos y el último `order_id`, mide el tiempo de restauración (septiembre: 38 min para 74 GB, dentro del RTO de 4 h) y borra el clon. Así la Acción 5 tiene tiempos reales, no estimados.
- **Las automatizaciones tienen alarma:** `FlagAutomatorStale` (compartida) y `PITRRestoreDrillStale`.
- La alerta critical **no cambia** (1 min): si la caída dura más que un failover, sigue haciendo falta una persona.

### El runbook como automatización

- **Degradación:** Cloud Run `oms-ops-flag-automator`, disparado por la ruta `automation` de Alertmanager (`PostgresUnavailableFast`). Owner: equipo SRE. Salud: `ops_job_last_success_timestamp_seconds{job="flag-automator"}`.
- **Ejercicio PITR:** Cloud Run Job `oms-ops-pitr-drill`, mensual. Owner: equipo SRE. Salud: `ops_job_last_success_timestamp_seconds{job="pitr-restore-drill"}`.
- **Prevención:** ventana de mantenimiento y periodos de exclusión en Terraform (`maintenance_window`, `deny_maintenance_period` del módulo de `oms-pg-prod`).

### Métrica del runbook a lo largo de la evolución

| | V1 (manual) | V2 (comandos) | V3 (automatizado) |
|---|---|---|---|
| Corte de la BD | 48 s | 74 s | 41 s |
| Impacto total en el usuario | 21 min | 74 s (checkout) + 44 min ("Mis pedidos") | 41 s, con mensaje de mantenimiento |
| Detección | Probe + clientes | Alerta propia | Automatización a los 20 s, sin página |
| `POST /api/orders` fallidos | ~50 | 3 | 2 |
| Error budget consumido (SLO 1 Orders) | ~71 % (🟡) | ~4 % | ~3 % |
| Toil del on-call | Alto (20 min de hipótesis) | Medio (de madrugada) | Cero |

---

## Conclusión

Este runbook enseña que, con HA, la caída de la base de datos casi nunca es el problema: el failover tarda un minuto y está presupuestado en el SLO. El problema es todo lo que pasa alrededor. En V1 la BD estuvo caída 48 segundos y el checkout, 21 minutos, porque la app no sabía reconectar y nadie miró las operaciones de Cloud SQL. En V2 la app se recuperó sola, pero un flag olvidado de otro incidente y una réplica en la zona equivocada alargaron el impacto, y casi lanzamos un failover encima de otro. El runbook aprendió a decir "espera" con un límite claro y a poner precio (en datos y en aprobaciones) a las acciones que no tienen vuelta atrás. En V3 el sistema se degrada solo antes de que nadie se despierte, el mantenimiento cae a la hora de menos tráfico y el PITR se prueba cada mes en lugar de suponerse. Lo pendiente es el escenario que aún no hemos vivido: la pérdida de la región entera. El DR en `europe-west1` está documentado y su lag vigilado, pero hasta el ejercicio anual de failover regional (ADR-007), su RTO de 4 h es una estimación y no un dato.
