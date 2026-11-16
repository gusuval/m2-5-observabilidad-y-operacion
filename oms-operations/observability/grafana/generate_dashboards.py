#!/usr/bin/env python3
"""Genera los dashboards de Grafana del OMS (JSON importable, schemaVersion 39).

Uno por cada UID que citan los runbooks y las alertas (dashboard_url):
  oms-overview, oms-db-overview, oms-payments, oms-cache, oms-outbox, oms-deploys

Se generan desde código (y no a mano en la UI) para que:
  - las queries PromQL sean las MISMAS que las de los SLOs y las alertas (una sola fuente),
  - cada panel enlace a su runbook (links), que es lo que hace útil el dashboard a las 3 AM,
  - los cambios se revisen en PR como cualquier otro código.

Uso:  python3 generate_dashboards.py      → escribe ./<uid>.json
"""
import json
import pathlib

OUT = pathlib.Path(__file__).resolve().parent
RUNBOOKS = "https://runbooks.example.com"
DS = {"type": "prometheus", "uid": "${datasource}"}

_next_id = 0


def _id():
    global _next_id
    _next_id += 1
    return _next_id


def target(expr, legend="", ref="A"):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref, "range": True}


def runbook_link(slug, title=None):
    return {"title": title or f"Runbook {slug}", "url": f"{RUNBOOKS}/{slug}", "targetBlank": True}


def timeseries(title, targets, x, y, w=12, h=8, unit="short", thresholds=None, runbook=None,
               description="", min_=None, max_=None):
    steps = [{"color": "green", "value": None}]
    for value, color in (thresholds or []):
        steps.append({"color": color, "value": value})
    defaults = {
        "unit": unit,
        "custom": {"lineWidth": 2, "fillOpacity": 10, "spanNulls": True,
                   "thresholdsStyle": {"mode": "line+area" if thresholds else "off"}},
        "thresholds": {"mode": "absolute", "steps": steps},
    }
    if min_ is not None:
        defaults["min"] = min_
    if max_ is not None:
        defaults["max"] = max_
    return {
        "id": _id(), "type": "timeseries", "title": title, "description": description,
        "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(*t) if isinstance(t, tuple) else t for t in targets],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "links": [runbook_link(runbook)] if runbook else [],
    }


def stat(title, expr, x, y, w=6, h=5, unit="percentunit", thresholds=None, runbook=None,
         description="", decimals=3):
    steps = [{"color": "red", "value": None}]
    for value, color in (thresholds or []):
        steps.append({"color": color, "value": value})
    return {
        "id": _id(), "type": "stat", "title": title, "description": description,
        "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(expr)],
        "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals,
                                     "thresholds": {"mode": "absolute", "steps": steps}},
                        "overrides": []},
        "options": {"colorMode": "background", "graphMode": "area", "reduceOptions":
                    {"calcs": ["lastNotNull"], "fields": "", "values": False}, "textMode": "auto"},
        "links": [runbook_link(runbook)] if runbook else [],
    }


def row(title, y):
    return {"id": _id(), "type": "row", "title": title, "collapsed": False,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}


def dashboard(uid, title, panels, tags, extra_vars=None, description=""):
    variables = [{
        "name": "datasource", "label": "Prometheus", "type": "datasource", "query": "prometheus",
        "current": {"text": "Managed Prometheus (oms-production)", "value": "oms-prod-prometheus"},
        "hide": 0,
    }]
    variables += extra_vars or []
    return {
        "uid": uid, "title": title, "description": description, "tags": ["oms"] + tags,
        "timezone": "Europe/Madrid", "schemaVersion": 39, "version": 1, "editable": True,
        "refresh": "30s", "time": {"from": "now-6h", "to": "now"},
        "templating": {"list": variables},
        "annotations": {"list": [{
            "name": "Despliegues", "datasource": DS, "enable": True, "iconColor": "purple",
            "expr": 'changes(run_revision_traffic_percent{tag="canary"}[2m]) > 0',
            "titleFormat": "Canary {{job}} {{revision}} → {{tag}}",
        }]},
        "links": [{"title": "Índice de runbooks", "url": f"{RUNBOOKS}/", "targetBlank": True, "type": "link"}],
        "panels": panels,
    }


