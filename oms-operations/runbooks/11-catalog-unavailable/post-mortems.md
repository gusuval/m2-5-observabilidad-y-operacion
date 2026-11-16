# Post-mortems del runbook: catalog-unavailable

> *Escenarios simulados para el trabajo del Bloque 5, construidos sobre la arquitectura real del OMS.*
> Evolución del runbook a lo largo de 3 ocurrencias del incidente: V1 manual → V2 con comandos → V3 automatizada.
> Formato blameless: se analizan sistemas y procesos, no personas. Los on-call se identifican por rol.

---

## Versión 1: mitigación manual (PM #1)

**Fecha del incidente:** martes 3 de marzo de 2026, 11:20 → 12:31 CET
**Duración del incidente:** 71 minutos
**Detectado por:** tickets de atención al cliente a partir de las 11:48 ("la web me dice acceso denegado"). **Ninguna alerta saltó**: la probe desde europe-west1 seguía en verde y la app no registró ni un 5xx.
**On-call:** SRE de guardia (semana 10)
**Impacto:** ~22 % de los clientes en España (los de un ISP concreto) recibieron **403** en toda la tienda durante 71 min. Unos **35 pedidos perdidos** según la comparación con el martes anterior. Budget de NFR-AVAIL-002 consumido según el SLI: **0 %**. Si los 403 hubieran contado: 22 % × 71 min ≈ **15,6 min equivalentes, el 71 % del budget mensual**.

### Lo que ocurrió
A las 11:15, para frenar a un scraper que estaba vaciando la caché del catálogo, se añadió desde la consola una regla `deny(403)` en `oms-edge-policy` con un rango demasiado amplio: un /12 que, además del proveedor del bot, incluía clientes residenciales de un ISP español. La regla se aplicó en el edge en ~5 min.

### Cómo se mitigó (improvisado, sin runbook)
El on-call recibió el aviso de soporte a las 11:50. Como no había 5xx, revisó Cloud Run, Redis y Postgres (todo sano) durante ~25 min. A las 12:18, desde el portátil personal con ese ISP, otra persona de `#oms-sre` reprodujo el 403 y relacionó la hora con el cambio de Armor. La regla **se borró** desde la consola a las 12:24. A las 12:31 se confirmó con clientes. Esa misma tarde el scraper volvió y el hit ratio de la caché cayó (ver [runbook 08](../08-redis-cache-hit-ratio-low/runbook.md)).

### Aprendizajes y acciones
- **El SLI no vio el incidente**: un 403 del edge no es un 5xx. → Métrica `loadbalancer_requests_total{backend_service, code}` y alerta `CatalogEdgeDenySpike`. *(Hecho, 05/03)*
- **La probe desde una IP de GCP no representa a los clientes.** → La verificación del runbook exige probar también desde otra red (móvil).
- **Cambios de Cloud Armor desde la consola, sin revisión.** → Solo por Terraform, con la regla 24 h en `--preview` antes de aplicarla. *(Hecho, sprint 11)*
- **Borrar la regla quitó también la protección.** → Desactivar = `--preview`, nunca `delete`.

### Versión 1 del runbook tras este post-mortem

> **Runbook v1 (texto literal):**
> "Si el catálogo no carga: comprobar en orden CDN, Load Balancer, Cloud Armor (reglas añadidas en las últimas 24 h), Cloud Run `oms-catalog`, Redis y Postgres.
> Desactivar cualquier regla de Armor reciente. Avisar a soporte."

(Una lista de capas sin comandos y sin forma de saber por dónde empezar. Por eso hay V2.)

---

## Versión 2: diagnóstico y comandos exactos (PM #2)

**Fecha del incidente:** martes 7 de julio de 2026, 10:05 → 10:32 CEST
**Duración del incidente:** 27 minutos
**Detectado por:** `CatalogUnavailable` (`trigger="5xx"`) a las 10:09 y, un minuto después, `trigger="probe"`. Respuesta del on-call a las 10:10.
**On-call:** SRE de guardia (semana 28), otra persona distinta a la de PM #1.
**Impacto:** 15 % de 5xx de media en el catálogo durante 21 min → **3,2 min equivalentes, el 14 % del budget mensual** de NFR-AVAIL-002. Sin impacto en `POST /api/orders`.

