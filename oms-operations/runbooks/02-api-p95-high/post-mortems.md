# Post-mortems del runbook: api-p95-high

> *Escenarios simulados para el trabajo del Bloque 5, construidos sobre la arquitectura real del OMS.*
> Evolución del runbook a lo largo de 3 ocurrencias del incidente: V1 manual → V2 con comandos → V3 automatizada.
> Formato blameless: se analizan sistemas y procesos, no personas. Los on-call se identifican por rol.

---

## Versión 1: mitigación manual (PM #1)

**Fecha del incidente:** miércoles 17 de junio de 2026, 14:20 → 15:07 CEST
**Duración del incidente:** 47 minutos (primera queja a las 14:28; p95 por debajo de 250 ms a las 15:07)
**Detectado por:** el equipo de backoffice en `#oms-backoffice` ("la pantalla de pedidos tarda una eternidad"). No había ninguna alerta de latencia: solo existían alertas de error rate.
**On-call:** SRE de guardia (semana 25)
**Impacto:** ~4.500 `GET /api/orders*` en la franja, el 85 % por encima de 250 ms y el 60 % por encima de 800 ms (p95 de 1,9 s). **SLO 2 de Orders:** ~2.700 peticiones > 800 ms, el **24 % del budget del objetivo del 99 %** en una tarde. Además, 40 s de bloqueo de escrituras (ver abajo) provocaron 9 `POST /api/orders` con 503: el **13 % del budget del SLO 1**.

### Lo que ocurrió
La release del martes 16 añadió al backoffice el listado "pendientes de envío", que filtra `orders.orders` por `status` y ordena por `created_at`. No había índice para ese filtro: cada carga era un seq scan de 1,4 M filas. El turno de tarde de los almacenes (12 personas) abrió la pantalla, que se refresca cada 30 s. La CPU de Cloud SQL pasó del 30 % al 92 % y todas las consultas de pedidos, también las de los clientes en "Mis pedidos", se volvieron lentas.

### Cómo se mitigó (improvisado, sin runbook)
1. El on-call vio la latencia alta en `oms-orders` y forzó una revisión nueva del servicio para "reiniciarlo" (`--update-env-vars` con un valor cualquiera). No cambió nada: el problema no estaba en las instancias. Se perdieron 10 min.
2. Abrió la consola de Cloud SQL, vio la CPU al 92 % y buscó en Query Insights. A las 14:52 identificó la query del listado.
3. Un desarrollador de Orders creó el índice con `CREATE INDEX` **sin `CONCURRENTLY`**. La tabla quedó bloqueada para escritura durante 40 s y fallaron 9 checkouts.
4. Con el índice creado, la CPU bajó al 35 % y el p95 volvió a 140 ms a las 15:07.

### Aprendizajes y acciones
- **No teníamos alerta de latencia.** El backoffice se enteró 8 min antes que nosotros. → Crear `APILatencyP95High` (p95 > 250 ms, 10 min) y `APILatencyP99High`. *(Hecho, 18/06)*
- **"Reiniciar" fue la primera reacción y no servía:** ninguna instancia estaba mal. → El runbook empieza por **dónde** está la latencia (endpoint, servicio, BD), no por actuar.
- **Un índice en caliente también es un cambio de producción:** sin `CONCURRENTLY` bloqueó el checkout. → El runbook incluye el comando exacto y la aprobación del EM.
- No había forma de quitar la carga del backoffice sin tocar código. → `orders.read-from-replica` pasa a cubrir también el backoffice. *(Hecho, 25/06)*
- `pg_stat_statements` no estaba habilitado en `oms-pg-prod`. → `CREATE EXTENSION pg_stat_statements` + flag `pg_stat_statements.track = top`. *(Hecho, 19/06)*

### Versión 1 del runbook tras este post-mortem

> **Runbook v1 (texto literal):**
> "Si salta APILatencyP95High: mirar la CPU de Cloud SQL. Si está alta, buscar la query lenta en Query Insights
> y avisar al equipo del módulo. Si hace falta un índice, crearlo con CONCURRENTLY. No reiniciar el servicio."

(Cubre el caso de la query lenta y nada más. Por eso hay V2.)

---

## Versión 2: diagnóstico y comandos exactos (PM #2)

