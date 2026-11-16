# Plan de instrumentación del OMS

> Qué se instrumenta en cada módulo del OMS y por qué.
> Cada métrica se traza a un quality attribute de la spec (`anexo-arquitectura/01-inventario-restricciones.md`) y a las restricciones del enunciado del Bloque 5.

---

## Regla guía (del Vídeo 1)

> **Si lo especificas, lo mides. Si lo mides, lo alertas.**

Cada fila de las tablas siguientes se defiende con la frase:
"esta métrica existe porque la spec dice X, y la usamos para verificar Y."
Si una métrica no tiene fila, **no se instrumenta**.

### Umbrales de la spec que gobiernan este documento

| ID | Umbral | Dónde se mide |
|---|---|---|
| NFR-PERF-002 (enunciado B5) | p95 < 250 ms en endpoints críticos | SLO 2 de Orders, alerta `APILatencyP95High` (runbook 02) |
| NFR-PERF-003 (enunciado B5) | p99 < 800 ms | SLO 2 de Orders, alerta `APILatencyP99High` (runbook 02) |
| NFR-PERF-001 (anexo) | `GET /api/products` p95 < 150 ms gracias a Redis | `CatalogLatencyHigh`, hit ratio (runbook 08) |
| NFR-AVAIL-001 | Checkout 99,9 % mensual, multi-AZ con failover automático | SLO 1 de Orders, `CheckoutUnavailable` (runbook 10) |
| NFR-AVAIL-002 | Catálogo 99,95 %; sirve aunque la DB falle parcialmente | `CatalogUnavailable` (runbook 11), `RedisCacheDown` (runbook 05) |
| OPS-005 | Backups con PITR 14 d, RTO ≤ 4 h | `CloudSQLBackupMissing`, runbook 09 |
| OPS-007 | Degradación elegante | Estado de circuit breakers y flags, runbooks 03, 05 y 09 |
| REG-GDPR-001 | Datos solo en regiones UE | Política de organización GCP + `gcp_resource_outside_eu` (auditoría) |
| FR-014 | Email de confirmación en < 5 min | `outbox_oldest_pending_age_seconds` (runbook 04) |

### Convenciones de etiquetas

- `job`: servicio de Cloud Run (`oms-catalog`, `oms-orders`, `oms-outbox-relay`).
- `module`: módulo hexagonal (`catalog`, `inventory`, `orders`, `payments`, `billing`, `iam`).
- `endpoint`: **plantilla** de ruta (`/api/orders/{id}`), nunca la URL concreta. Así se evita la explosión de cardinalidad y no se filtran IDs en las etiquetas.
- `code`: código HTTP. `revision`: revisión de Cloud Run, necesaria para el análisis de canary.
- `synthetic="true"`: lo añade la app a las peticiones de las probes sintéticas (cabecera `X-OMS-Synthetic`, firmada). Los SLIs las excluyen.
- **Buckets obligatorios de los histogramas** (los SLIs se calculan con el bucket exacto, no por interpolación): `http_request_duration_seconds` → `0.05, 0.1, 0.15, 0.25, 0.4, 0.5, 0.8, 1, 2.5, 5`; `payment_authorization_duration_seconds` y `payment_provider_request_duration_seconds` → `0.5, 1, 2, 2.5, 3, 5, 10`. Cambiar un bucket es cambiar el SLO: requiere PR con revisión de SRE.
- **Prohibido** como etiqueta: `user_id`, `order_id`, `email` o cualquier PII (GDPR + cardinalidad).

### Origen de las métricas

