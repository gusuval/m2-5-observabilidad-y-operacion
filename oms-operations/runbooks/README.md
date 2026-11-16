# Runbooks

> Cada subcarpeta es un runbook independiente con sus 3 artefactos:
> `runbook.md`, `alert.yaml`, `post-mortems.md`.

## Estructura por runbook

```
01-db-pool-saturated/
├── runbook.md       ← el procedimiento (lo lee el on-call)
├── alert.yaml       ← la regla Prometheus que dispara el runbook
└── post-mortems.md  ← evolución V1 → V2 → V3 a lo largo del tiempo
```

## Criterios comunes (cualquier runbook tiene que cumplirlos)

1. **Trigger explícito**: la alerta exacta que lo dispara, con su severity.
2. **Contexto**: qué impacta al usuario y qué NO afecta (acota el blast radius).
3. **Diagnóstico copiable**: comandos exactos en bloques de código. No vale "comprueba si la DB está sana"; tiene que ser el `psql -c "SELECT..."`.
4. **Acciones ordenadas de menor a mayor impacto**: se empieza por lo barato y se escala.
5. **Acciones idempotentes**: ejecutarlas dos veces no empeora la situación.
6. **Verificación medible**: qué métrica concreta tiene que volver a la normalidad y en qué ventana.
7. **Rollback**: qué se deshace si la mitigación empeoró las cosas.
8. **Feature flag antes que redeploy**: si una mitigación se puede expresar como flag, se expresa como flag (regla 7 del enunciado).
9. **Nunca "reinicia el servicio" como única acción**: si un reinicio aparece, va precedido de diagnóstico y de al menos una alternativa.

## Catálogo

| ID | Carpeta | Alerta principal | Severity | Módulo / pieza | Estado |
|---|---|---|---|---|---|
| 01 | [`01-db-pool-saturated`](01-db-pool-saturated/runbook.md) | `DBConnectionPoolSaturated` | critical | Orders · Cloud SQL | **OBLIGATORIO** ✅ |
| 02 | [`02-api-p95-high`](02-api-p95-high/runbook.md) | `APILatencyP95High` | warning→critical | Todos los endpoints críticos | elegido ✅ |
| 03 | [`03-stripe-payment-gateway-degraded`](03-stripe-payment-gateway-degraded/runbook.md) | `StripeGatewayDegraded` | critical | Payments · Stripe | elegido ✅ |
| 04 | [`04-outbox-backlog-creciente`](04-outbox-backlog-creciente/runbook.md) | `OutboxBacklogGrowing` | warning→critical | Outbox relay | adicional (bonus) ✅ |
| 05 | [`05-redis-cache-down`](05-redis-cache-down/runbook.md) | `RedisCacheDown` | critical | Catalog · Memorystore | adicional (bonus) ✅ |
| 06 | [`06-cloud-sql-disk-full`](06-cloud-sql-disk-full/runbook.md) | `CloudSQLDiskUsageHigh` | warning→critical | Cloud SQL | adicional ✅ |
| 07 | [`07-canary-rollout-failed`](07-canary-rollout-failed/runbook.md) | `CanaryRolloutFailed` | critical | CI/CD · Cloud Run | adicional ✅ |
| 08 | [`08-redis-cache-hit-ratio-low`](08-redis-cache-hit-ratio-low/runbook.md) | `RedisCacheHitRatioLow` | warning | Catalog · Memorystore | adicional (fuera de catálogo) ✅ |
| 09 | [`09-postgres-unavailable`](09-postgres-unavailable/runbook.md) | `PostgresUnavailable` | critical | Cloud SQL | adicional (fuera de catálogo) ✅ |
| 10 | [`10-checkout-unavailable`](10-checkout-unavailable/runbook.md) | `CheckoutUnavailable` | critical | Orders + Payments (end-to-end) | adicional (fuera de catálogo) ✅ |
| 11 | [`11-catalog-unavailable`](11-catalog-unavailable/runbook.md) | `CatalogUnavailable` | critical | Catalog (end-to-end) | adicional (fuera de catálogo) ✅ |
| 12 | [`12-server-cpu-high`](12-server-cpu-high/runbook.md) | `CloudRunCPUSaturated` | warning→critical | Cloud Run (servidores de app) | adicional (fuera de catálogo) ✅ |

**Para la evaluación:** 01 es el obligatorio y 02 y 03 son los 2 elegidos del catálogo. 04 y 05 son el 4.º y el 5.º runbook del bonus. Del 06 al 12 se entregan como material operativo adicional para cubrir los modos de fallo de la arquitectura.

### Cómo se relacionan (árbol de decisión rápido a las 3 AM)

