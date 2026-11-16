# Post-mortems del runbook: cloud-sql-disk-full

> *Escenarios simulados para el trabajo del Bloque 5, construidos sobre la arquitectura real del OMS.*
> Evolución del runbook a lo largo de 3 ocurrencias: V1 manual → V2 con comandos → V3 preventiva.
> Formato blameless: se analizan sistemas y procesos, no personas. Los on-call se identifican por rol.

---

## Versión 1: mitigación manual (PM #1)

**Fecha del incidente:** sábado 14 de febrero de 2026, 03:40 → 04:27 CET
**Duración del incidente:** 47 minutos con escrituras fallando (primer error 03:40 → escrituras restablecidas 04:27).
**Detectado por:** la alerta genérica de error rate > 5 % a las 03:52. Un email automático de Cloud Monitoring ("disk utilization > 90 %") había llegado el viernes a las 22:10 a un buzón compartido que nadie miraba de noche.
**On-call:** SRE de guardia (semana 7).
**Impacto:** 14 `POST /api/orders` con 5xx (madrugada de sábado, poco tráfico): **~20 % del error budget** del SLO 1 de Orders. Logins fallidos durante 47 min (IAM escribe en `audit_log`). Outbox parado. Coste permanente: **+100 GB de disco (≈ 22 €/mes para siempre)**.

### Lo que ocurrió
En enero se probó Datastream para llevar pedidos a BigQuery (POC de analítica, región UE). La POC terminó el 9 de enero y se borró el stream, pero **el slot lógico `datastream_bq_poc` se quedó en Postgres**, inactivo, reteniendo todo el WAL desde entonces (~3 GB/día). En cinco semanas el auto storage increase llevó el disco de 100 GB a su límite (150 GB) sin avisar a nadie, y el sábado a las 03:40 se llenó: `could not write to file "pg_wal/…": No space left on device`.

### Cómo se mitigó (improvisado, sin runbook)
El on-call vio el 5 % de errores, revisó el pool (runbook 01, sano) y tardó ~30 min en ver el aviso de "almacenamiento lleno" en la consola de Cloud SQL. Subió el disco desde la consola a 200 GB (no sabía cuánto hacía falta, así que dobló) y las escrituras volvieron a las 04:27. El lunes, buscando qué había crecido, el equipo encontró el slot reteniendo 96 GB y lo eliminó: el uso bajó de ~150 GB a ~54 GB. **El disco se quedó en 200 GB**: Cloud SQL no lo reduce.

### Aprendizajes y acciones
- **Nadie mira un email a las 22:10 de un viernes.** → Alertas `CloudSQLDiskUsageHigh` (warning 80 %, critical 90 %) por Alertmanager y PagerDuty. *(Hecho, 16/02)*
- **El auto-increase escondió el problema 5 semanas.** Creció de 100 a 150 GB sin que nadie preguntara por qué. → La warning al 80 % sirve justamente para eso: preguntar antes de cada crecimiento.
- **Subir el disco fue correcto para recuperar el servicio, pero se dobló a ciegas.** → Diagnóstico antes de comprar y aprobación del EM para cualquier cambio irreversible.
- Los POC con CDC no tenían owner ni fecha de fin. → Política: todo slot lógico se crea con ticket, owner y fecha de caducidad.

### Versión 1 del runbook tras este post-mortem

> **Runbook v1 (texto literal):**
> "Si dispara CloudSQLDiskUsageHigh: mirar en la consola de Cloud SQL el uso de disco. Comprobar en `pg_replication_slots` si hay slots inactivos y borrarlos. Si el disco está lleno, subirlo desde la consola."

(Dice qué mirar, pero no cómo saber qué ocupa el espacio. Y "subirlo" es la acción irreversible puesta como si fuera barata. Por eso hay V2.)

---

## Versión 2: diagnóstico y comandos exactos (PM #2)