| Origen | Cómo llega a Prometheus | Ejemplos |
|---|---|---|
| App (OpenTelemetry SDK → OTLP → Managed Prometheus) | Métricas de negocio y de HTTP emitidas por el monolito | `http_requests_total`, `db_pool_*`, `outbox_*`, `payment_*` |
| `postgres_exporter` (sidecar en el bastión, rol `pg_monitor`) | Scrape cada 30 s | `pg_up`, `pg_stat_activity_count`, `pg_stat_database_deadlocks` |
| `stackdriver_exporter` (Cloud Monitoring → Prometheus) + recording rules que renombran | Métricas gestionadas de GCP | `cloudsql_*`, `redis_*`, `run_*` |
| `blackbox_exporter` (probes sintéticas desde europe-west1, **fuera** de la región primaria) | `catalog-home` y `stripe-api` cada 30 s; `checkout-e2e` cada **60 s** (crea y cancela un pedido real: a 30 s serían ~2.880 pedidos sintéticos al día, más que los reales) | `probe_success`, `probe_duration_seconds`, `probe_ssl_earliest_cert_expiry` |
| Logs del HTTPS Load Balancer → métricas basadas en logs | Contadores con `backend_service`, `code`, `path` (plantilla) y `method` | `loadbalancer_requests_total`, `loadbalancer_backend_5xx_total` (5xx generados por el LB cuando el backend no responde) |
| Jobs de automatización → Pushgateway | Cada job publica al terminar con éxito | `ops_job_last_success_timestamp_seconds{job}` |

---

## Módulo Catalog (`oms-catalog`)

| Quality attribute | Métrica | SLI derivado | Alerta asociada |
|---|---|---|---|
| NFR-PERF-001 / NFR-PERF-002 (p95 < 150 ms / < 250 ms) | `http_request_duration_seconds_bucket{job="oms-catalog", endpoint=~"/api/products.*"}` | `histogram_quantile(0.95, sum by (le, endpoint) (rate(...[5m])))` | `APILatencyP95High` (runbook 02) |
| NFR-PERF-001 (la caché Redis funciona) | `cache_requests_total{cache="catalog", result="hit\|miss\|error"}` | `hit / (hit + miss)` en 15 min | `RedisCacheHitRatioLow` (runbook 08) |
| NFR-AVAIL-002 (disponibilidad del catálogo) | `http_requests_total{job="oms-catalog", code}` + `probe_success{probe="catalog-home"}` | no-5xx / total (excluye `/healthz`) | `CatalogUnavailable` (runbook 11) |
| NFR-AVAIL-002 (sirve aunque la DB falle) | `cache_stale_served_total{cache="catalog"}` | % de respuestas servidas en stale | informativa: panel en `oms-cache` |
| OPS-007 (degradación ante caída de Redis) | `circuit_breaker_state{breaker="redis"}`, `cache_requests_total{result="bypass"}` (con `catalog.cache-bypass` activo) | — | `RedisCacheDown`, `CatalogCacheBypassLeftOn` (runbook 05) |
| Diagnóstico del hit ratio | `cache_requests_total` con las etiquetas `key_type` (listing, product, categories) y `revision`; `cache_invalidations_total{cache, reason="event\|bulk_import\|manual"}` | — | `RedisCacheHitRatioLow`, `CatalogBulkInvalidationInProgress` (runbook 08) |
| NFR-AVAIL-002 en el borde (el cliente llega) | `loadbalancer_requests_total{backend_service=~"oms-catalog\|oms-spa", code}`, `probe_ssl_earliest_cert_expiry{probe="catalog-home"}` | — | `CatalogEdgeDenySpike`, `CatalogTLSCertExpiringSoon` (runbook 11) |
| NFR-PERF-004 (búsqueda p95 < 300 ms) | `http_request_duration_seconds_bucket{endpoint="/api/search"}` | p95 en 5 min | `SearchLatencyHigh` (dentro del runbook 02) |

## Módulo Inventory (dentro de `oms-orders`)

| Quality attribute | Métrica | SLI | Alerta |
|---|---|---|---|
| FR-001 (reserva atómica disponible) | `stock_reservations_total{outcome="reserved\|out_of_stock\|error"}` | `(reserved + out_of_stock) / total`; quedarse sin stock es una respuesta correcta | `StockReservationErrors` (> 1 % en 10 min, warning) |
| NFR-PERF-002 (la reserva no penaliza el checkout) | `stock_reservation_duration_seconds_bucket` | p95 < 50 ms (presupuesto interno dentro de los 250 ms) | se ve en la traza; sin alerta propia (la cubre el runbook 02) |
| FR-001 (cero double-reserve) | `stock_double_reservation_detected_total` (lo emite el job de reconciliación) | debe ser **0** | `StockDoubleReservation` (critical en cuanto `increase(...[1h]) > 0`) |
| Contención de locks | `pg_locks_waiting{schema="inventory"}` | — | diagnóstico en el runbook 01 (paso 3) |