### Qué se vio durante esta segunda ocurrencia
Un nodo de Elasticsearch llegó al 95 % de disco (flood stage) y el clúster pasó a reubicar shards. Las búsquedas tardaban 5–8 s. El cliente de búsqueda de la app tenía un timeout de 10 s y **ningún límite de concurrencia**: las peticiones a `/api/search` ocuparon las 80 peticiones concurrentes de cada instancia de `oms-catalog`, y los listados, que no tienen nada que ver con la búsqueda, se quedaron en cola hasta el timeout.
1. El runbook V1 mandaba revisar las capas en orden. El on-call pasó **8 min** comprobando CDN, LB y Armor, que estaban bien.
2. En Cloud Run vio la CPU al 90 % y las instancias en 20/20, lo interpretó como falta de capacidad y subió `max-instances` a 30. Las instancias nuevas se llenaron igual de búsquedas colgadas y el pool de BD subió sin motivo.
3. A las 10:22 miró los 5xx por endpoint: casi todos venían de peticiones que esperaban detrás de `/api/search`. A las 10:24 activó `catalog.search-suggestions-disabled`; la carga de búsqueda bajó un 60 % y el catálogo se recuperó a las 10:32.

### Qué cambió en el runbook (V1 → V2)
- **"Primero averigua de dónde sale el error"**: paso 0 con `curl` y `openssl` desde fuera, y paso 1 con los `statusDetails` del LB y los 5xx por endpoint. Cada salida lleva directamente a una capa.
- **Comandos copiables por capa**: Armor (DENY por prioridad + audit log), certificado (`domainStatus`), SPA (bundles que dan 404), Cloud Run (revisión, CPU, instancias), dependencias (Redis, Postgres, `_cluster/health` de Elasticsearch).
- **Mitigaciones como flags antes que cambios de infraestructura**: `catalog.serve-stale`, `catalog.search-suggestions-disabled` y el nuevo `catalog.search-fallback-postgres` (búsqueda básica por nombre con `pg_trgm`).
- **Acciones de edge reversibles**: regla de Armor a `--preview`, SPA restaurada desde `oms-production-spa-releases` con `index.html` al final, `serve-while-stale` en el CDN, e invalidación **solo** de `/index.html` y **solo** con el origen sano.
- **Comunicación** con plantillas y tiempos (10 min → atención al cliente).

### Acciones derivadas
- **Bulkhead de búsqueda** en `oms-catalog`: timeout de 800 ms y máximo 10 búsquedas concurrentes por instancia. Lo que excede recibe 503 inmediato y la SPA muestra "búsqueda no disponible, navega por categorías". *(Hecho, sprint 29)*
- `max-instances` de `oms-catalog` devuelto a 20 (fórmula de capacidad del pool). *(Hecho, el mismo día)*
- Alerta de disco de los nodos de `oms-search` al 80 % en Cloud Monitoring. *(Hecho)*

---

## Versión 3: degradación automática y prevención en el edge (PM #3)

**Fecha:** martes 6 de octubre de 2026, 16:12 → 16:18 CEST. **`CatalogUnavailable` no llegó a dispararse.**

### Qué pasó (o, mejor, qué NO pasó)
Un evento de mantenimiento de host de GCE reinició un nodo de `oms-search` y el clúster quedó en `red` 6 min mientras recuperaba un shard primario. El bulkhead cortó las búsquedas a los 800 ms sin bloquear a los listados. `SearchLatencyHigh` ([runbook 02](../02-api-p95-high/runbook.md)) llegó por webhook a `oms-ops-flag-automator`, que activó `catalog.search-suggestions-disabled` y, con el clúster en `red`, `catalog.search-fallback-postgres`. Los 5xx del catálogo no pasaron del 0,4 % durante 6 min (≈ 0,02 min equivalentes, **0,1 % del budget**), por debajo del umbral del 2 %. Con el clúster en `green` durante 15 min, el automatizador apagó los dos flags.