def query_var(name, label, query, include_all=True):
    return {"name": name, "label": label, "type": "query", "datasource": DS,
            "query": {"query": query, "refId": "PrometheusVariableQueryEditor-VariableQuery"},
            "refresh": 2, "includeAll": include_all, "multi": True,
            "current": {"text": "All", "value": "$__all"} if include_all else {}}


JOB_VAR = query_var("job", "Servicio", 'label_values(http_requests_total{job=~"oms-.*"}, job)')
INSTANCE_VAR = query_var("instance", "Instancia", 'label_values(db_pool_size{job=~"$job"}, instance)')

POST_ORDERS = 'job="oms-orders", endpoint="/api/orders", method="POST"'


# ─────────────────────────────── oms-overview ───────────────────────────────
def overview():
    p = [row("SLOs y error budget (28 d rolling): error-budget-policy.md", 0)]
    p += [
        stat("SLO Orders · disponibilidad POST /api/orders (obj. 99,9 %)",
             "slo:orders_availability:ratio_28d", 0, 1, thresholds=[(0.999, "green")],
             runbook="10-checkout-unavailable", decimals=4),
        stat("SLO Orders · consultas < 250 ms (obj. 95 %)",
             "slo:orders_latency_250ms:ratio_28d", 6, 1, thresholds=[(0.95, "green")],
             runbook="02-api-p95-high"),
        stat("SLO Orders · consultas < 800 ms (obj. 99 %)",
             "slo:orders_latency_800ms:ratio_28d", 12, 1, thresholds=[(0.99, "green")],
             runbook="02-api-p95-high"),
        stat("SLO Payments · autorización definitiva < 3 s (obj. 99 %)",
             "slo:payments_authorization:ratio_28d", 18, 1, thresholds=[(0.99, "green")],
             runbook="03-stripe-payment-gateway-degraded"),
    ]
    budget = stat("Error budget restante (peor SLO → nivel de la política)",
                  "min(slo:error_budget_remaining:ratio)", 0, 6, w=8, h=6,
                  thresholds=[(0.1, "orange"), (0.5, "green")], decimals=1,
                  description="🟢 > 50 % · 🟡 10–50 % · 🔴 < 10 % (freeze) · ⚫ ≤ 0 %")
    budget["links"] = [{"title": "Política de error budget",
                        "url": f"{RUNBOOKS}/slos/error-budget-policy", "targetBlank": True}]
    p.append(budget)
    p.append(timeseries("Budget restante por SLO", [("slo:error_budget_remaining:ratio", "{{slo}}")],
                        8, 6, w=16, h=6, unit="percentunit", thresholds=[(0.1, "red"), (0.5, "yellow")]))

    p.append(row("Burn rate (alertas multiwindow)", 12))
    p += [
        timeseries("Burn rate Orders disponibilidad (1 h / 6 h)", [
            ("slo:orders_post_errors:ratio_rate1h / 0.001", "1 h"),
            ("slo:orders_post_errors:ratio_rate6h / 0.001", "6 h", "B"),
        ], 0, 13, thresholds=[(6, "orange"), (14.4, "red")], runbook="10-checkout-unavailable",
            description="Rápida: 14,4× (1 h). Lenta: 6× (6 h). Con mínimo de 20 peticiones/h."),
        timeseries("Payments: ratio de intentos malos (1 h)", [(
            '1 - (sum(rate(payment_authorization_duration_seconds_bucket{outcome=~"approved|declined", le="3"}[1h]))'
            ' / sum(rate(payment_authorization_duration_seconds_count{outcome!~"invalid_request|requires_action"}[1h])))',
            "malos")], 12, 13, unit="percentunit", thresholds=[(0.06, "orange"), (0.144, "red")],
            runbook="03-stripe-payment-gateway-degraded"),
    ]

    p.append(row("Golden signals por servicio", 21))
    p += [
        timeseries("Tráfico (req/s)", [('sum by (job) (rate(http_requests_total{job=~"$job"}[5m]))', "{{job}}")],
                   0, 22, w=8, unit="reqps"),
        timeseries("Errores 5xx (%)", [(
            'sum by (job) (rate(http_requests_total{job=~"$job", code=~"5.."}[5m]))'
            ' / sum by (job) (rate(http_requests_total{job=~"$job"}[5m]))', "{{job}}")],
            8, 22, w=8, unit="percentunit", thresholds=[(0.01, "orange"), (0.05, "red")],
            runbook="10-checkout-unavailable"),
        timeseries("Latencia p95 por endpoint crítico", [(
            'histogram_quantile(0.95, sum by (le, job, endpoint) (rate(http_request_duration_seconds_bucket{job=~"$job", endpoint=~"/api/orders.*|/api/products.*|/api/cart.*"}[5m])))',
            "{{job}} {{endpoint}}")], 16, 22, w=8, unit="s", thresholds=[(0.25, "orange"), (0.8, "red")],
            runbook="02-api-p95-high"),
        timeseries("p99 de POST /api/orders (incluye Stripe)", [(
            f'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{{{POST_ORDERS}}}[5m])))',
            "p99")], 0, 30, w=8, unit="s", thresholds=[(0.8, "red")], runbook="02-api-p95-high"),
        timeseries("Probes sintéticas (desde europe-west1)", [
            ('avg_over_time(probe_success{probe="checkout-e2e"}[5m])', "checkout-e2e"),
            ('avg_over_time(probe_success{probe="catalog-home"}[5m])', "catalog-home", "B"),
        ], 8, 30, w=8, unit="percentunit", min_=0, max_=1, runbook="10-checkout-unavailable"),
        timeseries("CPU Cloud Run p99 e instancias vs máximo", [
            ('job:run_container_cpu_utilization:p99_5m{job=~"$job"}', "CPU p99 {{job}}"),
            ('sum by (job) (run_instance_count{job=~"$job"}) / max by (job) (run_max_instances{job=~"$job"})',
             "instancias/max {{job}}", "B"),
        ], 16, 30, w=8, unit="percentunit", thresholds=[(0.85, "red")], runbook="12-server-cpu-high"),
    ]

    p.append(row("Dependencias (detalle en sus dashboards)", 38))
    p += [
        timeseries("Pool de BD (máx. por servicio)", [(
            "max by (job) (db_pool_in_use / db_pool_size)", "{{job}}")], 0, 39, w=6, unit="percentunit",
            thresholds=[(0.75, "orange"), (0.9, "red")], runbook="01-db-pool-saturated"),
        timeseries("Stripe: errores API/timeout", [(
            'sum(rate(payment_provider_requests_total{provider="stripe", outcome=~"api_error|timeout"}[5m]))'
            ' / sum(rate(payment_provider_requests_total{provider="stripe"}[5m]))', "error ratio")],
            6, 39, w=6, unit="percentunit", thresholds=[(0.05, "red")], runbook="03-stripe-payment-gateway-degraded"),
        timeseries("Outbox: edad del evento más antiguo", [(
            "max(outbox_oldest_pending_age_seconds)", "edad")], 12, 39, w=6, unit="s",
            thresholds=[(60, "orange"), (300, "red")], runbook="04-outbox-backlog-creciente"),
        timeseries("Caché catálogo: hit ratio", [(
            'sum(rate(cache_requests_total{cache="catalog", result="hit"}[5m]))'
            ' / sum(rate(cache_requests_total{cache="catalog", result=~"hit|miss"}[5m]))', "hit ratio")],
            18, 39, w=6, unit="percentunit", min_=0, max_=1, runbook="08-redis-cache-hit-ratio-low"),
    ]
    return dashboard("oms-overview", "OMS · Overview (SLOs, budget y golden signals)", p, ["slo", "overview"],
                     [JOB_VAR], "Primera pantalla del on-call. Cada panel enlaza a su runbook.")


