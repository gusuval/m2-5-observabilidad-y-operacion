# Post-mortems del runbook: server-cpu-high

> *Escenarios simulados para el trabajo del Bloque 5, construidos sobre la arquitectura real del OMS.*
> Evolución del runbook a lo largo de 3 ocurrencias del incidente: V1 manual → V2 con comandos → V3 automatizada.
> Formato blameless: se analizan sistemas y procesos, no personas. Los on-call se identifican por rol.

---

## Versión 1: mitigación manual (PM #1)

**Fecha del incidente:** domingo 15 de febrero de 2026, 02:40 → 03:55 CET
**Duración del incidente:** 75 minutos (primeros 429 a las 02:40 → verificación a las 03:55)
**Detectado por:** alerta genérica de error rate > 5 % en `oms-catalog` a las 02:52. No había alertas de CPU ni de instancias.
**On-call:** SRE de guardia (semana 7)
**Impacto:**
- Catálogo: ~14 min equivalentes de indisponibilidad (429 y 5xx intermitentes entre el 20 % y el 60 % de las peticiones). El **64 % del objetivo mensual** del catálogo (99,95 % ≈ 22 min/mes).
- Checkout: 2 `POST /api/orders` con 503 a las 03:12 por la reacción descrita abajo: el **3 % del budget del SLO 1**. Poco, porque eran las 3 de la madrugada.

### Lo que ocurrió
Un scraper de precios recorrió el catálogo desde ~300 IPs de proveedores cloud: paginación completa de `GET /api/products` con todas las combinaciones de orden y filtro, y `/api/search` barriendo prefijos. El tráfico se multiplicó por 6 a una hora en la que normalmente es mínimo. Casi todo eran páginas que no estaban en Redis, así que cada petición iba a Postgres. `oms-catalog` llegó a 20/20 instancias con la CPU al 97 % y Cloud Run empezó a devolver 429.

### Cómo se mitigó (improvisado, sin runbook)
1. A las 03:05 el on-call subió `max-instances` de `oms-catalog` de 20 a 40 para "dar más capacidad". No hizo la cuenta: 40 × 5 + 200 + 10 = **410 conexiones posibles > 380**. Con los misses de caché, las instancias nuevas abrieron sus pools y a las 03:12 Postgres llegó a `max_connections`: `oms-orders` no conseguía conexión y fallaron 2 checkouts. Bajó a 20 de nuevo a las 03:15.
2. Sacó las 20 IPs con más peticiones de los logs del Load Balancer y las bloqueó con una regla de Cloud Armor desde la consola. El scraper rotó de IPs en 10 minutos.
3. A las 03:30 llamó al segundo SRE de la rotación, que propuso un rate limit por IP en las rutas del catálogo. Se creó a las 03:40 (sin preview: por suerte no afectó a clientes). La CPU bajó al 55 % a las 03:48.

### Aprendizajes y acciones
- **No teníamos alertas de CPU ni de techo de instancias.** → `CloudRunCPUSaturated` y `CloudRunAtMaxInstances`. *(Hecho, 17/02)*
- **Subir `max-instances` sin la fórmula del pool convirtió un problema del catálogo en uno del checkout.** → La fórmula pasa a estar en el runbook con un comando que la calcula desde la configuración real. *(Hecho, 18/02)*
- **Bloquear IPs a mano no sirve contra un bot que rota:** hay que limitar por comportamiento (tasa por IP) en el borde. → Runbook V1 con el comando de rate limit.
- Cloud Armor solo se tocaba desde la consola, sin rol del on-call. → Elevación temporal a `roles/compute.securityAdmin` con Privileged Access Manager. *(Hecho, 24/02)*

### Versión 1 del runbook tras este post-mortem

> **Runbook v1 (texto literal):**
> "Si salta CloudRunCPUSaturated o CloudRunAtMaxInstances: mirar las IPs con más peticiones en los logs del LB.
> Si unas pocas concentran el tráfico, crear un rate limit de Cloud Armor en /api/products y /api/search.
> Si hace falta subir max-instances, comprobar antes que max-instances × DB_POOL_SIZE de todos los servicios < 380."

(Asume que la CPU alta siempre es un bot. Por eso hay V2.)

---

## Versión 2: diagnóstico y comandos exactos (PM #2)