Además, el 15 de septiembre el modo preview **evitó una repetición de PM #1**: una regla nueva contra un bot habría bloqueado el 9 % del tráfico legítimo. Se vio en los logs de preview y se corrigió antes de aplicarla.

### Qué cambió en el runbook (V2 → V3)
- **La Acción 1b está automatizada** para la búsqueda (webhook de `SearchLatencyHigh` → flags, con apagado automático). Un fallo de Elasticsearch ya no tumba el catálogo; como mucho, empeora la búsqueda.
- **Prevención en el edge**: las reglas de Cloud Armor pasan por un job de pipeline que las aplica 24 h en `--preview` y bloquea el merge si la regla habría denegado > 1 % del tráfico de España. Alerta `CatalogTLSCertExpiringSoon` (< 14 días) para no descubrir un certificado caducado por los clientes.
- **La probe también se vigila**: `CatalogProbeMissing`. Si la probe muere, la regla `trigger="probe"` no puede disparar.
- El triage end-to-end se mantiene: los fallos que quedan (revisión defectuosa, Postgres caído, CDN) siguen necesitando a una persona, pero el paso 1 lleva a la capa en < 2 min.

### El runbook como automatización

- **Servicio:** Cloud Run `oms-ops-flag-automator` (el mismo que en el [runbook 05](../05-redis-cache-down/post-mortems.md)).
- **Disparador:** receiver de Alertmanager para `alertname="SearchLatencyHigh"`; consulta `/_cluster/health` antes de activar el fallback a Postgres, y comprueba que el pool de `oms-catalog` está < 75 %.
- **Prevención en el edge:** job `armor-preview-check` del pipeline de `infra/` (Cloud Build), que lee 24 h de `enforcedSecurityPolicy.outcome="DENY"` en preview para la regla nueva.
- **Salud:** `FlagAutomatorStale` (self-test del automatizador) y `CatalogProbeMissing` (la probe).
- **Owner:** equipo SRE (automatizador y pipeline de edge) y equipo Catalog (bulkhead y fallback de búsqueda).

### Métrica del runbook a lo largo de la evolución

| | V1 (manual) | V2 (comandos) | V3 (automatizado / preventivo) |
|---|---|---|---|
| Causa | Regla de Cloud Armor | Elasticsearch sin bulkhead | Elasticsearch (contenido) |
| Detección | Clientes, 28 min tarde | `CatalogUnavailable` (4 min) | `SearchLatencyHigh` + webhook, sin página |
| Tiempo hasta localizar la capa | ~60 min | ~17 min (8 min revisando capas sanas) | < 1 min (automático) |
| Duración del impacto | 71 min | 27 min | 6 min, solo en la búsqueda |
| Budget de NFR-AVAIL-002 | 0 % según el SLI (71 % real) | 14 % | 0,1 % |
| Toil del on-call | Alto (adivinar) | Medio (árbol con comandos) | Bajo (verificar) |
| Siguiente mejora | Ver lo que ve el cliente | Aislar dependencias | DR del catálogo en europe-west1 |

---

## Conclusión

Este runbook no arregla nada por sí mismo; su valor es **llegar en dos minutos a la capa que falla**. En V1 el problema estaba en el edge y no lo veíamos: el SLI decía 100 % mientras un cliente de cada cinco recibía un 403. La lección fue incómoda: **un SLI que solo cuenta 5xx no mide la experiencia del usuario**, y una probe desde GCP no es un cliente real. En V2 vimos el error contrario: revisar las capas de arriba abajo "por si acaso" costó 8 minutos de un budget de 22 al mes, y subir instancias sin entender la causa solo multiplicó el problema. De ahí sale la regla del runbook: primero el `statusDetails` del LB y los 5xx por endpoint, después la capa. En V3 las dependencias que pueden fallar sin tirar el catálogo (la búsqueda) están aisladas con un bulkhead y una degradación automática, y los cambios que más daño hicieron (Armor) pasan por un preview que los prueba contra tráfico real antes de aplicarlos. Lo pendiente es estructural: el catálogo vive en una sola región y una caída regional de europe-west3 sigue costando más de 22 minutos. Eso es una decisión de arquitectura (DR activo del catálogo en europe-west1) y no de runbook.