# ─────────────────────────────── oms-db-overview ────────────────────────────
def db():
    p = [row("Pool de conexiones de la app (runbook 01)", 0)]
    p += [
        timeseries("Pool usage por instancia", [(
            'max by (job, instance) (db_pool_in_use{job=~"$job", instance=~"$instance"} / db_pool_size{job=~"$job", instance=~"$instance"})',
            "{{job}}/{{instance}}")], 0, 1, w=12, unit="percentunit", min_=0, max_=1,
            thresholds=[(0.7, "yellow"), (0.75, "orange"), (0.9, "red")], runbook="01-db-pool-saturated",
            description="Verificación del runbook 01: < 70 % durante 10 min."),
        timeseries("Peticiones esperando conexión", [(
            'sum by (job) (db_pool_pending_acquires{job=~"$job"})', "{{job}}")], 12, 1, w=6,
            thresholds=[(1, "red")], runbook="01-db-pool-saturated"),
        timeseries("Tiempo de acquire p99", [(
            'histogram_quantile(0.99, sum by (le, job) (rate(db_pool_acquire_duration_seconds_bucket{job=~"$job"}[5m])))',
            "{{job}}")], 18, 1, w=6, unit="s", thresholds=[(2, "red")]),
    ]
    p.append(row("Postgres (oms-pg-prod)", 9))
    p += [
        timeseries("Sesiones por estado", [('sum by (state) (pg_stat_activity_count{datname="oms"})', "{{state}}")],
                   0, 10, w=8),
        timeseries("Conexiones totales vs max_connections", [
            ('sum(pg_stat_activity_count{datname="oms"})', "en uso"),
            ("max(pg_settings_max_connections)", "max_connections", "B"),
        ], 8, 10, w=8, thresholds=[(380, "red")], runbook="01-db-pool-saturated"),
        timeseries("Deadlocks/s", [('rate(pg_stat_database_deadlocks{datname="oms"}[5m])', "deadlocks")],
                   16, 10, w=8, thresholds=[(0.1, "red")]),
        timeseries("CPU Cloud SQL", [('max(cloudsql_cpu_utilization{database_id="oms-production:oms-pg-prod"})', "cpu")],
                   0, 18, w=8, unit="percentunit", thresholds=[(0.7, "orange"), (0.85, "red")],
                   runbook="02-api-p95-high"),
        timeseries("Disco usado / cuota", [(
            "max(cloudsql_disk_bytes_used / cloudsql_disk_quota_bytes)", "uso")], 8, 18, w=8, unit="percentunit",
            thresholds=[(0.8, "orange"), (0.9, "red")], runbook="06-cloud-sql-disk-full"),
        timeseries("Lag de réplicas", [('max by (replica) (cloudsql_replication_lag_seconds)', "{{replica}}")],
                   16, 18, w=8, unit="s", thresholds=[(5, "orange"), (300, "red")], runbook="09-postgres-unavailable"),
        timeseries("Disponibilidad (pg_up / cloudsql_up)", [
            ("min(pg_up)", "pg_up"), ("min(cloudsql_up)", "cloudsql_up", "B")], 0, 26, w=12, min_=0, max_=1,
            runbook="09-postgres-unavailable"),
        timeseries("Cron kill-idle: horas desde el último éxito", [(
            '(time() - max(ops_job_last_success_timestamp_seconds{job="kill-idle-connections"})) / 3600', "horas")],
            12, 26, w=12, unit="h", thresholds=[(3, "red")], runbook="01-db-pool-saturated"),
    ]
    return dashboard("oms-db-overview", "OMS · Base de datos (Cloud SQL + pool)", p, ["db", "cloudsql"],
                     [JOB_VAR, INSTANCE_VAR])