**Fecha del incidente:** martes 16 de junio de 2026, 02:05 → 02:46 CEST
**Duración del incidente:** 41 minutos (inicio del crecimiento 02:05 → disco en el 31 % a las 02:46). Sin impacto en usuarios.
**Detectado por:** `CloudSQLDiskUsageHigh` (critical) a las 02:21. Respuesta del on-call a las 02:24.
**On-call:** SRE de guardia (semana 25).
**Impacto:** ninguna escritura fallida. Coste permanente: el auto-increase creció el disco **de 200 a 230 GB a las 02:18** (≈ 6,6 €/mes para siempre) por un pico que duró 40 minutos.

### Qué se vio durante esta segunda ocurrencia
La reconciliación nocturna (`oms-reconcile`, 02:00) incluía desde la release 2026.24 un join nuevo con `payments`. El plan cambió a un hash join que no cabía en `work_mem` y empezó a escribir ficheros temporales: **~140 GB en 20 minutos**. El V1 ayudó a descartar slots rápido, pero:
1. El V1 terminaba en "súbelo desde la consola". El on-call lo tenía preparado a las 02:30, sin saber que los temporales desaparecen al terminar la query.
2. No había forma de saber si el espacio eran datos, WAL o temporales. Hasta las 02:41 no relacionó el crecimiento con la query de `oms-reconcile` (lo vio en `pg_stat_activity` por la duración).
3. Canceló la query (02:43) y el uso bajó en un minuto de 207 a 71 GB. Pero el auto-increase ya había comprado 30 GB que nunca se devolverán.

### Qué cambió en el runbook (V1 → V2)
- **Paso 2 "datos, WAL o temporales"** con `pg_database_size`, `pg_ls_waldir()` y `pg_ls_tmpdir()`: en 10 segundos sabes a qué rama del diagnóstico ir.
- **Velocidad de crecimiento** (`deriv` en GB/h) como primera pregunta: > 10 GB/h casi siempre son temporales.
- **SQL copiable** para slots (con WAL retenido), temporales, tamaño por tabla y bloat, y la lista de lo que **no se toca nunca** (`audit_log`, slots físicos, `VACUUM FULL`).
- **Acciones ordenadas de lo que libera a lo que compra**, con la subida de disco como última, marcada como irreversible y con aprobación del EM.
- **Verificación con ventana de 1 h**, porque las métricas de Cloud SQL llegan con retraso.

### Acciones derivadas
- `temp_file_limit = 20GB` en los flags de Cloud SQL: una query que intente escribir más falla ella sola, en vez de llenar el disco. *(Hecho, 18/06)*
- `oms-reconcile` corregido (índice nuevo en `payments.payment_intents (order_id)`) y con `work_mem` propio. *(Hecho, sprint 26)*
- Alerta predictiva contra el límite del auto-increase y alerta de slots inactivos.
- Purga programada del outbox (ADR-009 decía "semanal", pero nunca se había implementado: 2,4 millones de filas y 12 GB en `outbox.events`).

---

## Versión 3: predicción y purga programada (PM #3)

**Fecha:** miércoles 23 de septiembre de 2026, 10:30 CEST. **El incidente no llegó a producirse.**