## Módulo Orders (`oms-orders`)

| Quality attribute | Métrica | SLI | Alerta |
|---|---|---|---|
| NFR-AVAIL-001: disponibilidad de `POST /api/orders` | `http_requests_total{job="oms-orders", endpoint="/api/orders", method="POST", code}` | [`slos-orders.md` SLO 1](../slos/slos-orders.md): no-5xx / (total − 4xx) en 28 d | `OrdersAvailabilityBurnRateFast` / `...Slow` |
| NFR-PERF-002/003: latencia de consulta de pedido | `http_request_duration_seconds_bucket{job="oms-orders", endpoint=~"/api/orders.*", method="GET"}` | [`slos-orders.md` SLO 2](../slos/slos-orders.md): % < 250 ms y % < 800 ms | `OrdersLatencyBurnRate`, `APILatencyP95High` (runbook 02) |
| Transacciones ACID (rollback rate) | `orders_transaction_rollbacks_total{reason}` / `orders_created_total` | ratio en 15 min (base histórica < 0,5 %) | `OrdersRollbackRateHigh` (> 2 % en 15 min, warning) |
| FR-014: confirmación en < 5 min | `order_confirmation_lag_seconds_bucket` (de `PaymentCaptured` a `OrderConfirmed`) | p99 < 60 s | `OutboxBacklogGrowing` (runbook 04) |
| Capacidad de BD desde la app | `db_pool_in_use`, `db_pool_size`, `db_pool_pending_acquires`, `db_pool_acquire_duration_seconds_bucket` | — (infra, **no** SLI) | `DBConnectionPoolSaturated` (runbook 01) |

## Módulo Payments (`oms-orders`)

| Quality attribute | Métrica | SLI | Alerta |
|---|---|---|---|
| Disponibilidad de la autorización (experiencia de pago) | `payment_authorizations_total{outcome="approved\|declined\|failed\|queued"}`, `payment_authorization_duration_seconds_bucket` | [`slos-payments.md`](../slos/slos-payments.md): respuesta definitiva en < 3 s / intentos válidos, 28 d | `PaymentsAuthBurnRateFast` / `...Slow` |
| Salud externa de Stripe | `payment_provider_requests_total{provider="stripe", operation, outcome="success\|card_error\|api_error\|timeout"}`, `payment_provider_request_duration_seconds_bucket` | (sin SLI propio: da visibilidad de cuánto budget se lleva un tercero) | `StripeGatewayDegraded` (runbook 03) |
| OPS-007: degradación elegante | `circuit_breaker_state{breaker="stripe"}` (0 = cerrado, 1 = semiabierto, 2 = abierto), `payment_retry_queue_depth` | — | `StripeCircuitOpen` (info), `PaymentRetryQueueStuck` (runbook 03) |
| Separar "es Stripe" de "es nuestra salida" | `probe_success{probe="stripe-api"}`, `probe_duration_seconds{probe="stripe-api"}` (blackbox contra `api.stripe.com/v1/balance` con la clave restringida `oms-stripe-healthcheck-key`) | — | diagnóstico del runbook 03 |
| REG-PCI-002: sin PAN en logs | `log_pan_pattern_detected_total` (filtro del log pipeline) | debe ser **0** | `PANDetectedInLogs` (critical, va a seguridad y no a on-call) |

## Módulo Billing (`oms-orders`, consumidor de eventos)

| Quality attribute | Métrica | SLI | Alerta |
|---|---|---|---|
| FR-022: factura generada tras confirmar | `invoices_generated_total`, `invoice_generation_failures_total{reason}` | generadas / `OrderConfirmed` en 1 h | `InvoiceGenerationFailing` (> 5 % en 30 min, warning) |
| REG-IVA-002: numeración correlativa sin huecos | `invoice_number_gaps_total` (check nocturno) | debe ser **0** | `InvoiceNumberGap` (critical, horario laboral → finanzas + equipo) |
| Latencia de la factura | `invoice_generation_lag_seconds_bucket` | p95 < 10 min | se ve en el runbook 04 (comparte outbox) |

## Módulo IAM (`oms-catalog`)