# ─────────────────────────────── oms-payments ───────────────────────────────
def payments():
    p = [row("Experiencia de pago (SLO Payments)", 0)]
    p += [
        stat("SLI Payments 28 d", "slo:payments_authorization:ratio_28d", 0, 1, thresholds=[(0.99, "green")],
             runbook="03-stripe-payment-gateway-degraded"),
        timeseries("Autorizaciones por resultado", [(
            "sum by (outcome) (rate(payment_authorizations_total[5m]))", "{{outcome}}")], 6, 1, w=18, h=5,
            unit="reqps"),
    ]
    p.append(row("Stripe (dependencia externa)", 6))
    p += [
        timeseries("Peticiones a Stripe por resultado", [(
            'sum by (outcome) (rate(payment_provider_requests_total{provider="stripe"}[5m]))', "{{outcome}}")],
            0, 7, w=8, unit="reqps", runbook="03-stripe-payment-gateway-degraded"),
        timeseries("Latencia Stripe p95/p99", [
            ('histogram_quantile(0.95, sum by (le) (rate(payment_provider_request_duration_seconds_bucket{provider="stripe"}[5m])))', "p95"),
            ('histogram_quantile(0.99, sum by (le) (rate(payment_provider_request_duration_seconds_bucket{provider="stripe"}[5m])))', "p99", "B"),
        ], 8, 7, w=8, unit="s", thresholds=[(2.5, "red")], runbook="03-stripe-payment-gateway-degraded"),
        timeseries("Circuit breaker y cola de reintentos", [
            ('max(circuit_breaker_state{breaker="stripe"})', "breaker (0 cerrado · 1 semi · 2 abierto)"),
            ("sum(payment_retry_queue_depth)", "cola PENDING_PAYMENT", "B"),
        ], 16, 7, w=8, runbook="03-stripe-payment-gateway-degraded"),
    ]
    return dashboard("oms-payments", "OMS · Payments y Stripe", p, ["payments", "stripe"])


