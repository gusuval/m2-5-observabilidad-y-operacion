# Runbook: Catálogo no disponible (triage end-to-end)

> **Adicional (fuera del catálogo).** Es el runbook de entrada cuando el cliente **no puede ver la tienda**. No arregla una pieza concreta: localiza en qué capa está el fallo (edge, Cloud Run, Redis, Postgres o búsqueda) y deriva al runbook específico, con las mitigaciones transversales que se pueden aplicar mientras tanto.
> **Versión:** V3 (ver `post-mortems.md`). **Owner:** equipo SRE. **Última revisión:** 2026-10-13.

---

## Trigger

**Alerta:** `CatalogUnavailable` (severity: **critical**, PagerDuty). Tiene dos reglas con el mismo nombre; la etiqueta `trigger` dice cuál ha saltado:
- `trigger="probe"`: `probe_success{probe="catalog-home"} == 0` desde **europe-west1** durante **2 minutos**. La probe hace `GET /` (SPA desde el CDN) + `GET /api/products?page=1` cada 30 s, fuera de la región primaria.
- `trigger="5xx"`: más del **2 %** de 5xx en `oms-catalog` (app + los 5xx que genera el propio Load Balancer) durante **3 minutos**.

**Alertas asociadas:** `CatalogAvailabilityBurnRateSlow` (warning: degradación lenta del 99,95 %), `CatalogEdgeDenySpike` (warning: Cloud Armor está bloqueando > 10 % del tráfico; **los 403 no son 5xx** y no los ve el SLI, ver PM #1), `CatalogTLSCertExpiringSoon` (warning, < 14 días) y `CatalogProbeMissing` (la probe no reporta).

**Cuándo dispara (patrones conocidos):**
1. **Edge**: regla de Cloud Armor que bloquea a usuarios reales (PM #1), certificado caducado o sin renovar, despliegue de la SPA a medias en el bucket, cambio en el URL map.
2. **Revisión nueva de `oms-catalog`** que falla → [runbook 07](../07-canary-rollout-failed/runbook.md).
3. **Saturación de `oms-catalog`**: CPU o `max-instances` al límite → [runbook 12](../12-server-cpu-high/runbook.md).
4. **Dependencias**: Redis caído ([05](../05-redis-cache-down/runbook.md)) o con hit ratio bajo ([08](../08-redis-cache-hit-ratio-low/runbook.md)), Postgres caído ([09](../09-postgres-unavailable/runbook.md)) o con el pool saturado ([01](../01-db-pool-saturated/runbook.md)).
5. **Búsqueda**: Elasticsearch lento o en rojo que arrastra a todo `oms-catalog` porque las peticiones de búsqueda ocupan la concurrencia de las instancias (PM #2).

---

## Contexto

**Qué impacto tiene (en lenguaje de negocio):**
- **El cliente no puede ver productos, y quien no ve productos no compra.** El catálogo es la puerta de entrada del 100 % de las compras nuevas.
- **NFR-AVAIL-002: 99,95 % mensual ≈ 22 min/mes de budget.** Cada minuto de caída total es el **4,5 %** del budget mensual. Cuando salta la alerta (2–3 min de `for:`) ya se ha gastado ~10 %. Aquí **se mitiga primero y se investiga después**.
- **Imagen**: la tienda en blanco se comparte en redes. A los 10 min hay que avisar a atención al cliente (sección Comunicación).

**Qué NO afecta:**
- **Checkout** (`oms-orders`): es otro servicio de Cloud Run con su propio backend en el LB. Un cliente con el carrito ya hecho puede comprar si la SPA carga (el carrito está en `localStorage`). Si la SPA no carga (fallo del edge), **sí** se ve afectado: comprueba la probe `checkout-e2e` y, si falla, abre también el [runbook 10](../10-checkout-unavailable/runbook.md).
- **Pedidos ya hechos, pagos, facturas, outbox**: no dependen del catálogo.
- **Backoffice**: entra por otro host y otro backend.

**Arquitectura del camino (de fuera hacia dentro):**
`www.oms.example.com` → HTTPS Load Balancer global (`oms-web-https-fr`, proxy `oms-web-https-proxy`, certificado gestionado `oms-web-cert`, URL map `oms-web-urlmap`) → **Cloud Armor** `oms-edge-policy` → dos caminos:
- `/*` → backend bucket `oms-spa` con **Cloud CDN** → bucket GCS `oms-production-spa` (europe-west3). `index.html` va con `no-cache` (el CDN revalida siempre) y los bundles con hash en el nombre van como `immutable` (ADR-008).
- `/api/*` → backend service `oms-catalog` (serverless NEG, europe-west3, sin CDN) → Cloud Run `oms-catalog` → Redis `oms-redis-prod` → Cloud SQL `oms-pg-prod`; y `/api/search*` → Elasticsearch 8 en GCE (MIG regional `oms-search`, 3 nodos en europe-west3, ILB interno `search.oms.internal:9200`).

**Pre-requisitos para diagnosticar:**
- Grupo `oms-oncall@` con `roles/run.developer`, `roles/logging.viewer`, `roles/compute.loadBalancerAdmin` (URL map, backend bucket, invalidar CDN), `roles/compute.securityAdmin` (Cloud Armor), `roles/storage.objectAdmin` sobre `oms-production-spa` y `roles/iap.tunnelResourceAccessor`.
- Dashboard [`oms-overview`](https://grafana.example.com/d/oms-overview) (paneles "Catálogo: probe + 5xx", "Edge: LB / CDN / Armor") y [`oms-cache`](https://grafana.example.com/d/oms-cache).
- **Setup de sesión** ejecutado (ver [`../README.md`](../README.md#setup-de-sesión-del-on-call-copiar-una-vez-al-empezar-el-incidente)), más:

```bash
bastion() { gcloud compute ssh oms-ops-bastion --zone=europe-west3-a --tunnel-through-iap --command "$1"; }
# En el bastión, oms-es = curl a https://search.oms.internal:9200 con la CA del clúster y la API key de oms-search-ops-apikey
export SITE=https://www.oms.example.com
```

---

## Diagnóstico (en orden, ejecuta y mira)

> **Regla de PM #2:** no recorras las capas de arriba abajo "por si acaso". Primero averigua **de dónde sale el error** (paso 0 y 1) y ve directo a esa capa. Con 22 min de budget al mes no hay tiempo para revisar lo que funciona.

### 0. ¿Qué ve un cliente? (desde tu portátil, fuera de GCP · 30 s)

```bash
curl -sS -o /dev/null -w "SPA  %{http_code} %{time_total}s\n" "$SITE/"
curl -sS -o /dev/null -w "API  %{http_code} %{time_total}s\n" "$SITE/api/products?page=1"
curl -sSI "$SITE/" | grep -iE "^(HTTP|age|cache-control|via|x-goog)"
echo | openssl s_client -connect www.oms.example.com:443 -servername www.oms.example.com 2>/dev/null | openssl x509 -noout -enddate -issuer
promq 'max(probe_success{probe="catalog-home"})'; promq 'max(probe_success{probe="checkout-e2e"})'
```

| Lo que ves | Significa | Ve a |
|---|---|---|
| Error de TLS / `notAfter` en el pasado | Certificado caducado o no servido | Paso 2b |
| `403` en SPA o API | Cloud Armor (casi siempre) | Paso 2a |
| SPA `200` pero página en blanco en el navegador | `index.html` apunta a bundles que no existen | Paso 2c |
| SPA falla y API `200` | Bucket, backend bucket o URL map | Paso 2c |
| SPA `200`, API `5xx` o timeout | Backend: Cloud Run o dependencias | Paso 1 y 3 |
| Todo `200` desde fuera, pero la alerta sigue | Fallo intermitente o solo en parte de los usuarios | Paso 1 |

**Anuncia ya** en `#incident-response`: *"Investigando CatalogUnavailable (trigger=X). Capa sospechosa: Y."*

### 1. ¿Qué dice el Load Balancer de los errores? (la pista más rápida)

```bash
gcloud logging read 'resource.type="http_load_balancer" AND (httpRequest.status>=500 OR httpRequest.status=403)' \
  --freshness=10m --limit=2000 \
  --format="value(resource.labels.backend_service_name,httpRequest.status,jsonPayload.statusDetails)" \
  | sort | uniq -c | sort -rn | head
promq 'sum by (endpoint, code) (rate(http_requests_total{job="oms-catalog", code=~"5.."}[5m]))'
```

| `statusDetails` | Significa | Ve a |
|---|---|---|
| `denied_by_security_policy` (403) | Una regla de Cloud Armor | Paso 2a |
| `response_sent_by_backend` (5xx) | La app responde con error | Paso 3 (mira qué `endpoint` domina) |
| `failed_to_pick_backend` / `failed_to_connect_to_backend` | No hay backend sano: Cloud Run no arranca, NEG o región con problemas | Paso 3 |
| `backend_timeout` | La app tarda más que el timeout del backend (30 s) | Paso 3 y 4 |
| errores en `oms-spa` (`backend_service_name` vacío, backend bucket) | Bucket o CDN | Paso 2c |

**Si un solo `endpoint` concentra los 5xx** (por ejemplo `/api/search`): el problema es de esa dependencia (paso 4c). Si están repartidos entre todos: Cloud Run o una dependencia común (pasos 3, 4a y 4b).

### 2. Capa edge

**2a. Cloud Armor: ¿qué regla bloquea y quién la cambió?**
```bash
gcloud logging read 'resource.type="http_load_balancer" AND jsonPayload.enforcedSecurityPolicy.outcome="DENY"' \
  --freshness=15m --limit=2000 --format="value(jsonPayload.enforcedSecurityPolicy.priority)" | sort | uniq -c | sort -rn
gcloud compute security-policies describe oms-edge-policy --format=json \
  | jq -r '.rules[] | [.priority, .action, (.preview // false), (.match.expr.expression // (.match.config.srcIpRanges|join(","))), .description] | @tsv'
gcloud logging read 'protoPayload.methodName:"compute.securityPolicies"' --freshness=24h --limit=10 \
  --format="table(timestamp,protoPayload.methodName,protoPayload.authenticationInfo.principalEmail)"
```
**Una prioridad concentra los DENY y se cambió hace poco:** es la causa. **Acción 2**. Anota la prioridad.
**Los DENY vienen de la regla de rate limit permanente (prioridad 1000) y de pocas IPs:** es Armor haciendo su trabajo contra un bot. No es la causa del incidente.

**2b. Certificado y proxy:**
```bash
gcloud compute ssl-certificates describe oms-web-cert --global --format="yaml(managed.status,managed.domainStatus,expireTime)"
gcloud compute target-https-proxies describe oms-web-https-proxy --global --format="value(sslCertificates,urlMap)"
dig +short CAA oms.example.com; dig +short www.oms.example.com
```
**`domainStatus` = `FAILED_CAA_FORBIDDEN` o `FAILED_NOT_VISIBLE`:** un cambio de DNS impide renovar. Corrige el registro (CAA tiene que permitir `pki.goog`; el A tiene que apuntar a la IP de `oms-web-https-fr`) con quien gestione el DNS. Google reintenta la emisión sola, pero puede tardar hasta 60 min: **escala al EM** y comunica (Comunicación).
**El proxy no lista `oms-web-cert` o apunta a otro URL map:** cambio manual del LB. Revierte con Terraform (`infra/lb`) o con la Acción 4.

**2c. SPA, bucket y CDN:**
```bash
gcloud storage ls -l gs://oms-production-spa/index.html
curl -s "$SITE/index.html" | grep -oE '/assets/[^"]+\.(js|css)' | sort -u \
  | while read a; do curl -s -o /dev/null -w "%{http_code} $a\n" "$SITE$a"; done
gcloud compute backend-buckets describe oms-spa --format="yaml(bucketName,enableCdn,cdnPolicy.cacheMode,cdnPolicy.serveWhileStale)"
gcloud logging read 'protoPayload.methodName:("compute.urlMaps" OR "compute.backendBuckets")' --freshness=24h --limit=10 \
  --format="table(timestamp,protoPayload.methodName,protoPayload.authenticationInfo.principalEmail)"
```
**Algún bundle da `404`:** el deploy de la SPA subió `index.html` antes que sus bundles, o se cortó a medias. **Acción 3**.
**`index.html` no existe o el bucket da `403`:** permisos del bucket o borrado. Acción 3 (restaurar la release).
**Cambio reciente en el URL map:** revierte con Terraform.

### 3. Cloud Run `oms-catalog`: ¿revisión nueva o saturación?

```bash
gcloud run services describe oms-catalog --region=$REGION --format="yaml(status.traffic,status.conditions)"
gcloud run revisions list --service=oms-catalog --region=$REGION --limit=3 --format="table(metadata.name,metadata.creationTimestamp)"
promq 'sum by (revision) (rate(http_requests_total{job="oms-catalog", code=~"5.."}[5m])) / sum by (revision) (rate(http_requests_total{job="oms-catalog"}[5m]))'
promq 'max(run_container_cpu_utilization{job="oms-catalog"})'; promq 'max(run_instance_count{job="oms-catalog"}) / max(run_max_instances{job="oms-catalog"})'
```
**Revisión creada en la última hora y los 5xx vienen de ella:** **Acción 5** (revertir) y [runbook 07](../07-canary-rollout-failed/runbook.md).
**CPU > 85 % o instancias al máximo:** saturación → [runbook 12](../12-server-cpu-high/runbook.md). Antes, descarta el paso 4c: una búsqueda lenta también llena las instancias.
**`status.conditions` con `Ready=False`:** la revisión no arranca (imagen, secreto, variable). Acción 5.

### 4. Dependencias (mira las tres a la vez)

```bash
promq 'max(redis_up{instance_id="oms-redis-prod"})'                                                    # 4a Redis
promq 'sum(rate(cache_requests_total{job="oms-catalog", cache="catalog", result="hit"}[5m])) / sum(rate(cache_requests_total{job="oms-catalog", cache="catalog", result=~"hit|miss"}[5m]))'
promq 'max(pg_up)'; promq 'max by (job) (db_pool_in_use{job="oms-catalog"} / db_pool_size{job="oms-catalog"})'   # 4b Postgres
promq 'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="oms-catalog", endpoint="/api/search"}[5m])))'
bastion 'oms-es "/_cluster/health?filter_path=status,number_of_nodes,unassigned_shards"'               # 4c búsqueda
bastion 'oms-es "/_cat/nodes?v&h=name,heap.percent,cpu,disk.used_percent"'
```
**4a. `redis_up` = 0** → [runbook 05](../05-redis-cache-down/runbook.md). **Hit ratio < 80 %** → [runbook 08](../08-redis-cache-hit-ratio-low/runbook.md).
**4b. `pg_up` = 0** → [runbook 09](../09-postgres-unavailable/runbook.md) y **Acción 1a** ya. **Pool de `oms-catalog` > 90 %** → [runbook 01](../01-db-pool-saturated/runbook.md).
**4c. `status` = `red`, `disk.used_percent` > 90 % (con el 95 %, Elasticsearch pasa los índices a solo lectura) o p95 de `/api/search` > 1 s:** la búsqueda está arrastrando al catálogo. **Acción 1b**.

---

## Acciones (de menor a mayor impacto, idempotentes)

> Con 22 min de budget al mes, la Acción 1 se ejecuta **en cuanto el diagnóstico apunta a una dependencia**, sin esperar a entender la causa raíz.

### Acción 1: Degradar el catálogo con flags (impacto: bajo · reversible en 15 s)

```bash
flag_on catalog.serve-stale                        # 1a: Postgres caído o lento (necesita Redis vivo)
flag_on catalog.search-suggestions-disabled        # 1b: búsqueda lenta: quita autocompletado y sugerencias (~60 % de las llamadas a ES)
flag_on catalog.search-fallback-postgres           # 1b: si ES sigue mal: búsqueda básica por nombre en Postgres (pg_trgm, rate limited)
```
**Qué hace:** 1a sirve el catálogo desde Redis aunque el TTL haya caducado; con Redis caído no aporta nada (ver [runbook 05](../05-redis-cache-down/runbook.md)). 1b quita la parte cara de la búsqueda y, si hace falta, la sustituye por una búsqueda simple sin facetas que no pasa por Elasticsearch.
**Idempotente:** sí, son flags.
**Coste:** precios o stock con hasta 1 h de antigüedad en el catálogo (el checkout revalida siempre); búsqueda peor (sin sugerencias, sin tolerancia a errores tipográficos).
**Cuándo NO:** 1a si `redis_up` = 0. `search-fallback-postgres` si el pool de `oms-catalog` está > 75 % (se añadiría carga a Postgres).

### Acción 2: Poner en preview la regla de Cloud Armor (impacto: bajo · solo si el paso 2a la identificó)

```bash
gcloud compute security-policies rules update <PRIORIDAD> --security-policy=oms-edge-policy --preview
```
**Qué hace:** la regla deja de bloquear, pero sigue **registrando** lo que habría bloqueado. No se borra: se conserva para el post-mortem y para reactivarla corregida.
**Por qué preview y no `delete`:** en PM #1 se borró la regla y con ella desapareció la protección contra el scraper que la había motivado.
**Idempotente:** sí. Ponerla en preview dos veces no cambia nada.
**Cuándo NO:** si la regla es de la OWASP CRS o la de rate limit permanente (prioridad 1000). Desactivarlas expone la tienda; escala al EM y a seguridad.

### Acción 3: Restaurar la SPA y servirla desde la caché del CDN (impacto: bajo-medio · solo si el paso 2c lo indica)

```bash
gcloud compute backend-buckets update oms-spa --serve-while-stale=86400         # si el bucket falla, el CDN sirve la última copia buena
gcloud storage ls gs://oms-production-spa-releases/ | tail -3                     # elige la release anterior (<SHA_ANTERIOR>)
gcloud storage rsync gs://oms-production-spa-releases/<SHA_ANTERIOR>/assets gs://oms-production-spa/assets --recursive
gcloud storage cp gs://oms-production-spa-releases/<SHA_ANTERIOR>/index.html gs://oms-production-spa/index.html \
  --cache-control="no-cache"                                                      # index.html SIEMPRE el último
```
**Qué hace:** sube primero los bundles de la release buena y después su `index.html`. Como los bundles tienen hash, conviven con los de la release rota sin pisarse.
**Idempotente:** sí. `rsync` y `cp` con los mismos objetos no cambian nada.
**Coste:** se pierde la funcionalidad de la release nueva hasta que se redespliegue bien.

### Acción 4: Invalidar la caché del CDN, con cuidado (impacto: medio)

```bash
gcloud compute url-maps invalidate-cdn-cache oms-web-urlmap --path="/index.html" --async
```
**Cuándo:** **solo después** de que el origen esté sano (Acción 3 hecha y `gcloud storage ls` correcto), y solo si el CDN sigue sirviendo una copia rota.
**Cuándo NO:** **nunca con el origen roto**: invalidar borra las copias buenas que todavía te salvan. Tampoco invalides `/*`: los bundles son `immutable` y con hash, no lo necesitan, y vaciar todo el CDN manda el tráfico entero al bucket.
**Idempotente:** sí. Invalidar dos veces la misma ruta no tiene efecto adicional.

### Acción 5: Revertir la revisión de `oms-catalog` (impacto: medio · el paso 3 apunta a una revisión nueva)

```bash
gcloud run services update-traffic oms-catalog --region=$REGION --to-revisions=<REVISION_ANTERIOR>=100
```
**Qué hace:** devuelve el 100 % del tráfico a la última revisión sana, sin construir nada. Detalle y análisis en el [runbook 07](../07-canary-rollout-failed/runbook.md).
**Idempotente:** sí. Fuera de la ventana de OPS-002 está permitido para mitigar; anótalo en el ticket.

### Acción 6: Escalar (impacto: alto · cuando nada de lo anterior aplica)

Si a los **10 min** de la alerta no hay causa identificada o la causa no se puede mitigar (certificado que Google tiene que reemitir, incidente de GCP en la región): escala al **EM de guardia** (PagerDuty level 2) y abre caso P1 con soporte de Google Cloud si el [Service Health](https://status.cloud.google.com) muestra incidente en europe-west3. Para una caída regional completa, la decisión de DR (europe-west1) es del EM y sigue el [runbook 09](../09-postgres-unavailable/runbook.md).

---

## Comunicación

| Cuándo | Dónde | Mensaje |
|---|---|---|
| Al confirmar (paso 0) | `#incident-response` | *"CatalogUnavailable: la tienda no carga para [todos / parte de los usuarios]. Capa: X. Siguiente update en 15 min."* |
| A los 10 min sin mitigar | `#incident-response`, mencionando a atención al cliente | *"Estamos teniendo problemas para mostrar el catálogo. Los pedidos ya hechos no están afectados y los carritos se conservan. Os avisamos al resolver."* |
| Cada 15 min | `#incident-response` | Estado, acción en curso, siguiente paso. |
| Al cerrar | `#incident-response` y `#oms-releases` si hubo rollback | Mensaje de cierre de la Verificación. |

---

## Verificación

```promql
# 1. Probe en verde desde europe-west1
min_over_time(probe_success{probe="catalog-home"}[10m]) == 1
# 2. 5xx del catálogo (app + LB) por debajo del 0,05 % (el objetivo de NFR-AVAIL-002)
(sum(rate(http_requests_total{job="oms-catalog", code=~"5.."}[5m])) + (sum(rate(loadbalancer_backend_5xx_total{backend_service="oms-catalog"}[5m])) or vector(0)))
  / (sum(rate(http_requests_total{job="oms-catalog"}[5m])) + (sum(rate(loadbalancer_backend_5xx_total{backend_service="oms-catalog"}[5m])) or vector(0))) < 0.0005
# 3. Sin bloqueos anómalos en el edge
sum(rate(loadbalancer_requests_total{backend_service=~"oms-catalog|oms-spa", code="403"}[5m])) / sum(rate(loadbalancer_requests_total{backend_service=~"oms-catalog|oms-spa"}[5m])) < 0.01
# 4. Latencia de vuelta en NFR-PERF-001
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="oms-catalog", endpoint=~"/api/products.*"}[5m]))) < 0.15
```

**Ventana de observación:** **10 minutos** consecutivos con las 4 condiciones, más el paso 0 repetido desde tu portátil **y** desde una red móvil (otro ISP: en PM #1 el fallo solo afectaba a un ISP).

**Cómo confirmas:**
1. Panel "Catálogo: probe + 5xx" de [`oms-overview`](https://grafana.example.com/d/oms-overview).
2. `CatalogUnavailable` en *resolved* en Alertmanager.
3. Comunica en `#incident-response`: *"Catálogo recuperado a las HH:MM. Causa: [capa]. Duración: N min (≈ X % del budget mensual de NFR-AVAIL-002). Acciones: [lista]. Flags activos: [lista]. Cerrando incidente."*

---

## Rollback

| Acción | Rollback | Cuándo |
|---|---|---|
| 1a | `flag_off catalog.serve-stale` | Con Postgres sano y el hit ratio > 90 % durante 15 min. |
| 1b | `flag_off catalog.search-fallback-postgres`, y 10 min después `flag_off catalog.search-suggestions-disabled` | Con ES en `green` y p95 de `/api/search` < 300 ms (NFR-PERF-004) durante 15 min. |
| 2 | `gcloud compute security-policies rules update <PRIORIDAD> --security-policy=oms-edge-policy --no-preview`, **solo con la regla corregida** y tras 24 h en preview revisando lo que habría bloqueado | Nunca la misma regla tal cual. |
| 3 | Redesplegar la release nueva con el pipeline (bundles primero, `index.html` al final). `serve-while-stale` se queda: es la configuración correcta. | En horario de OPS-002, con el bug corregido. |
| 4 | No tiene rollback: el CDN se vuelve a llenar solo. | — |
| 5 | `gcloud run services update-traffic oms-catalog --region=$REGION --to-revisions=<REVISION_NUEVA>=100`, o mejor un canary nuevo con el fix | Según el [runbook 07](../07-canary-rollout-failed/runbook.md). |

---

## Después del incidente

- [ ] Anota en el ticket: capa culpable, duración, minutos equivalentes de budget consumidos de NFR-AVAIL-002 y qué runbook específico se usó.
- [ ] Revisa con `flag_get` los flags tocados (`serve-stale`, `search-*`).
- [ ] Si fue Cloud Armor: la regla corregida vuelve por Terraform con 24 h en `--preview` (proceso V3). Nada de cambios en consola.
- [ ] Si fue la SPA: el pipeline tiene que subir bundles antes que `index.html`. Si no lo hizo, abre ticket.
- [ ] Si consumiste > 25 % del budget mensual: post-mortem en < 48 h y revisión del estado en la [política de error budget](../../slos/error-budget-policy.md).
- [ ] Si el diagnóstico no te llevó directo a la capa, añade la pista que faltaba al paso 0 o al 1.

---

## Enlaces relacionados

- Alerta YAML: [`alert.yaml`](alert.yaml) · Post-mortems: [`post-mortems.md`](post-mortems.md)
- Arquitectura: [`ADR-008`](../../../anexo-arquitectura/05-adrs/ADR-008-frontend-spa-cdn.md) (SPA + CDN), [`ADR-005`](../../../anexo-arquitectura/05-adrs/ADR-005-redis-cache-catalogo.md) (caché)
- Instrumentación y NFR-AVAIL-002: [`../../observability/instrumentation.md`](../../observability/instrumentation.md) · Política: [`../../slos/error-budget-policy.md`](../../slos/error-budget-policy.md)
- Dashboard: <https://grafana.example.com/d/oms-overview>
- Runbooks derivados: [`05`](../05-redis-cache-down/runbook.md) · [`08`](../08-redis-cache-hit-ratio-low/runbook.md) · [`09`](../09-postgres-unavailable/runbook.md) · [`01`](../01-db-pool-saturated/runbook.md) · [`07`](../07-canary-rollout-failed/runbook.md) · [`12`](../12-server-cpu-high/runbook.md) · [`10`](../10-checkout-unavailable/runbook.md) (si el checkout también cae) · [`02`](../02-api-p95-high/runbook.md) (si es lento pero no cae)
