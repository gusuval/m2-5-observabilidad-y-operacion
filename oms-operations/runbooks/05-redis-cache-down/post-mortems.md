# Post-mortems del runbook: redis-cache-down

> *Escenarios simulados para el trabajo del Bloque 5, construidos sobre la arquitectura real del OMS.*
> Evolución del runbook a lo largo de 3 ocurrencias del incidente: V1 manual → V2 con comandos → V3 automatizada.
> Formato blameless: se analizan sistemas y procesos, no personas. Los on-call se identifican por rol.

---

## Versión 1: mitigación manual (PM #1)

**Fecha del incidente:** martes 10 de febrero de 2026, 20:35 → 21:41 CET (pico de tráfico de la tarde)
**Duración del incidente:** 66 minutos
**Detectado por:** la alerta genérica de error rate > 5 % a las 20:44 y tickets de soporte ("se me ha vaciado el carrito"). No existía ninguna alerta sobre Redis.
**On-call:** SRE de guardia (semana 7)
**Impacto:**
- Catálogo: ~25 % de 5xx durante 38 min → **9,5 min equivalentes, el 43 % del budget mensual de NFR-AVAIL-002** (22 min).
- Checkout: 31 `POST /api/orders` con 5xx por la saturación del primario → **44 % del budget del SLO 1 de Orders** (70 peticiones / 28 d). Quedó un 56 % (🟢, al límite del 🟡).
- Carrito: ~1.400 operaciones fallidas y ~260 clientes que vieron "tu carrito está vacío" (el carrito solo existía en Redis). ~90 tickets de soporte.

### Lo que ocurrió
El nodo primario de `oms-redis-prod` falló a las 20:35 y Memorystore hizo un failover automático en ~70 s. Ese failover era sano, pero el cliente Redis de la app no tenía circuit breaker, usaba `commandTimeout` de 2 s con 3 reintentos y no tenía keepalive TCP: las conexiones medio abiertas se quedaron colgadas. Cada petición del catálogo esperaba hasta 6 s antes de tratar el error como miss (ADR-005 §6). Cloud Run escaló `oms-catalog` a 20 instancias con las conexiones igual de colgadas.

### Cómo se mitigó (improvisado, sin runbook)
El on-call no relacionó los 5xx con Redis hasta las 21:02 (vio `cache_miss_due_to_error` en los logs). En la consola, Memorystore estaba `READY` desde las 20:37. Decidió redesplegar `oms-catalog` (21:05) para "refrescar conexiones". Las instancias nuevas se conectaron bien, pero con la caché sin calentar y sin single-flight: el 100 % de las lecturas fueron a Postgres a la vez. La CPU de Cloud SQL llegó al 97 %, el pool de `oms-catalog` se saturó y el primario, compartido, empezó a fallar también en el checkout. La caché se fue llenando sola y a las 21:41 todo volvió a la normalidad. Nadie tomó una acción que acortara la segunda mitad.

### Aprendizajes y acciones
- **No había alerta de Redis.** → `RedisCacheDown` con las dos señales (`redis_up` y errores de la app). *(Hecho, 12/02)*
- **El adapter tardaba 6 s en declarar un miss.** → Timeout de 50 ms, circuit breaker (`circuit_breaker_state{breaker="redis"}`), keepalive TCP y reconexión ante `READONLY`. *(Hecho, sprint 8)*
- **Redesplegar "para refrescar" convirtió un problema de conexiones en una estampida.** → Single-flight por clave en el read-through (ya lo pedía ADR-005, no estaba implementado). *(Hecho, sprint 8)*
- **El carrito solo vivía en Redis.** → La SPA guarda siempre una copia en `localStorage`; `POST /api/orders` recibe las líneas en el cuerpo. *(Hecho, sprint 9)*

### Versión 1 del runbook tras este post-mortem

> **Runbook v1 (texto literal):**
> "Si dispara RedisCacheDown: mirar el estado de `oms-redis-prod` en la consola de Memorystore. Si está en failover, esperar.
> No redesplegar `oms-catalog` salvo que la consola diga READY y sigan los errores. Vigilar la CPU de Cloud SQL."

(Dice **qué mirar**, pero no **cómo**, ni qué hacer si Postgres no aguanta. Por eso hay V2.)

---

## Versión 2: diagnóstico y comandos exactos (PM #2)

