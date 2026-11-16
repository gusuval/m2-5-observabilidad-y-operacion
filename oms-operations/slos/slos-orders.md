# SLOs del módulo Orders

> Dos SLOs sobre lo que el cliente hace con sus pedidos: **crearlos** (si falla, perdemos la venta) y **consultarlos** ("¿dónde está mi pedido?", lo que más tráfico de soporte genera cuando va lento).
> Son SLOs **internos** del equipo, no un SLA con el cliente.

**Datos de tráfico usados en los cálculos** (anexo de arquitectura, operación normal):

| Dato | Valor | Origen |
|---|---|---|
| Pedidos creados / día | ~2.000 (pico Black Friday ~10.000) | Alcance del OMS |
| `POST /api/orders` válidos / día | ~2.500 (pedidos + reintentos + pedidos que fallan en validación de negocio) | Logs del Bloque 4 |
| `GET /api/orders*` / día | ~850.000 (≈ 10 req/s de media) | Logs del Bloque 4 |
| Ventana | 28 días rolling = 40.320 min | 4 semanas exactas: todas las ventanas tienen los mismos lunes de campaña |

---

## SLO 1 · Disponibilidad de `POST /api/orders`

**Enunciado:**
> **El 99,9 % de las peticiones válidas a `POST /api/orders` se responden sin error del servidor (código distinto de 5xx), en una ventana rolling de 28 días.**

**Por qué importa al usuario:**
`POST /api/orders` es el clic en "Confirmar compra". Un 5xx ahí significa que el cliente ya decidió comprar, rellenó la dirección e introdujo la tarjeta, y el sistema le falla en el último paso. Según los post-mortems, ~1 de cada 4 clientes que ve ese error no reintenta: es **ingreso perdido directamente** y, además, la duda de "¿me habrán cobrado?" acaba en una llamada a soporte. No medimos CPU ni conexiones de BD, sino si el cliente consiguió comprar.