# ─────────────────────────────── oms-cache ──────────────────────────────────
def cache():
    p = [row("Caché del catálogo (Memorystore oms-redis-prod)", 0)]
    p += [
        timeseries("Hit ratio catálogo", [(
            'sum(rate(cache_requests_total{cache="catalog", result="hit"}[5m]))'
            ' / sum(rate(cache_requests_total{cache="catalog", result=~"hit|miss"}[5m]))', "hit ratio")],
            0, 1, w=8, unit="percentunit", min_=0, max_=1, runbook="08-redis-cache-hit-ratio-low"),
        timeseries("Peticiones a caché por resultado", [(
            'sum by (result) (rate(cache_requests_total{cache="catalog"}[5m]))', "{{result}}")], 8, 1, w=8,
            unit="reqps", runbook="05-redis-cache-down"),
        timeseries("Redis up", [("min(redis_up)", "redis_up")], 16, 1, w=8, min_=0, max_=1,
                   runbook="05-redis-cache-down"),
        timeseries("Memoria usada / máxima", [("max(redis_memory_used_bytes / redis_memory_max_bytes)", "memoria")],
                   0, 9, w=8, unit="percentunit", thresholds=[(0.9, "red")], runbook="08-redis-cache-hit-ratio-low"),
        timeseries("Evictions/s", [("sum(rate(redis_evicted_keys_total[5m]))", "evictions")], 8, 9, w=8,
                   runbook="08-redis-cache-hit-ratio-low"),
        timeseries("Respuestas servidas en stale", [(
            'sum(rate(cache_stale_served_total{cache="catalog"}[5m]))', "stale/s")], 16, 9, w=8,
            runbook="11-catalog-unavailable"),
        timeseries("Latencia catálogo p95 (NFR-PERF-001 < 150 ms)", [(
            'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="oms-catalog", endpoint=~"/api/products.*"}[5m])))',
            "p95")], 0, 17, w=24, unit="s", thresholds=[(0.15, "orange"), (0.25, "red")], runbook="02-api-p95-high"),
    ]
    return dashboard("oms-cache", "OMS · Caché del catálogo (Redis)", p, ["cache", "redis", "catalog"])