| Quality attribute | Métrica | SLI | Alerta |
|---|---|---|---|
| Éxito de login (el usuario puede entrar) | `auth_login_attempts_total{outcome="success\|bad_credentials\|error"}` | `(success + bad_credentials) / total` (meter mal la contraseña no es un fallo del sistema) | `LoginErrorRateHigh` (> 2 % en 10 min) |
| Latencia de emisión y refresco de tokens | `auth_token_issue_duration_seconds_bucket` | p95 < 100 ms | dentro de `APILatencyP95High` |
| NFR-SEC-004 / REG-GDPR-005: intentos sospechosos | `auth_login_attempts_total{outcome="bad_credentials"}` por ASN (en logs, no en etiquetas) + `auth_lockouts_total` | — | `CredentialStuffingSuspected` (> 10× la base en 15 min → seguridad) |
| NFR-SEC-004: MFA de admin | `auth_admin_login_without_mfa_total` | debe ser **0** | `AdminLoginWithoutMFA` (critical → seguridad) |

## Outbox (shared, `oms-outbox-relay`)

| Quality attribute | Métrica | SLI | Alerta |
|---|---|---|---|
| ADR-009 / FR-014: entrega fiable y a tiempo de eventos | `outbox_pending_events` (gauge), `outbox_oldest_pending_age_seconds` | edad del evento más antiguo < 60 s | `OutboxBacklogGrowing` (runbook 04) |
| Relay vivo | `outbox_relay_last_success_timestamp_seconds`, `outbox_events_published_total`, `outbox_publish_failures_total{handler}` | — | `OutboxRelayStalled` (runbook 04) |
| Fallos por consumidor (bulkhead) | `outbox_handler_duration_seconds_bucket{handler, outcome}` (handlers: `orders.confirm-on-payment`, `inventory.consolidate-reservation`, `billing.generate-invoice`, `notifications.order-confirmation-email`, `gdpr.deletion-workflow`) | — | `OutboxHandlerFailing` (runbook 04) |
| Poison messages y eventos aparcados | `outbox_dead_letter_events` (gauge), `outbox_deferred_events{handler}` (aparcados por un flag, p. ej. `billing.pdf-generation-paused`; se excluyen de la edad y de los pendientes para que la alerta no se quede disparada) | — | `OutboxDeadLetterNotEmpty` (runbook 04) |

---

## Infraestructura compartida

> Las métricas de infraestructura **nunca** son SLI. Se usan para diagnosticar y para alertas **preventivas**: avisan antes de que el SLI de usuario empiece a caer.

### Cloud SQL Postgres (`oms-pg-prod`)

| Métrica | Por qué | Alerta |
|---|---|---|
| `db_pool_in_use / db_pool_size` (app) | Saturación de conexiones = 5xx en escrituras | `DBConnectionPoolSaturated` (runbook 01) |
| `pg_stat_activity_count{state}` (postgres_exporter) | Distinguir carga real de sesiones zombi | diagnóstico del runbook 01 |
| `rate(pg_stat_database_deadlocks{datname="oms"}[5m])` | Detectar bloqueos (contención en `inventory`) | `PostgresDeadlocks` (> 0,1/s durante 10 min, warning) |
| `pg_up`, `cloudsql_up`, `db_connection_errors_total{job, error="auth_failed\|connection_refused\|timeout"}` (app) | El motor responde y la app puede conectar | `PostgresUnavailable` (runbook 09) |
| `cloudsql_available_for_failover` | 0 mientras la HA no está lista (justo después de un failover) | `CloudSQLFailoverInProgress` (runbook 09) |
| `pg_replication_slot_active`, `pg_replication_slot_retained_wal_bytes{slot_name}` (query personalizada de postgres_exporter) | Un slot inactivo retiene WAL y llena el disco | `CloudSQLReplicationSlotInactive` (runbook 06) |
| `cloudsql_disk_bytes_used / cloudsql_disk_quota_bytes`, `predict_linear(...)` frente a `oms:cloudsql_disk_autoincrease_limit_bytes` (300 GiB) | Prevenir disco lleno (el disco crece solo, pero el auto-increase tiene límite y **no se puede reducir**) | `CloudSQLDiskUsageHigh`, `CloudSQLDiskWillFillIn24h`, `CloudSQLDiskAutoIncreaseNearLimit` (runbook 06) |
| `cloudsql_cpu_utilization` | Separar "BD lenta" de "app lenta" | diagnóstico de los runbooks 02 y 12 |
| `cloudsql_replication_lag_seconds{replica="oms-pg-dr"}` | RPO ≤ 1 h (OPS-005) | `DRReplicaLagHigh` (> 300 s durante 15 min) |
| `cloudsql_last_backup_success_timestamp_seconds` | PITR 14 d y backups (OPS-005) | `CloudSQLBackupMissing` (> 26 h sin backup) |

