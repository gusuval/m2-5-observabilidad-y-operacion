# Post-mortems del runbook: redis-cache-hit-ratio-low

> *Escenarios simulados para el trabajo del Bloque 5, construidos sobre la arquitectura real del OMS.*
> Evolución del runbook a lo largo de 3 ocurrencias del incidente: V1 manual → V2 con comandos → V3 automatizada.
> Formato blameless: se analizan sistemas y procesos, no personas. Los on-call se identifican por rol.

---

## Versión 1: mitigación manual (PM #1)

**Fecha del incidente:** martes 14 de abril de 2026, 11:00 → 12:25 CEST
**Duración del incidente:** 85 minutos (inicio del import → hit ratio > 90 %)
**Detectado por:** alerta genérica de latencia del catálogo (p95 > 250 ms) a las 11:22. No existía ninguna alerta sobre el hit ratio.
**On-call:** SRE de guardia (semana 16)
**Impacto:**
- Catálogo: hit ratio del 93 % al **35 %**; p95 de `GET /api/products` de 38 a **310 ms** durante 70 min (NFR-PERF-001 incumplido). Sin 5xx.
- Postgres: CPU de `oms-pg-prod` al 81 %. Por la CPU compartida, el p95 de `GET /api/orders` subió a 270 ms durante 25 min: **~9 % del budget de latencia del SLO 2 de Orders**.
- Precios: el import se cortó a medias y durante 2 h convivieron precios nuevos y viejos en el catálogo (el checkout cobraba siempre el precio de Postgres, coherente con lo que mostraba la ficha tras la invalidación).

### Lo que ocurrió
El backoffice lanzó el import de precios de primavera: 12.000 productos en lotes. Cada producto emitía un `ProductUpdated`, que invalidaba su clave **y todos los listings de su categoría** (ADR-005 §3). Con ~40 categorías tocadas cientos de veces por minuto, los listings no vivían más de unos segundos en Redis y casi todas las lecturas iban a Postgres.

### Cómo se mitigó (improvisado, sin runbook)
El on-call abrió Memorystore en la consola: instancia `READY`, memoria al 60 %, sin errores. Al no ver nada raro, revisó Cloud Run y Cloud SQL durante ~25 min sin encontrar la causa. A las 11:55 preguntó en `#oms-sre`; alguien relacionó la hora con el import. Se pidió por Slack en `#oms-backoffice` que lo parasen y, como el importador no tenía forma de pausarse, se **mató el proceso** a las 12:02. La caché se recuperó sola en ~20 min.

### Aprendizajes y acciones
- **No había alerta de hit ratio** y la latencia avisó 22 min tarde. → `RedisCacheHitRatioLow` (< 80 % durante 15 min). *(Hecho, 16/04)*
- **Nadie podía ver las invalidaciones.** → Métrica `cache_invalidations_total{cache, reason}` y alerta informativa `CatalogBulkInvalidationInProgress`. *(Hecho, sprint 17)*
- **El import no se podía pausar**: matarlo dejó precios a medias. → Flag `catalog.bulk-import-paused` (el importador lo comprueba antes de cada lote) y modo `--batch-invalidate` (una invalidación por categoría al final). *(Hecho, sprint 18)*
- Los imports grandes se avisan en `#oms-releases` y se lanzan fuera de hora punta.

### Versión 1 del runbook tras este post-mortem

> **Runbook v1 (texto literal):**
> "Si dispara RedisCacheHitRatioLow: comprobar si hay un import del backoffice en marcha (preguntar en #oms-backoffice) y pedir que se pause.
> Mirar memoria y evictions de Redis en Grafana. Si hubo un deploy reciente, puede ser el arranque en frío por cambio de prefijo: esperar 30 min."

(Tres causas, ninguna forma de distinguirlas con datos. Por eso hay V2.)

---

## Versión 2: diagnóstico y comandos exactos (PM #2)