**Fecha del incidente:** martes 16 de junio de 2026, 13:20 → 13:44 CEST
**Duración del incidente:** 24 minutos (12 min sin Redis + 4 min de estampida al volver + verificación)
**Detectado por:** `RedisCacheDown` (`trigger="redis_up"`) a las 13:21. Respuesta del on-call a las 13:23.
**On-call:** SRE de guardia (semana 25), otra persona distinta a la de PM #1.
**Impacto:** 0 errores durante los 12 min sin Redis (el breaker funcionó; p95 del catálogo de 35 a 190 ms). Al volver Redis: 6 % de 5xx en el catálogo durante 4 min → 0,24 min equivalentes (**1 % del budget** de NFR-AVAIL-002) y 3 `POST /api/orders` fallidos (**4 % del budget** de Orders). Carritos: el dataset volvió vacío, pero la SPA resincronizó desde `localStorage` el 94 % en la siguiente visita.

### Qué se vio durante esta segunda ocurrencia
Google puso la instancia en `REPAIRING` tras un fallo del nodo y la reparación duró 12 min. Las mejoras del V1 hicieron su trabajo, pero el runbook se quedó corto:
1. "Mirar el estado en la consola" no distinguía mantenimiento, reparación o fallo. El on-call no sabía si esperar o actuar, y perdió ~5 min buscando el historial de operaciones.
2. A las 13:29 propuso un **failover manual con la instancia en `REPAIRING`**. Un compañero de `#oms-sre` le paró: habría fallado o habría alargado la reparación.
3. Durante los 12 min, el breaker oscilaba (semiabierto cada 30 s) y la CPU de Cloud SQL llegó al 82 %, sin ningún freno: el fallback a Postgres **no tenía rate limit**.
4. Al volver Redis (13:32), los breakers de las 9 instancias se cerraron a la vez contra una caché **vacía**: 100 % de misses, pool de `oms-catalog` saturado (`DBConnectionPoolSaturated` saltó a las 13:33) y 5xx durante 4 min.
5. No había forma de entrar a Redis con `redis-cli` (TLS + AUTH) para comprobar `PING` o la memoria.

### Qué cambió en el runbook (V1 → V2)
- **Comandos exactos**: `gcloud redis instances describe` (campo `state`), `gcloud redis operations list`, logs del recurso y `oms-redis-cli` en el bastión (`PING`, `INFO`) con una tabla de salida → significado → siguiente paso.
- **Regla explícita**: no hay failover manual con `FAILING_OVER`/`MAINTENANCE`/`REPAIRING`, y el failover lleva una **guarda de zona** para ser idempotente.
- **Bypass como feature flag con rate limit propio** (`catalog.cache-bypass`, 25 lecturas/s por instancia, `429` con `Retry-After`) en lugar de depender solo del breaker. **Carrito local forzado** con `cart.local-only`.
- **Proteger Postgres** como acción propia: `orders.batch-jobs-paused` y el flag nuevo `catalog.read-from-replica`.
- **Warm-up controlado al recuperar**: Cloud Run Job `oms-catalog-cache-warmer` y apagado escalonado del bypass (50 % → 25 % → OFF) con `flag_rollout`, vigilando la CPU de Cloud SQL.
- Explicado **cuándo `catalog.serve-stale` sí aplica** (Redis lee pero no escribe, o Postgres caído) y cuándo no (Redis caído: no hay nada stale que servir).

### Acciones derivadas
- Warning `RedisCacheErrorsElevated` para los errores parciales (OOM, `READONLY`). *(Hecho)*
- Alerta `CatalogCacheBypassLeftOn`: en un simulacro interno de julio el bypass se quedó en ON 3 días después de terminar. *(Hecho)*
- Evaluar que la degradación la active una automatización por webhook, no el on-call.

---

## Versión 3: degradación automática (PM #3)

**Fecha:** martes 22 de septiembre de 2026, 11:40 → 11:47 CEST (Redis sin servicio) · verificado a las 12:12. **No hubo impacto en el usuario.**

### Qué pasó (o, mejor, qué NO pasó)
Un problema de red en la zona europe-west3-a dejó a `oms-redis-prod` sin servicio 7 min (failover lento por el propio incidente zonal). Saltó `RedisCacheDown` a las 11:41, y la misma alerta llegó por webhook a `oms-ops-flag-automator`, que en 75 s encendió `catalog.cache-bypass` y `cart.local-only` y, con el lag de la réplica en 0,4 s, `catalog.read-from-replica`. El p95 del catálogo subió de 35 a 120 ms, la CPU del primario no pasó del 41 % y **no hubo ni un 5xx** en el catálogo ni en el checkout. Cuando Redis llevó 5 min estable, el automatizador lanzó el warm-up y bajó el bypass en escalones de 5 min. El on-call, paginado, solo verificó. Consumo de budget: **0 %**.