**Fecha del incidente:** lunes 4 de mayo de 2026, 10:00 → 10:31 CEST
**Duración del incidente:** 31 minutos (degradación desde 10:00; `CloudRunAtMaxInstances` a las 10:13; mitigado a las 10:16; verificado a las 10:31)
**Detectado por:** `CloudRunCPUSaturated` warning a las 10:10 y `CloudRunAtMaxInstances` critical en `oms-orders` a las 10:13.
**On-call:** SRE de guardia (semana 19), distinta persona que en PM #1.
**Impacto:** p95 de `GET /api/orders*` en 340 ms durante 14 min (~1.350 consultas, el 30 % por encima de 250 ms: el **0,7 % del budget del objetivo del 95 % del SLO 2**). 3 `POST /api/orders` con 504 por timeout: el **4 % del budget del SLO 1**. Las facturas de la mañana llegaron con 45 min de retraso, todas correctas.

### Qué se vio durante esta segunda ocurrencia
La campaña "Días sin IVA" multiplicó por 3,5 los pedidos. `oms-orders` llegó a 20/20 instancias con la CPU p99 al 92 %. El runbook V1 mandaba mirar las IPs: el on-call lo hizo y vio tráfico repartido entre miles de clientes con conversión normal. **No bloqueó nada** (bien), pero el runbook no decía qué hacer con tráfico legítimo, salvo subir `max-instances`. Hizo la cuenta de la fórmula (20 → 26 en `oms-orders` cabía: 370) y, mientras preparaba el cambio, el segundo SRE señaló que la CPU no se iba en atender peticiones: la generación de PDF de facturas (300 ms de CPU por pedido, dentro de `oms-orders`) consumía el 38 % del tiempo del servicio, multiplicada por 3,5 como los pedidos. `flag_on billing.pdf-generation-paused` a las 10:16: CPU del 92 % al 64 % e instancias de 20 a 15 en 3 minutos. No hizo falta subir `max-instances`.

### Qué cambió en el runbook (V1 → V2)
- **Diagnóstico de la causa antes que la capacidad:** ¿bot o legítimo? (tráfico frente a la semana anterior + conversión + top IPs), ¿revisión nueva?, ¿qué trabajo caro? (segundos de servicio por endpoint, facturas por minuto, flags), ¿Cloud Run o Cloud SQL?, margen de la fórmula.
- **Acciones con flags primero** (`billing.pdf-generation-paused`, `catalog.search-suggestions-disabled`, `orders.batch-jobs-paused`): sin redeploy y sin tocar la fórmula.
- **Cloud Armor con preview:** la regla se crea en modo preview, se comprueba a quién bloquearía y después se activa. Comandos idempotentes (`describe || create`).
- **Capacidad con la fórmula calculada desde la configuración real**, más CPU por instancia (no abre conexiones) y, con aprobación, la redistribución del pool del catálogo.
- **Distinción explícita con la CPU de Cloud SQL** (→ runbooks 01 y 02).

### Acciones derivadas
- Rate limit permanente en Cloud Armor (prioridad 1000, 300 req/min por IP en catálogo y búsqueda) y ban automático (prioridad 1100) en Terraform, con Adaptive Protection activado. *(Hecho, 19/05)*
- Sacar la generación de PDF de `oms-orders` a un Cloud Run Job propio. *(En backlog, prioridad alta antes de Black Friday)*
- Encender `billing.pdf-generation-paused` siempre es la primera acción en `oms-orders` con CPU alta: candidata a automatizar.

---

## Versión 3: defensas preventivas y flags automáticos (PM #3)

**Fecha:** jueves 15 de octubre de 2026. **Dos amenazas el mismo día y ninguna llegó a incidente.**

### Qué pasó (o, mejor, qué NO pasó)
- **03:10:** otro scraper, ×5 sobre `/api/products`. La regla permanente 1000 devolvió 429 a 41 IPs que superaban 300 req/min y la 1100 baneó 10 min a las 6 más agresivas. Adaptive Protection propuso una regla adicional por firma de user agent, que seguridad revisó por la mañana. `oms-catalog` llegó a 9/20 instancias y 68 % de CPU. **Nadie fue paginado.**
- **10:00:** campaña de otoño, pedidos ×3. A las 10:20 disparó `CloudRunCPUSaturated` warning en `oms-orders` y `oms-ops-flag-automator` encendió `billing.pdf-generation-paused` en 15 s. La CPU bajó al 66 % sin llegar al máximo de instancias. El flag se apagó solo a las 11:05, 30 min después de resolverse la alerta, y las facturas pendientes salieron en 12 min. **Cero impacto en SLO.**