**Fecha del incidente:** martes 21 de julio de 2026, 10:05 → 10:41 CEST
**Duración del incidente:** 36 minutos (deploy 10:05 → alerta 10:20 → rollback 10:36 → hit ratio > 90 % a las 10:41)
**Detectado por:** `RedisCacheHitRatioLow` a las 10:20. Respuesta del on-call a las 10:22.
**On-call:** SRE de guardia (semana 30), otra persona distinta a la de PM #1.
**Impacto:** hit ratio al **22 %**, p95 del catálogo a 260 ms durante 30 min, CPU de Cloud SQL al 74 %. Sin 5xx. ~4 % del budget de latencia del SLO 2 de Orders.

### Qué se vio durante esta segunda ocurrencia
La revisión `oms-catalog-00087` traía dos cambios: un prefijo de clave nuevo (`catalog:v3:` → `catalog:v4:`, arranque en frío previsto) y una librería de configuración nueva que leía `CATALOG_CACHE_TTL_LISTING_SECONDS=300` **como milisegundos**. El canary (10 % del tráfico) no lo paró porque su análisis solo miraba 5xx y latencia, y un 10 % de misses apenas movía el p95 global.
1. El runbook V1 decía "si hubo deploy, esperar 30 min". El on-call esperó 10 min convencido de que era el prefijo, pero el ratio no subía.
2. No había forma de ver el TTL real de las claves. Montar `redis-cli` con TLS y AUTH desde el bastión le llevó ~6 min.
3. Con `TTL` en 0–1 s en todas las claves de listing, revirtió el tráfico a la revisión anterior a las 10:36.
4. Las causas posibles (import, deploy, memoria) se miraban de oído y en el orden en que se le ocurrían al on-call.

### Qué cambió en el runbook (V1 → V2)
- **Diagnóstico por pasos y por datos**: hit ratio por `key_type` y por `revision`, `cache_invalidations_total` por `reason`, memoria y evictions, y una tabla de causas por orden de frecuencia.
- **Comandos de Redis copiables** con el wrapper `oms-redis-cli` del bastión: `--scan` de prefijos, `TTL` de una muestra y `INFO keyspace` (`avg_ttl`).
- **Distinguir prefijo nuevo de TTL roto**: con un prefijo nuevo, las claves tienen TTL normal y el ratio sube en minutos; con el TTL roto, las claves duran segundos y el ratio no se recupera nunca.
- **Acciones por causa**, cada una idempotente: `serve-stale` (y para qué causas no sirve), throttle al bot en Cloud Armor, pre-calentado, pausa del import por flag, corrección del TTL o rollback, y escalado de memoria con aprobación.

### Acciones derivadas
- El análisis del canary ([runbook 07](../07-canary-rollout-failed/runbook.md)) compara el hit ratio de la revisión nueva con el de la estable. *(Hecho, sprint 31)*
- Test de configuración que falla si un TTL de caché es < 10 s. *(Hecho)*
- Durante la revisión se vio la memoria al 88 % mientras convivían `v3` y `v4`. → Memorystore ampliado de 5 a 8 GB el 4 de agosto. *(Hecho)*
- Cloud Run Job `oms-catalog-cache-warmer` para pre-calentar el top 2.000. *(Hecho, sprint 32)*

---

## Versión 3: prevención (PM #3)

**Fecha:** martes 27 de octubre de 2026. **El incidente no llegó a producirse** (dos veces el mismo día).

### Qué pasó (o, mejor, qué NO pasó)
- **10:30, deploy con prefijo nuevo** (`catalog:v4:` → `catalog:v5:`): el pipeline ejecutó `oms-catalog-cache-warmer --key-version=v5` **antes** de mover tráfico a la revisión nueva. El hit ratio mínimo fue del 84 % y volvió al 91 % en 8 min. Ninguna alerta.
- **21:10, scraper** recorriendo el long tail del catálogo desde un rango de un proveedor de hosting: la regla permanente de Cloud Armor (throttle de 300 peticiones/min por IP en `/api/products/`) lo frenó desde el primer minuto. El hit ratio bajó del 93 % al 86 % durante 20 min, sin evictions. No saltó `RedisCacheHitRatioLow` (umbral 80 %). Al día siguiente se revisó el dashboard y se añadió una regla específica para ese rango.