### Memorystore Redis (`oms-redis-prod`)

| Métrica | Por qué | Alerta |
|---|---|---|
| `cache_requests_total{result}` (app) → hit ratio | NFR-PERF-001: sin caché el catálogo no cumple 150 ms | `RedisCacheHitRatioLow` (runbook 08) |
| `redis_up` + `cache_requests_total{result="error"}` | NFR-AVAIL-002 / OPS-007 | `RedisCacheDown` (runbook 05) |
| `redis_memory_used_bytes / redis_memory_max_bytes`, `rate(redis_evicted_keys_total[5m])` | Las evictions explican un hit ratio bajo | diagnóstico del runbook 08 |

### Cloud Run (`oms-catalog`, `oms-orders`, `oms-outbox-relay`)

| Métrica | Por qué | Alerta |
|---|---|---|
| `run_container_cpu_utilization_bucket` (distribución de Cloud Monitoring → p99 por servicio en la recording rule `job:run_container_cpu_utilization:p99_5m`) | Saturación de CPU = latencia | `CloudRunCPUSaturated` (runbook 12) |
| `run_instance_count` vs `run_max_instances` | Si el autoscaling choca con su techo, ya no hay más capacidad | `CloudRunAtMaxInstances` (runbook 12) |
| `run_container_startup_latency_seconds` (cold starts) | Los cold starts suben el p99 | diagnóstico del runbook 02 |
| `http_requests_total{revision}` y latencia por `revision` | Análisis de canary | `CanaryRolloutFailed` (runbook 07) |
| `run_revision_traffic_percent{job, revision, tag="stable\|canary\|previous"}` (de `status.traffic[]` de Cloud Run) | Saber qué revisión es la canary y en qué etapa está | `CanaryRolloutStuck` (runbook 07) |

### Probes sintéticas (blackbox, desde europe-west1)

| Probe | Qué hace | Alerta |
|---|---|---|
| `checkout-e2e` | Login de usuario sintético → carrito → `POST /api/orders` con tarjeta de test de Stripe en **modo test** (cuenta separada) → cancelación | `CheckoutUnavailable` (runbook 10) |
| `catalog-home` | `GET /` (CDN) + `GET /api/products?page=1` | `CatalogUnavailable` (runbook 11) |

### Automatización y flags