### Qué cambió en el runbook (V2 → V3)
- **Las Acciones 1 y 2b están automatizadas** en `oms-ops-flag-automator`. El runbook empieza por "comprueba que el automatizador actuó" y el on-call se centra en la causa.
- **La recuperación también está automatizada**: warm-up + apagado escalonado con guardas (CPU de Cloud SQL < 70 % y p95 < 150 ms en cada escalón; si no, vuelve al anterior).
- **Prevención**: un Cloud Scheduler semanal publica en `#oms-sre` el próximo `maintenanceSchedule` de Memorystore. Si cae en una campaña o en Black Friday, se reprograma con `gcloud redis instances reschedule-maintenance`.
- Alerta `FlagAutomatorStale` (compartida, `observability/ops-automation-alerts.yaml`): si el automatizador muere, nos enteramos antes de la siguiente caída.
- `RedisCacheDown` **se mantiene critical**: perder Redis es perder redundancia y la automatización necesita supervisión humana. Lo que cambia es el trabajo del on-call: verificar, no ejecutar.

### El runbook como automatización

- **Servicio:** Cloud Run `oms-ops-flag-automator` (ingress interno, autenticación por token de Alertmanager), repo `oms-ops-automation`.
- **Disparador:** receiver `flag-automator` de Alertmanager para `alertname="RedisCacheDown"` (firing y resolved).
- **Qué hace:** firing → `flag_on catalog.cache-bypass`, `flag_on cart.local-only` y `flag_on catalog.read-from-replica` si el lag < 5 s; publica en `#oms-alerts`. Resolved + 5 min estable → `oms-catalog-cache-warmer` → rollout 50 % → 25 % → OFF con guardas → apaga los otros dos flags. Es idempotente (lee el estado del flag antes de cambiarlo) y nunca toca Memorystore: failover y recreación siguen siendo decisiones humanas.
- **Salud:** self-test cada 5 min (Cloud Scheduler `oms-ops-flag-automator-selftest`, simula la alerta contra Unleash de staging) → `ops_job_last_success_timestamp_seconds{job="flag-automator"}`, vigilada por `FlagAutomatorStale`.
- **Owner:** equipo SRE.

### Métrica del runbook a lo largo de la evolución

| | V1 (manual) | V2 (comandos) | V3 (automatizado) |
|---|---|---|---|
| Detección | Alerta genérica + clientes (+9 min) | `RedisCacheDown` (1 min) | `RedisCacheDown` + webhook (1 min) |
| Tiempo hasta degradación controlada | Nunca (estampida) | ~14 min (manual, sin rate limit) | 75 s (automática) |
| Duración del impacto en el usuario | 66 min | 4 min | 0 |
| 5xx del catálogo (min equivalentes / % budget NFR-AVAIL-002) | 9,5 min / 43 % | 0,24 min / 1 % | 0 / 0 % |
| `POST /api/orders` fallidos (% budget Orders) | 31 (44 %) | 3 (4 %) | 0 (0 %) |
| Carritos afectados | ~260 vacíos | 6 % sin resincronizar | 0 |
| Toil del on-call | Alto (adivinar, redesplegar) | Medio (copiar comandos, escalones a mano) | Bajo (verificar) |
| Siguiente mejora | Breaker, single-flight, carrito local | Automatizar degradación y recuperación | Reprogramar mantenimiento antes de Black Friday |

---

## Conclusión

Este runbook enseña que **degradación elegante no es lo mismo que "el código tiene un try/catch"**. En V1 la degradación existía en el ADR, pero un timeout de 2 s y un redespliegue bienintencionado convirtieron un failover de 70 s en una hora de errores, y el carrito, que solo vivía en Redis, desapareció delante del cliente. En V2 el breaker y el carrito en `localStorage` funcionaron; lo que falló fue el **regreso**: una caché vacía recibiendo el 100 % del tráfico a la vez. De ahí sale la idea que más vale de todo el runbook: **apagar un bypass también es un cambio de producción** y se hace por escalones. En V3 el on-call ya no improvisa ni copia comandos: la degradación y la recuperación las ejecuta una automatización con guardas y con su propia alerta de vida, y las decisiones irreversibles (failover, recrear la instancia) siguen siendo humanas. Lo pendiente no es operativo: con el pico ×5 de Black Friday, el fallback a Postgres no aguantaría ni con la réplica, y eso obliga a revisar si `oms-redis-prod` necesita réplicas de lectura o una segunda capa de caché en el CDN para los listados públicos (re-evaluación de ADR-005).