### Qué pasó (o, mejor, qué NO pasó)
El equipo de datos montó otra prueba de CDC para un cuadro de mando de ventas y su conector se cayó el viernes 18 sin que nadie lo viera. El martes 22 a las 16:40, `CloudSQLReplicationSlotInactive` avisó en Slack: slot `debezium_sales_dash` inactivo con 5,4 GB retenidos. El miércoles a las 10:30, el on-call siguió el paso 3 del runbook, confirmó con el owner (que figuraba en el ticket de creación del slot, por la política de PM #1) que la prueba podía resincronizar y ejecutó la Acción 4. El WAL bajó de 8 GB a 1,5 GB en el siguiente checkpoint. El disco nunca pasó del 34 %. Esa misma semana, `temp_file_limit` cortó dos veces una consulta ad hoc de un analista que habría escrito > 60 GB: falló la consulta, no el OMS.

### Qué cambió en el runbook (V2 → V3)
- **Predicción en vez de umbral:** `CloudSQLDiskWillFillIn24h` compara `predict_linear(...[6h], 24h)` con el **límite** del auto-increase (el techo real), publicado como recording rule. `CloudSQLDiskAutoIncreaseNearLimit` avisa cuando la red de seguridad está casi gastada.
- **Causas vigiladas una a una:** `CloudSQLReplicationSlotInactive` (PM #1) y `temp_file_limit` (PM #2).
- **Purga programada:** Cloud Scheduler `oms-ops-db-housekeeping` (`0 4 * * 0`, zona `Europe/Madrid`) → Cloud Run Job `oms-ops-db-housekeeping`: exporta a `gs://oms-prod-outbox-archive/` y borra por lotes el outbox publicado > 7 días, lanza `VACUUM (ANALYZE)` sobre `outbox.events` y publica en `#oms-sre` un informe semanal (top 10 tablas por crecimiento, bloat > 20 %, slots). Es la Acción 2 del runbook, ejecutada cada semana.
- **La purga también necesita alerta:** `DBHousekeepingJobStale` si no termina con éxito en 8 días. La primera ejecución liberó 12 GB dentro de `outbox.events` (reutilizables, no devueltos al disco).
- La alerta critical al 90 % **se mantiene** como última red. La señal habitual pasa a ser la predicción, en horario laboral.

### El runbook como automatización

- **Job:** Cloud Run Job `oms-ops-db-housekeeping` (imagen `oms-ops-tools`, usuario `oms_ops`, `--task=outbox-archive|vacuum|report`, idempotente).
- **Disparador:** Cloud Scheduler `oms-ops-db-housekeeping`, `0 4 * * 0` (domingo 04:00, mínimo tráfico).
- **Owner:** equipo SRE.
- **Salud:** `ops_job_last_success_timestamp_seconds{job="db-housekeeping"}`, vigilada por `DBHousekeepingJobStale`.

### Métrica del runbook a lo largo de la evolución

| | V1 (manual) | V2 (comandos) | V3 (predicción + purga) |
|---|---|---|---|
| Detección | Error rate de la app (disco ya lleno) | Alerta al 90 % | Alerta de slot inactivo, 4 días antes de cualquier umbral |
| Tiempo hasta mitigar | 47 min | 22 min desde la alerta | 0 (sin impacto; acción planificada en horario laboral) |
| Escrituras fallidas | 14 pedidos + logins | 0 | 0 |
| Error budget consumido (SLO 1 Orders) | ~20 % | 0 % | 0 % |
| GB de disco añadidos para siempre | +100 GB (≈ 22 €/mes) | +30 GB (≈ 6,6 €/mes) | 0 |
| Toil del on-call | Alto (de madrugada, a ciegas) | Medio (SQL guiado, de madrugada) | Bajo (un `DROP` aprobado, de día) |

---

## Conclusión

Este runbook trata un problema que no avisa: el disco crece en silencio y, con auto storage increase, ni siquiera se llena cuando debería. En V1 lo pagamos dos veces: 47 minutos sin vender y 100 GB que seguiremos pagando mientras exista la instancia. En V2 aprendimos que la pregunta no es "¿está lleno?" sino "¿qué lo llena y a qué velocidad?", porque 140 GB de temporales desaparecen al cancelar una query y comprar disco para ellos es tirar el dinero. En V3 dejamos de mirar el porcentaje y empezamos a mirar las causas: predicción contra el techo real, slots vigilados, temporales acotados y una purga semanal que también tiene su alarma. Lo pendiente es de capacidad, no de operación: con `audit_log` creciendo 3 años sin poder borrarse (REG-GDPR-003), en 2027 habrá que particionarlo y archivar los meses antiguos en almacenamiento WORM con aprobación del DPO.