```
¿El usuario NO puede comprar?        → 10-checkout-unavailable  (triage end-to-end)
   ├─ ¿Postgres no responde?         → 09-postgres-unavailable
   ├─ ¿Pool de conexiones al >90%?   → 01-db-pool-saturated
   ├─ ¿Stripe falla / lento?         → 03-stripe-payment-gateway-degraded
   └─ ¿Justo después de un deploy?   → 07-canary-rollout-failed
¿El usuario NO puede ver el catálogo? → 11-catalog-unavailable
   ├─ ¿Redis caído?                  → 05-redis-cache-down
   └─ ¿Redis vivo pero hit ratio bajo? → 08-redis-cache-hit-ratio-low
¿Funciona pero lento?                → 02-api-p95-high
   └─ ¿CPU de Cloud Run al límite?   → 12-server-cpu-high
¿Funciona, pero los emails / facturas / confirmaciones no llegan? → 04-outbox-backlog-creciente
¿Disco de Cloud SQL creciendo?       → 06-cloud-sql-disk-full
```

---

## Convenciones del entorno (comunes a todos los runbooks)

> Los runbooks citan estos nombres literalmente. Si cambia la infra, se cambia **aquí y en los runbooks a la vez**.

### Plataforma (heredada del Bloque 4, GCP)

| Pieza | Nombre en producción | Nombre en pre-producción |
|---|---|---|
| Proyecto GCP | `oms-production` | `oms-staging` |
| Región primaria / DR | `europe-west3` (Frankfurt) / `europe-west1` (Bélgica) | `europe-west3` |
| Cloud SQL (Postgres 16, HA regional) | `oms-pg-prod` | `oms-pg-staging` |
| Réplica DR (cross-region, async) | `oms-pg-dr` (europe-west1) | — |
| Base de datos / usuarios | `oms` · app: `oms_app` · operación: `oms_ops` | igual |
| Memorystore Redis (STANDARD_HA) | `oms-redis-prod` | `oms-redis-staging` |
| Cloud Run: storefront (Catalog + IAM) | `oms-catalog` | `oms-catalog` |
| Cloud Run: transaccional (Orders + Inventory + Payments + Billing) | `oms-orders` | `oms-orders` |
| Cloud Run: worker del outbox (sin ingress, `min-instances=1`) | `oms-outbox-relay` | `oms-outbox-relay` |
| Bastión de operación (IAP, sin IP pública) | `oms-ops-bastion` (zona `europe-west3-a`) | `oms-ops-bastion-stg` |
| VPC / Cloud NAT (salida a Stripe y al proveedor de email) | `oms-vpc` · NAT `oms-nat-euw3` (router `oms-nat-router`) | igual con sufijo `-stg` |
| Borde: dominio · LB · certificado | `www.oms.example.com` · forwarding rule `oms-web-https-fr`, proxy `oms-web-https-proxy`, URL map `oms-web-urlmap`, certificado gestionado `oms-web-cert` | `staging.oms.example.com` |
| Borde: Cloud Armor | política `oms-edge-policy` (regla 1000: throttle 300/min por IP; 1100: ban; prioridades 900–999 reservadas para reglas temporales de incidente) | igual |
| Backend services del LB | `oms-catalog`, `oms-orders` (NEG serverless `oms-orders-neg-euw3`; en DR `oms-orders-neg-euw1`) · backend bucket `oms-spa` (Cloud CDN) → bucket `oms-production-spa` | igual |
| Búsqueda | Elasticsearch 8 en GCE, MIG regional `oms-search` (3 nodos, europe-west3), `search.oms.internal:9200`. Full-text en ES; el **autocompletado** usa trigramas (`pg_trgm`) sobre `catalog.products` en Postgres | 1 nodo |
| Servicio en frío (DR) | `oms-orders` en `europe-west1` con `min-instances=0`, `max-instances=6` (la fórmula de capacidad queda en 370 < 380 si se activa) | — |
| Secretos (Secret Manager) | `oms-db-ops-password`, `oms-unleash-admin-token`, `oms-stripe-secret-key`, `oms-stripe-healthcheck-key` (restringida, solo lectura), `oms-redis-auth`, `oms-redis-ca`, `oms-jwt-signing-key`, `oms-email-provider-api-key`, `oms-gitlab-ops-token` | igual |

> El monolito modular (ADR-001) es **una única imagen**. Se despliega como 3 servicios de Cloud Run según su perfil de carga: lectura masiva, transaccional y worker. Así escalan por separado sin romper el monolito. La variable `OMS_ROLE` selecciona qué routers arranca cada servicio.

### Observabilidad

| Herramienta | URL |
|---|---|
| Prometheus (Managed Service for Prometheus, frontend) | `https://prometheus.oms.example.com` |
| Grafana | `https://grafana.example.com` (dashboards `oms-overview`, `oms-db-overview`, `oms-payments`, `oms-cache`, `oms-outbox`, `oms-deploys`) |
| Alertmanager → PagerDuty | servicio PagerDuty `OMS Production` |
| Runbooks publicados | `https://runbooks.example.com/<slug>` |
| Feature flags (Unleash self-hosted) | `https://flags.oms.example.com`, proyecto `oms` |