### Qué cambió en el runbook (V2 → V3)
- **El pre-calentado es automático** en tres momentos: a diario a las 05:30 (Cloud Scheduler), como paso del pipeline antes de mover tráfico a una revisión nueva de `oms-catalog` y al terminar cada import. La Acción 3 manual queda para casos fuera de esos tres.
- **El import ya no vacía la caché**: `--batch-invalidate` por defecto y el flag `catalog.bulk-import-paused` como freno.
- **Throttle permanente contra scraping** en `oms-edge-policy`, desplegado por Terraform y probado 24 h en modo `--preview`.
- **`serve-stale` automático**: `oms-ops-flag-automator` (el mismo del [runbook 05](../05-redis-cache-down/post-mortems.md#versión-3-degradación-automática-pm-3)) enciende `catalog.serve-stale` si `RedisCacheHitRatioLow` coincide con p95 del catálogo > 150 ms, y lo apaga tras 30 min con el ratio > 90 %.
- Nueva alerta `CatalogCacheWarmerStale`: si el pre-calentado deja de ejecutarse, nos enteramos al día siguiente y no en el próximo deploy.

### El runbook como automatización

- **Job:** Cloud Run Job `oms-catalog-cache-warmer` (`--top=2000`, lee de `oms-pg-prod-replica` con `DB_POOL_SIZE=2`). Idempotente: reescribir una clave solo renueva su TTL.
- **Disparadores:** Cloud Scheduler `oms-catalog-cache-warmer-daily` (`30 5 * * *`, zona `Europe/Madrid`), paso `warm-cache` del pipeline de `oms-catalog` (antes de `update-traffic`) y llamada del importador al terminar.
- **Salud:** `ops_job_last_success_timestamp_seconds{job="catalog-cache-warmer"}`, vigilada por `CatalogCacheWarmerStale` (> 26 h).
- **Owner:** equipo Catalog (el job) y equipo SRE (disparadores y alerta).

### Métrica del runbook a lo largo de la evolución

| | V1 (manual) | V2 (comandos) | V3 (preventivo) |
|---|---|---|---|
| Detección | Latencia, 22 min tarde | `RedisCacheHitRatioLow` (15 min) | Sin alerta: no bajó del 80 % |
| Tiempo hasta dar con la causa | ~55 min (preguntando en Slack) | ~14 min (con 10 min de espera innecesaria) | — |
| Hit ratio mínimo | 35 % | 22 % | 84 % |
| Duración con p95 > 150 ms | 70 min | 30 min | 0 |
| Budget de latencia del SLO 2 de Orders | ~9 % | ~4 % | 0 % |
| Toil del on-call | Alto (sin datos, matar un proceso) | Medio (comandos por causa) | Cero |
| Siguiente mejora | Alerta, métrica de invalidaciones, import pausable | Pre-calentado y canary con hit ratio | Dimensionar Memorystore para el ×5 de Black Friday |

---

## Conclusión

El hit ratio es la avería más traicionera de la caché porque **no rompe nada**: no hay errores, solo un catálogo más lento y un Postgres más cargado, y por eso en V1 tardamos casi una hora en dar con la causa preguntando en Slack. V2 convirtió la intuición en datos: el ratio por `key_type` y por revisión, las invalidaciones por motivo y el `TTL` real de las claves separan las cinco causas en minutos, y se vio que "esperar al arranque en frío" no es una acción si no sabes distinguirlo de un TTL roto. En V3 dejamos de reaccionar: la caché se calienta **antes** de que una revisión nueva reciba tráfico, el import ya no la vacía y los bots chocan con Cloud Armor. Dos ocurrencias que en primavera habrían sido incidentes se quedaron en una línea del dashboard. Lo pendiente es de capacidad: con el pico ×5 de Black Friday, 8 GB pueden no bastar, y la alerta `RedisMemoryPressure` nos dará la señal con tiempo para escalar fuera de la hora punta.