# ─────────────────────────────── oms-outbox ─────────────────────────────────
def outbox():
    p = [row("Outbox y relay (FR-014: confirmación < 5 min)", 0)]
    p += [
        timeseries("Eventos pendientes", [("sum(outbox_pending_events)", "pendientes")], 0, 1, w=8,
                   runbook="04-outbox-backlog-creciente"),
        timeseries("Edad del evento más antiguo", [("max(outbox_oldest_pending_age_seconds)", "edad")], 8, 1, w=8,
                   unit="s", thresholds=[(60, "orange"), (300, "red")], runbook="04-outbox-backlog-creciente"),
        timeseries("Segundos desde el último éxito del relay", [(
            "time() - max(outbox_relay_last_success_timestamp_seconds)", "segundos")], 16, 1, w=8, unit="s",
            thresholds=[(120, "red")], runbook="04-outbox-backlog-creciente"),
        timeseries("Publicados vs fallos por handler", [
            ("sum(rate(outbox_events_published_total[5m]))", "publicados/s"),
            ("sum by (handler) (rate(outbox_publish_failures_total[5m]))", "fallos {{handler}}", "B"),
        ], 0, 9, w=12, runbook="04-outbox-backlog-creciente"),
        timeseries("Lag PaymentCaptured → OrderConfirmed p99", [(
            "histogram_quantile(0.99, sum by (le) (rate(order_confirmation_lag_seconds_bucket[5m])))", "p99")],
            12, 9, w=12, unit="s", thresholds=[(60, "orange"), (300, "red")], runbook="04-outbox-backlog-creciente"),
    ]
    return dashboard("oms-outbox", "OMS · Outbox y eventos", p, ["outbox", "events"])


# ─────────────────────────────── oms-deploys ────────────────────────────────
def deploys():
    svc = query_var("service", "Servicio", 'label_values(http_requests_total{job=~"oms-.*"}, job)', include_all=False)
    p = [row("Canary: revisión nueva vs estable (runbook 07)", 0)]
    p += [
        timeseries("Tráfico por revisión", [(
            'sum by (revision) (rate(http_requests_total{job="$service"}[5m]))', "{{revision}}")], 0, 1, w=8,
            unit="reqps", runbook="07-canary-rollout-failed"),
        timeseries("Errores 5xx por revisión", [(
            'sum by (revision) (rate(http_requests_total{job="$service", code=~"5.."}[5m]))'
            ' / sum by (revision) (rate(http_requests_total{job="$service"}[5m]))', "{{revision}}")],
            8, 1, w=8, unit="percentunit", thresholds=[(0.01, "red")], runbook="07-canary-rollout-failed"),
        timeseries("p95 por revisión", [(
            'histogram_quantile(0.95, sum by (le, revision) (rate(http_request_duration_seconds_bucket{job="$service"}[5m])))',
            "{{revision}}")], 16, 1, w=8, unit="s", runbook="07-canary-rollout-failed"),
        timeseries("% de tráfico por revisión (etapas 10 → 50 → 100)", [(
            'max by (revision, tag) (run_revision_traffic_percent{job="$service"})', "{{tag}} {{revision}}")],
            0, 9, w=24, h=6, unit="percent", runbook="07-canary-rollout-failed"),
    ]
    return dashboard("oms-deploys", "OMS · Despliegues y canary", p, ["deploys", "canary"], [svc])


if __name__ == "__main__":
    for builder in (overview, db, payments, cache, outbox, deploys):
        _next_id = 0
        d = builder()
        path = OUT / f"{d['uid']}.json"
        path.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"escrito {path.name}: {len(d['panels'])} paneles")