### Setup de sesión del on-call (copiar una vez al empezar el incidente)

```bash
# 1. Proyecto y región
gcloud config set project oms-production
export REGION=europe-west3

# 2. Helper para consultar Prometheus desde la terminal
export PROM_URL=https://prometheus.oms.example.com
promq() { curl -sG "$PROM_URL/api/v1/query" --data-urlencode "query=$1" \
            | jq -r '.data.result[] | "\(.metric | del(.__name__) | tostring)  \(.value[1])"'; }

# 3. Túnel a Cloud SQL vía bastión (IP privada, nunca pública)
gcloud compute ssh oms-ops-bastion --zone=europe-west3-a --tunnel-through-iap \
  -- -N -L 5432:localhost:5432 &      # en el bastión corre cloud-sql-proxy --private-ip oms-production:europe-west3:oms-pg-prod
export PGHOST=127.0.0.1 PGPORT=5432 PGDATABASE=oms PGUSER=oms_ops
export PGPASSWORD="$(gcloud secrets versions access latest --secret=oms-db-ops-password)"

# 4. Feature flags (Unleash admin API). Encender/apagar un flag es idempotente
export UNLEASH_URL=https://flags.oms.example.com
export UNLEASH_TOKEN="$(gcloud secrets versions access latest --secret=oms-unleash-admin-token)"
flag_on()  { curl -sf -X POST "$UNLEASH_URL/api/admin/projects/oms/features/$1/environments/production/on"  -H "Authorization: $UNLEASH_TOKEN" && echo "flag $1 = ON"; }
flag_off() { curl -sf -X POST "$UNLEASH_URL/api/admin/projects/oms/features/$1/environments/production/off" -H "Authorization: $UNLEASH_TOKEN" && echo "flag $1 = OFF"; }
flag_get() { curl -sf "$UNLEASH_URL/api/admin/projects/oms/features/$1" -H "Authorization: $UNLEASH_TOKEN" \
            | jq -r '.environments[] | select(.name=="production") | "\(.name): enabled=\(.enabled)"'; }
# Rollout parcial (estrategia flexibleRollout ya creada en el flag): flag_rollout <flag> <0-100>
flag_rollout() {
  local sid; sid=$(curl -sf "$UNLEASH_URL/api/admin/projects/oms/features/$1/environments/production/strategies" \
      -H "Authorization: $UNLEASH_TOKEN" | jq -r '.[] | select(.name=="flexibleRollout") | .id' | head -1)
  curl -sf -X PUT "$UNLEASH_URL/api/admin/projects/oms/features/$1/environments/production/strategies/$sid" \
    -H "Authorization: $UNLEASH_TOKEN" -H "Content-Type: application/json" \
    -d "{\"name\":\"flexibleRollout\",\"parameters\":{\"rollout\":\"$2\",\"stickiness\":\"random\",\"groupId\":\"$1\"}}" \
    && echo "flag $1 rollout = $2 %"; }

# 5. Ejecutar un comando en el bastión (redis-cli, curl a Elasticsearch, etc.: solo tienen red privada)
bastion() { gcloud compute ssh oms-ops-bastion --zone=europe-west3-a --tunnel-through-iap --command "$1"; }
#    En el bastión hay dos wrappers: `oms-redis-cli` (TLS, puerto 6378, AUTH de oms-redis-auth)
#    y `oms-es` (curl a https://search.oms.internal:9200 con la CA del clúster y la API key de operación).
```

> Los flags los lee la app cada 15 s (SDK de Unleash con polling). Un cambio de flag tarda **≤ 15 s** en aplicarse y **no requiere redeploy**.

### Catálogo de feature flags operativos (kill switches)