| Métrica | Por qué | Alerta |
|---|---|---|
| `ops_job_last_success_timestamp_seconds{job}` (jobs: `kill-idle-connections`, `flag-automator`, `stripe-breaker-controller`, `canary-analysis`, `db-housekeeping`, `pitr-restore-drill`, `catalog-cache-warmer`, `checkout-triage-selftest`, `armor-drift-check`) | Una automatización que falla en silencio nos devuelve al V1 | una `*Stale` por job (ver [runbooks/README](../runbooks/README.md#automatizaciones-compartidas-v3-de-varios-runbooks)) |
| `feature_flag_enabled{job, flag}` (gauge 0/1 del SDK de Unleash dentro de la app) | Saber qué kill switches están activos **según la app**, no según Unleash. Detecta flags olvidados en ON | `CatalogCacheBypassLeftOn` (runbook 05); panel de flags en `oms-overview` |

---

## Logs estructurados

Todos los módulos del OMS emiten logs JSON en stdout (NFR-MAINT-004). Cloud Logging los recoge con retención de 90 d (OPS-004) en un bucket de logs ubicado en `europe-west3` (REG-GDPR-001).

```json
{
  "ts":         "2026-01-15T10:23:47.123Z",
  "level":      "ERROR",
  "service":    "oms-orders",
  "module":     "payments",
  "revision":   "oms-orders-00042-xyz",
  "trace_id":   "4bf92f3577b34da6a3ce929d0e0e4736",
  "span_id":    "00f067aa0ba902b7",
  "request_id": "req_7f3a",
  "user_ref":   "u_9c1e…(hash)",
  "event":      "payment.authorization.failed",
  "error_code": "stripe_api_error",
  "duration_ms": 3012
}
```

**Campos incluidos y por qué:**
- `trace_id` / `span_id`: correlación log ↔ traza (NFR-MAINT-003). Es lo primero que se pega en Tempo o Cloud Trace.
- `revision`: hace posible decir "el error empezó con la revisión 00042" (runbook 07).
- `event`: clave estandarizada `<modulo>.<entidad>.<verbo>` (p. ej. `payment.authorization.failed`, `outbox.handler.failed`, `db.connection.failed`). Es la que se usa en las queries de los runbooks (`jsonPayload.event="payment.authorization.failed"`).
- `user_ref`: **hash** HMAC-SHA256 del `user_id` con una clave rotada en Secret Manager. Permite seguir a un usuario dentro de un incidente sin guardar su identificador en claro.

**Campos excluidos a propósito:**
- Email, nombre, dirección y teléfono: PII (REG-GDPR-001, NFR-SEC-001). No se loguean nunca, ni siquiera enmascarados.
- Cualquier dato de tarjeta (REG-PCI-002). Solo se loguean el `payment_intent_id`, `stripe_request_id` y `stripe_http_status` de Stripe, que no son datos de tarjeta (con `error_code` normalizado: `stripe_timeout`, `stripe_connection_error`, `stripe_authentication_error`, `stripe_rate_limit`). Un filtro del log router descarta y cuenta (`log_pan_pattern_detected_total`) cualquier línea que encaje con un patrón de PAN (Luhn sobre 13–19 dígitos).
- Cuerpos de request y response: aumentan el coste ×10 y son el vector más habitual de fuga de PII.

**Niveles en producción:** `INFO` y superiores. `DEBUG` se activa por módulo y durante 30 min máximo con el flag `observability.debug-logs-<module>`, que se apaga solo.

---

## Trazas

- OpenTelemetry SDK con propagación W3C `traceparent`, exportando a Cloud Trace (europe-west3).
- **Sampling:** 10 % head-based para tráfico normal y **100 % de las trazas con error o con latencia > 800 ms** (tail sampling en el OTel Collector). Las trazas que importan en un incidente son justo esas.
- Spans obligatorios dentro de `POST /api/orders`: `catalog.snapshot`, `inventory.reserve`, `payments.authorize` (con el atributo `stripe.request_id`), `orders.save` y `outbox.write`. Con ellos el runbook 02 puede responder "¿qué tramo se come el p95?".

---

## Coste de la telemetría (estimación mensual)

Base: ~200 req/s de media y ~2.000 pedidos/día (anexo), con el presupuesto de infra de 800 €/mes (OPS-003).

| Señal | Volumen | Precio de referencia | Coste/mes |
|---|---|---|---|
| Logs (`INFO`+, sin cuerpos) | ~200 req/s × ~2 líneas × ~400 B ≈ 14 GB/día ≈ 420 GB/mes | Cloud Logging: 50 GiB/mes gratis, después ~0,50 $/GiB | ≈ 185 $ |
| Logs: exclusión de health checks y probes en el log router | −30 % | — | ≈ −55 $ |
| Métricas (Managed Prometheus) | ~6.000 series activas, scrape cada 30 s → ~520 M muestras/mes | ~0,06 $/M muestras (primer tramo) | ≈ 31 $ |
| Trazas (10 % + errores) | ~52 M spans/mes | Cloud Trace: 2,5 M gratis, después 0,20 $/M | ≈ 10 $ |
| Probes sintéticas y uptime checks | 2 probes cada 30 s | dentro de la capa gratuita | 0 $ |
| **Total** | | | **≈ 170 $/mes (~21 % del presupuesto de infra)** |

> 21 % es alto para 800 €/mes. Tenemos tres palancas, en este orden: (1) bajar a `WARN` los logs de acceso de `oms-catalog`, que son ~70 % del volumen y ya tienen su métrica HTTP; (2) bajar el head sampling del 10 % al 5 %; (3) scrape cada 60 s para las métricas de infraestructura. **No** se toca: el 100 % de trazas con error y la resolución de 30 s de las métricas de SLO.