**Fecha del incidente:** miércoles 22 de julio de 2026, 11:04 → 11:36 CEST
**Duración del incidente:** 32 minutos (degradación desde 11:04; alerta critical a las 11:15; mitigado a las 11:21; verificado a las 11:36)
**Detectado por:** `APILatencyP95High` critical en `oms-catalog` / `/api/products/{id}` (p95 = 620 ms).
**On-call:** SRE de guardia (semana 30), otra persona distinta a la de PM #1.
**Impacto:** catálogo y búsqueda lentos durante 17 min (p95 de producto 620 ms, p99 2,8 s; búsqueda p95 900 ms). Sin errores. **No consumió budget de ningún SLO formal** (el SLO 2 es de pedidos y `oms-orders` no se vio afectado), pero incumplió NFR-PERF-001 y NFR-PERF-004, y las sesiones con búsqueda que acabaron en carrito bajaron un 18 % frente al miércoles anterior.

### Qué se vio durante esta segunda ocurrencia
La newsletter de rebajas de verano salió a las 11:00 y triplicó el tráfico del catálogo. `oms-catalog` escaló de 3 a 16 instancias. El runbook V1 mandaba mirar la CPU de Cloud SQL, que estaba al 35 %, y el on-call pasó 6 minutos en Query Insights sin encontrar nada. El problema estaba en otro sitio:
1. **Cold starts:** cada instancia nueva tardaba 4,2 s en arrancar y las peticiones que caían en ella esperaban. El p99 subió mucho más que el p95. El V1 no lo contemplaba.
2. **CPU de `oms-catalog` al 91 %:** las sugerencias de búsqueda (consultas de trigramas en cada pulsación de tecla) consumían el 40 % de la CPU del servicio.
3. El on-call no sabía qué flag existía para la búsqueda. Lo resolvió el segundo SRE de la rotación, al que llamó a las 11:18.

Mitigación: `flag_on catalog.search-suggestions-disabled` (CPU del 91 % al 58 % en 2 min) y `--min-instances=6` en `oms-catalog`.

### Qué cambió en el runbook (V1 → V2)
- **Diagnóstico por descarte con comandos copiables:** paso 0 (¿qué endpoint y qué servicio?), deploy reciente, tramo de la traza (Prometheus por tramo + Cloud Trace agrupado por span), cold starts, CPU de Cloud Run y, por último, la BD. Cada salida lleva a una acción o a otro runbook (01, 03, 07, 08, 12).
- **Acciones con flags primero** (`catalog.search-suggestions-disabled`, `catalog.serve-stale`, `orders.read-from-replica`), después `min-instances` y, como último recurso y con aprobación, el índice.
- **Cancelar la query por `query_id`** (`pg_cancel_backend`), sin terminar sesiones ni tocar las de `oms_ops`.
- **Alertas separadas:** `APILatencyP95High` pasa a warning (250 ms) y critical (500 ms); se añade `SearchLatencyHigh` (300 ms) y la burn rate del SLO 2 (`OrdersLatencyBurnRate`). El `runbook_url` lleva `job` y `endpoint` pre-rellenados.
- **Verificación medible:** p95 < 250 ms, p99 < 800 ms y burn rate < 1 durante 15 min.