**Por qué 99,9 % y no 99,5 %:** NFR-AVAIL-001 fija el checkout en 99,9 % mensual con multi-AZ y failover automático. Un SLO más laxo que la spec no protegería el compromiso. Ver la decisión 1 en el [README](../README.md#decisiones).

### SLI

| | Definición |
|---|---|
| **Numerador (good events)** | Peticiones `POST /api/orders` con código **distinto de 5xx**. Incluye 201 (creado) y **202 (aceptado como `PENDING_PAYMENT` con el breaker de Stripe abierto)**: el cliente tiene su pedido registrado y un mensaje claro. |
| **Denominador (valid events)** | Todas las `POST /api/orders` **excepto 4xx**: datos inválidos, sin stock (409), token caducado (401) o rate limit (429) son respuestas correctas del sistema a una petición incorrecta. También se excluye el tráfico de la probe sintética (`user_agent="oms-synthetic"`, filtrado en la app con la etiqueta `synthetic="true"`). |
| **Ventana** | 28 días rolling. |
| **Dónde se mide** | En la app (`http_requests_total` del SDK de OpenTelemetry). Los 5xx que genera el Load Balancer cuando la app no responde se suman desde `loadbalancer_backend_5xx_total`: si la app está caída no puede contarse a sí misma. |

```promql
# SLI de disponibilidad: good / valid (resultado entre 0 y 1; SLO ≥ 0.999)
1 - (
  (
    sum(increase(http_requests_total{job="oms-orders", endpoint="/api/orders", method="POST", synthetic!="true", code=~"5.."}[28d]))
    + sum(increase(loadbalancer_backend_5xx_total{backend_service="oms-orders", path="/api/orders", method="POST"}[28d]))
  )
  /
  (
    sum(increase(http_requests_total{job="oms-orders", endpoint="/api/orders", method="POST", synthetic!="true", code!~"4.."}[28d]))
    + sum(increase(loadbalancer_backend_5xx_total{backend_service="oms-orders", path="/api/orders", method="POST"}[28d]))
  )
)
```

### Error budget calculado

| Cálculo | Valor |
|---|---|
| Peticiones válidas en 28 d | 2.500 × 28 = **70.000** |
| Error budget (0,1 %) en peticiones | **70 peticiones fallidas** en 28 días |
| Equivalente en tiempo (caída total) | 0,1 % × 40.320 min = **40,3 min** por ventana (≈ 43 min/mes, lo que dice NFR-AVAIL-001) |
| Coste de 1 error | 1 petición = **1,4 %** del budget |
| Incidente tipo del runbook 01 (V1) | ~180 fallos → **257 % del budget** → ⚫ |
| Failover HA de Cloud SQL (~60 s) | ~2–5 fallos con reintento en la app → 3–7 % del budget. Lo absorbe, que es para lo que existe el budget. |

> **Consecuencia operativa del tráfico bajo:** con ~100 peticiones/hora, 1 único error en una hora es un 1 % de errores, 10 veces el objetivo. Por eso las alertas de burn rate exigen un **mínimo de peticiones en la ventana** (> 20 en 1 h, > 100 en 6 h). Sin ese mínimo, un solo 5xx a las 4 AM despertaría al on-call.

### Alertas de burn rate (multiwindow, multi-burn-rate · Vídeo 2)

Definidas en [`runbooks/01-db-pool-saturated/alert.yaml`](../runbooks/01-db-pool-saturated/alert.yaml), grupo `orders-slo-burn-rate`. Apuntan al runbook de triage [`10-checkout-unavailable`](../runbooks/10-checkout-unavailable/runbook.md).

| Alerta | Ventana larga / corta | Burn rate | Umbral de error | Budget gastado al disparar | Destino |
|---|---|---|---|---|---|
| `OrdersAvailabilityBurnRateFast` | 1 h / 5 min | 14,4× | > 1,44 % | 2 % en 1 h | PagerDuty (critical) |
| `OrdersAvailabilityBurnRateSlow` | 6 h / 30 min | 6× | > 0,6 % | 5 % en 6 h | PagerDuty (critical) |

```promql
# Rápida: ratio de 5xx en 1 h y en 5 min por encima de 14,4 × 0,001, con mínimo de tráfico
slo:orders_post_errors:ratio_rate1h > (14.4 * 0.001)
and slo:orders_post_errors:ratio_rate5m > (14.4 * 0.001)
and sum(increase(http_requests_total{job="oms-orders", endpoint="/api/orders", method="POST", code!~"4.."}[1h])) > 20
```

La ventana corta (5 min / 30 min) sirve para que la alerta **se resuelva sola** en cuanto el problema para, en lugar de seguir disparando 1 h por inercia de la ventana larga.

---

## SLO 2 · Latencia de consulta de pedidos (`GET /api/orders`, `GET /api/orders/{id}`)

**Enunciado:**
> **En una ventana rolling de 28 días, el 95 % de las consultas válidas de pedidos responden en menos de 250 ms y el 99 % en menos de 800 ms.**

**Por qué importa al usuario:**
"Mis pedidos" es la pantalla a la que vuelve el cliente después de comprar, y la que abre el agente de soporte con el cliente al teléfono. Si tarda, el cliente entiende que "algo va mal con mi pedido" y llama. Los dos umbrales salen del enunciado (**NFR-PERF-002**: p95 < 250 ms; **NFR-PERF-003**: p99 < 800 ms). El de 800 ms protege la cola: el usuario que espera casi un segundo es el que recarga la página y duplica la carga.

**Por qué este endpoint y no `POST /api/orders`:** la creación de pedido incluye la llamada síncrona a Stripe (300–600 ms de mediana, fuera de nuestro control). Un SLO de 250 ms sobre `POST` se incumpliría por diseño. Esa latencia queda cubierta por el SLO de Payments (< 3 s) y por la alerta de p99 del runbook 02.

### SLI

| | Definición |
|---|---|
| **Numerador (good events)** | Consultas con duración ≤ 250 ms (objetivo 95 %), y por separado ≤ 800 ms (objetivo 99 %). Se miden con los buckets `le="0.25"` y `le="0.8"` del histograma, que **existen exactamente** para que el SLI no sea una interpolación. |
| **Denominador (valid events)** | `GET` a `/api/orders` y `/api/orders/{id}` **sin 4xx ni 5xx**: los 4xx son errores del cliente y los 5xx ya los paga el SLO de disponibilidad. Así no penalizamos dos veces el mismo error. Excluye la probe sintética. |
| **Ventana** | 28 días rolling. |

```promql
# SLI 2a: proporción de consultas < 250 ms (SLO ≥ 0.95)
sum(increase(http_request_duration_seconds_bucket{job="oms-orders", endpoint=~"/api/orders|/api/orders/\\{id\\}", method="GET", code!~"[45]..", synthetic!="true", le="0.25"}[28d]))
/
sum(increase(http_request_duration_seconds_count{job="oms-orders", endpoint=~"/api/orders|/api/orders/\\{id\\}", method="GET", code!~"[45]..", synthetic!="true"}[28d]))

# SLI 2b: proporción de consultas < 800 ms (SLO ≥ 0.99): misma query con le="0.8"
```

### Error budget calculado

| Cálculo | 2a (< 250 ms, 95 %) | 2b (< 800 ms, 99 %) |
|---|---|---|
| Consultas válidas en 28 d | 850.000 × 28 ≈ **23,8 M** | 23,8 M |
| Budget en peticiones lentas | 5 % = **1,19 M** | 1 % = **238.000** |
| Equivalente en tiempo (todo lento) | 5 % × 40.320 = **2.016 min** (~33,6 h) | 1 % × 40.320 = **403 min** (~6,7 h) |

### Alertas de burn rate

Definidas en [`runbooks/02-api-p95-high/alert.yaml`](../runbooks/02-api-p95-high/alert.yaml) (`OrdersLatencyBurnRate`, dos reglas). Cada regla evalúa los **dos** objetivos (2a y 2b) y dispara con el primero que cruce:

| Severidad | Ventana larga / corta | Burn rate | Umbral 2a (> 250 ms) | Umbral 2b (> 800 ms) | Mínimo de tráfico | Destino |
|---|---|---|---|---|---|---|
| critical | 1 h / 5 min | 14,4× | > 72 % de consultas lentas | **> 14,4 %** | > 300 consultas/h | PagerDuty |
| warning | 6 h / 30 min | 6× | **> 30 %** | > 6 % | > 1.500 consultas/6 h | Slack + ticket |

> Con un budget del 5 %, una burn rate de 14,4× sobre 2a exige un 72 % de consultas lentas, lo que en la práctica es una caída. Por eso la **página rápida la dispara casi siempre el objetivo 2b** (cola > 800 ms, budget del 1 %) y el objetivo 2a se vigila sobre todo con la regla lenta. Es intencionado: el cliente sufre la cola, no la mediana.

La alerta por umbral `APILatencyP95High` (p95 > 250 ms durante 10 min) es la **operativa**: dice dónde mirar. La de burn rate dice **cuánto nos está costando**.

---

## Notas

- **Cuando producto pida 99,99 %** en `POST /api/orders`: el budget bajaría a **7 peticiones o 4 min** en 28 días. Un solo failover de Cloud SQL (≈ 60 s) lo agotaría. Llegar ahí exige multi-región activo-activo (~1.500 €/mes según OPS-003, el doble del presupuesto total). La conversación correcta es "¿cuánto ingreso perdemos en 36 minutos al mes y cuánto cuesta evitarlo?".
- **Revisión:** trimestral, junto con la [política de error budget](error-budget-policy.md). Si el tráfico crece un 30 % (NFR-SCAL-002), los porcentajes no cambian, pero el número de peticiones del budget sí: hay que recalcular la tabla.
- **Excluido explícitamente del SLO:** mantenimientos programados fuera de la franja comercial (NFR-AVAIL-003) **solo si** se anunciaron con 72 h de antelación en el status page. Si no se anunciaron, cuentan.
- **Lo que el SLO 1 no ve:** se mide en la app (más los 5xx del Load Balancer) y excluye los 4xx. Un bloqueo erróneo de Cloud Armor (403/429 a clientes legítimos) **no consume budget** del SLO 1. Por eso NFR-AVAIL-001 se verifica también con la probe externa `checkout-e2e` y la alerta `CheckoutUnavailable` (runbook 10): el SLO mide la app y la probe mide el recorrido completo del cliente.