| Flag | Efecto cuando está ON | Lo usa el runbook |
|---|---|---|
| `orders.batch-jobs-paused` | Pausa los jobs batch (export CSV FR-038, reconciliación nocturna) | 01, 06, 12 |
| `orders.read-from-replica` | Las lecturas de `GET /api/orders*` van a la réplica de lectura | 01, 02 |
| `payments.stripe-circuit-force-open` | Abre el circuit breaker de Stripe: los pedidos se aceptan como `PENDING_PAYMENT` y el cobro se reintenta en cola | 03, 10 |
| `catalog.cache-bypass` | El catálogo deja de llamar a Redis y va directo a Postgres (con rate limit propio) | 05, 08 |
| `catalog.serve-stale` | Sirve la última versión cacheada aunque el TTL haya expirado (stale-while-revalidate) | 05, 08, 09, 11 |
| `catalog.search-suggestions-disabled` | Desactiva autocompletado y sugerencias (la parte cara de la búsqueda) | 02, 11, 12 |
| `billing.pdf-generation-paused` | Pausa la generación de PDF de factura (el evento se queda en el outbox, no se pierde) | 04, 12 |
| `checkout.maintenance-page` | Muestra "estamos teniendo problemas, tu carrito está guardado" en lugar de un error genérico. **Sigue respondiendo 503 con `Retry-After`**, así que sigue contando en el SLO (no maquilla el budget) | 09, 10 |
| `payments.stripe-fast-retry` | Timeout de Stripe a 1.200 ms y 1 reintento con la **misma** idempotency key | 03 |
| `payments.retry-queue-paused` | El worker `payment-retry` no drena la cola `payments.retry_queue` (para drenar con control tras recuperarse Stripe) | 03 |
| `cart.local-only` | La SPA trabaja solo con el carrito de `localStorage` (que siempre guarda una copia); `POST /api/orders` revalida precio y stock | 05 |
| `catalog.read-from-replica` | Las lecturas del catálogo (misses de caché) van a `oms-pg-prod-replica` | 05, 08 |
| `catalog.bulk-import-paused` | El importador del backoffice se para entre lotes (evita invalidaciones masivas de caché) | 08 |
| `catalog.search-fallback-postgres` | Si Elasticsearch falla, búsqueda básica por nombre con `pg_trgm` (con rate limit) | 11 |
| `release.<ticket>-<slug>` | Flag de **release** de cada funcionalidad nueva (p. ej. `release.oms-2291-installments`), declarado en el MR. Apagarlo es la primera acción ante un canary fallido | 07 |
| `DEPLOY_FREEZE` (variable de GitLab CI, no Unleash) | El job `check-deploy-window` bloquea todos los despliegues | 07 |

> Flags usados con **rollout parcial** (`flag_rollout`): `payments.stripe-circuit-force-open` (cierre gradual del breaker; tras apagarlo hay que dejarlo otra vez al 100 % para que el siguiente `flag_on` actúe sobre todos) y `catalog.cache-bypass` (warm-up de la caché).

### Automatizaciones compartidas (V3 de varios runbooks)

| Pieza | Qué hace | Runbooks | Salud vigilada por |
|---|---|---|---|
| `oms-ops-flag-automator` (Cloud Run) | Recibe webhooks de Alertmanager (ruta `automation`) y aplica flags según `automator-rules.yaml`, con apagado automático cuando la alerta se resuelve. Publica cada cambio en `#oms-alerts` | 02, 04, 05, 08, 09, 11, 12 | `FlagAutomatorStale` ([`../observability/ops-automation-alerts.yaml`](../observability/ops-automation-alerts.yaml)) |
| `oms-ops-kill-idle` (Cloud Run Job, cada hora) | [`scripts/db-pool-saturated.sh`](../scripts/db-pool-saturated.sh) con `--idle-minutes 30` | 01 | `KillIdleConnectionsCronStale` |
| `oms-ops-stripe-breaker` (Cloud Run Job, cada minuto) | Fuerza o cierra en rampa el breaker de Stripe | 03 | `StripeBreakerControllerStale` |
| `canary-analysis` (job de GitLab CI) | Compara canary y estable y hace auto-rollback | 07 | `CanaryAnalysisNotRunning` |
| `oms-ops-db-housekeeping` (domingo 04:00) | Purga el outbox publicado (archivo en `gs://oms-prod-outbox-archive`) y vigila slots | 04, 06 | `DBHousekeepingJobStale` |
| `oms-ops-pitr-drill` (mensual) | Restauración PITR de prueba en un clon efímero | 09 | `PITRRestoreDrillStale` |
| `oms-catalog-cache-warmer` (Cloud Run Job) | Pre-calienta los productos y listados top | 05, 08 | `CatalogCacheWarmerStale` |
| `oms-ops-checkout-triage` | Triage automático del checkout y publicación de la capa sospechosa | 10 | `CheckoutTriageAutomationStale` |
| `oms-ops-armor-drift` (cada hora) | Compara `oms-edge-policy` con Terraform | 12 | `CloudArmorPolicyCheckStale` |

> Regla: **toda automatización publica `ops_job_last_success_timestamp_seconds{job=...}` y tiene una alerta si deja de hacerlo.** Una automatización que falla en silencio nos devuelve al V1 sin que nadie se entere.

### Métricas de referencia

El nombre exacto de cada métrica, su origen y por qué existe están en [`../observability/instrumentation.md`](../observability/instrumentation.md). Los runbooks solo usan métricas de esa tabla.

## Plantillas

`_template/` contiene los 3 ficheros base. Copia esa carpeta a `0X-tu-runbook/` y rellena los TODO. La plantilla **conserva sus TODO a propósito**.