### Acciones derivadas
- `min-instances=3` y startup CPU boost permanentes en `oms-catalog` (Terraform). Coste: ~90 €/mes. *(Hecho, 28/07)*
- Avisar en `#oms-releases` de cada envío de más de 20.000 emails (el acuerdo de PM #1 del runbook 01 no se estaba cumpliendo). *(Recordatorio a marketing)*
- Evaluar automatizar la Acción 1 cuando dispara `SearchLatencyHigh`: es barata, reversible y siempre la misma.
- Evitar que una query sin índice llegue a producción (lo que originó PM #1).

---

## Versión 3: automatización preventiva (PM #3)

**Fecha:** miércoles 23 de septiembre de 2026, campaña de las 11:00. **El incidente no llegó a producirse.**

### Qué pasó (o, mejor, qué NO pasó)
- **Lunes 21:** un MR añadía al backoffice el "export de pedidos del mes", con un filtro por `created_at` + `status` sin índice. El job de CI `query-plan-check` lo **bloqueó**: el `EXPLAIN` sobre la copia anonimizada de staging mostraba un seq scan de 1,4 M filas. El desarrollador añadió el índice a la migración y el MR pasó al día siguiente. Es el mismo patrón que PM #1, parado antes del deploy.
- **Miércoles 23, 11:00:** campaña con tráfico ×3 en el catálogo. Gracias a `min-instances=3` y al CPU boost, el p99 no pasó de 450 ms. La búsqueda subió a 340 ms de p95; a las 11:12 disparó `SearchLatencyHigh` y en 20 s `oms-ops-flag-automator` encendió `catalog.search-suggestions-disabled`. A las 11:15 el p95 de búsqueda estaba en 210 ms. El flag se apagó solo a las 11:52, 30 min después de resolverse la alerta. **No se paginó a nadie** y `APILatencyP95High` no llegó a disparar.

### Qué cambió en el runbook (V2 → V3)
- **La Acción 1 está automatizada:** `oms-ops-flag-automator` (Cloud Run, ingress interno, autenticado con OIDC) recibe los webhooks de Alertmanager. Tiene una lista cerrada de reglas `alerta → flag` (`automator-rules.yaml`): solo flags de impacto bajo y reversibles. Enciende el flag al disparar la alerta y lo apaga 30 min después de que se resuelva, como máximo un cambio por flag y hora (anti-flapping), y publica cada cambio en `#oms-alerts`. El runbook 12 reutiliza el mismo servicio.
- **Prevención en CI:** el job `query-plan-check` (etapa `test` de GitLab CI) aplica las migraciones del MR sobre `oms-pg-staging` (copia anonimizada semanal con estadísticas de producción), lanza `EXPLAIN` de las queries nuevas o modificadas y falla si hay un seq scan sobre una tabla de más de 100.000 filas. Se puede saltar con la etiqueta `query-plan-override` y la aprobación del tech lead.
- **La automatización se vigila:** Cloud Scheduler `oms-ops-flag-automator-selftest` llama cada 5 min a `/selftest` (lee un flag de Unleash y comprueba que Alertmanager le llega). Si va bien, publica `ops_job_last_success_timestamp_seconds{job="flag-automator"}`. `FlagAutomatorStale` avisa si pasan 15 min sin latido.
- El runbook manual **se mantiene** completo: la automatización cubre la búsqueda, pero la query lenta en caliente, la réplica y el índice siguen necesitando criterio humano.

### El runbook como automatización

- **Servicio:** Cloud Run `oms-ops-flag-automator` (repo `oms-ops`, reglas en `automator-rules.yaml`). **Owner:** equipo SRE.
- **Disparador:** webhook de Alertmanager (receiver `flag-automator`, filtra `automation="flag-automator"`).
- **Self-test:** `*/5 * * * *` (Cloud Scheduler `oms-ops-flag-automator-selftest`, zona `Europe/Madrid`).
- **Salud:** `ops_job_last_success_timestamp_seconds{job="flag-automator"}`, vigilada por `FlagAutomatorStale`.
- **Job de CI:** `query-plan-check`. **Owner:** equipo de Orders, con revisión de SRE.

### Métrica del runbook a lo largo de la evolución

| | V1 (manual) | V2 (comandos) | V3 (preventivo) |
|---|---|---|---|
| Veces disparado por incidente real | 1 | 1 | 0 (solo `SearchLatencyHigh`, resuelta sola) |
| Detección | Usuarios del backoffice (+8 min) | Alerta propia (critical) | Alerta warning + automatización en 20 s |
| Tiempo de mitigación | 47 min | 17 min (11:04 → 11:21) | 3 min, sin humano |
| % de peticiones > 800 ms en la franja | 60 % (pedidos, ~2.700) | ~8 % (catálogo) | < 0,5 % |
| Budget consumido | 24 % SLO 2 + 13 % SLO 1 | 0 % SLO formal (NFR-PERF-001/004 incumplidos) | 0 % |
| Toil del on-call | Alto (reinicio inútil, consola, índice con bloqueo) | Medio (diagnóstico en 6 pasos, flags) | Cero |
| Siguiente mejora | Diagnóstico completo y flags | Automatizar la búsqueda y parar las queries en CI | Capacity planning de Black Friday con el runbook 12 |

---

## Conclusión

Este runbook enseña que la latencia **no tiene una única causa** y que la primera reacción ("reinicia", "mira la BD") suele estar equivocada. En V1 reiniciamos un servicio sano y creamos un índice que bloqueó el checkout: actuar sin saber dónde estaba el tiempo nos costó más budget que el propio problema. En V2 el runbook pasó a ser un **árbol de descarte** (endpoint → deploy → tramo de la traza → cold starts → CPU → BD) con una acción concreta por rama, y las mitigaciones pasaron a ser flags. En V3 atacamos las dos causas que ya conocíamos en sitios distintos: la query lenta, en CI, antes de llegar a producción; la búsqueda cara, con un flag automático que se apaga solo. El aprendizaje incómodo es el mismo que en el runbook 01: **un automatismo que enciende flags tiene que tener su propia alerta** y una lista cerrada de lo que puede tocar. Si no, el día que falle nadie lo sabrá, y el día que haga demasiado nadie entenderá por qué el catálogo ha cambiado.