### Qué cambió en el runbook (V2 → V3)
- **Defensa permanente en el borde:** las reglas 1000 y 1100 y Adaptive Protection están en Terraform. El rate limit manual del runbook (Acción 2) pasa a ser una regla **temporal y más estricta** (prioridad 950) solo para bots que se cuelan por debajo de la permanente.
- **Flags automáticos por alerta:** `oms-ops-flag-automator` (el mismo servicio del runbook 02) tiene dos reglas nuevas: `CloudRunCPUSaturated{job="oms-orders"}` → `billing.pdf-generation-paused`; `CloudRunCPUSaturated{job="oms-catalog"}` → `catalog.search-suggestions-disabled`. Las enciende al disparar la warning y las apaga 30 min después de resolverse.
- **Las defensas se vigilan:** el Cloud Run Job `oms-ops-armor-drift` (Cloud Scheduler cada hora) compara `oms-edge-policy` con Terraform y solo publica `ops_job_last_success_timestamp_seconds{job="armor-drift-check"}` si no hay diferencias. `CloudArmorPolicyCheckStale` avisa si pasan 3 h sin éxito. La salud del automatizador la vigila `FlagAutomatorStale` (compartida).
- La alerta critical se mantiene: si con el flag automático y Cloud Armor la CPU sigue al 85 % y sin margen de instancias, es capacidad de verdad y hace falta un humano con la fórmula delante.

### El runbook como automatización

- **Flags:** Cloud Run `oms-ops-flag-automator`, reglas en `automator-rules.yaml` (repo `oms-ops`). Disparador: webhook de Alertmanager (`automation="flag-automator"`). **Owner:** equipo SRE.
- **Cloud Armor:** módulo Terraform `edge-security` (reglas 1000 y 1100 + Adaptive Protection). **Owner:** equipo de seguridad, con SRE.
- **Drift check:** `0 * * * *` (Cloud Scheduler `oms-ops-armor-drift`, zona `Europe/Madrid`) → Cloud Run Job `oms-ops-armor-drift` (`terraform plan -detailed-exitcode` sobre el módulo `edge-security`). **Owner:** equipo SRE.
- **Salud:** `ops_job_last_success_timestamp_seconds{job="armor-drift-check"}` (`CloudArmorPolicyCheckStale`) y `{job="flag-automator"}` (`FlagAutomatorStale`).

### Métrica del runbook a lo largo de la evolución

| | V1 (manual) | V2 (comandos) | V3 (preventivo) |
|---|---|---|---|
| Veces disparado por incidente real | 1 | 1 | 0 (dos amenazas absorbidas) |
| Detección | Alerta genérica de errores, 12 min tarde | `CloudRunCPUSaturated` + `AtMaxInstances` | Cloud Armor y warning con flag automático |
| Tiempo de mitigación | 68 min (02:40 → 03:48) | 16 min (10:00 → 10:16) | 15 s, sin humano |
| Impacto | 64 % del objetivo mensual del catálogo + 3 % SLO 1 | 4 % SLO 1 + 0,7 % SLO 2 | 0 % |
| Error del on-call evitado | — (rompió la fórmula del pool) | No bloqueó tráfico legítimo | — |
| Toil del on-call | Alto (consola, IPs una a una) | Medio (diagnóstico + flag) | Cero |
| Siguiente mejora | Diagnóstico bot / legítimo / trabajo caro | Reglas permanentes y flag automático | PDF fuera de `oms-orders` y PgBouncer para Black Friday (×5) |

---

## Conclusión

Este runbook empezó con la reacción más intuitiva ante una CPU alta, "más servidores", y esa reacción tumbó el checkout porque las instancias de Cloud Run no son gratis para la base de datos: cada una abre su pool. La primera lección fue que **la fórmula de capacidad manda sobre `max-instances`**. La segunda, en V2: la CPU alta no siempre es un ataque; preguntar "¿bot o cliente?" y "¿qué trabajo se come la CPU?" llevó a un flag de 15 s en lugar de a un cambio de capacidad. En V3 cada causa conocida tiene una defensa en el sitio adecuado: los bots, en el borde (Cloud Armor, antes de llegar a Cloud Run); el trabajo caro, con un flag automático y reversible. Como en los runbooks 01, 02 y 07, las defensas automáticas tienen su propia alerta, porque una regla de Cloud Armor borrada a mano o un automatizador caído no avisan solos. Lo que queda es de arquitectura: con Black Friday a ×5, la fórmula de 380 conexiones no da para todos, y eso se resuelve con PgBouncer y sacando los PDF del servicio transaccional, no con un runbook.
