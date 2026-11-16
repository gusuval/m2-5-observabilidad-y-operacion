# SLO del módulo Payments

> Payments tiene una particularidad: depende de Stripe (externo, ADR-006). Eso cambia cómo se define el SLO,
> porque parte de la disponibilidad **no está en nuestras manos**, pero la experiencia del cliente sí es responsabilidad nuestra.

---

## SLO · Disponibilidad de la autorización de pagos

**Enunciado:**
> **El 99,0 % de los intentos válidos de autorización de pago reciben una respuesta definitiva (aprobado, o rechazado por el banco emisor) en menos de 3 segundos, en una ventana rolling de 28 días.**

**Por qué importa al usuario:**
Cuando el cliente pulsa "Pagar", solo hay tres resultados que entiende: "pagado", "tu banco ha rechazado la tarjeta" o "no sabemos qué ha pasado". Los dos primeros son **buenos**, porque el sistema funcionó y el cliente sabe qué hacer. El tercero (timeout, error genérico, spinner de más de 3 s) es el que hace abandonar el checkout y, peor, genera la duda de si le han cobrado. Este SLO mide exactamente esa incertidumbre.

**Por qué 3 s:** por encima de ~3 s el cliente empieza a recargar o a pulsar "Pagar" otra vez (ver post-mortems del runbook 03). Las claves de idempotencia de Stripe evitan el doble cobro, pero la experiencia ya está rota. Incluye 3DS frictionless; los retos 3DS con interacción del usuario se excluyen (ver denominador).

**Por qué 99,0 % y no 99,9 % como Orders:** heredamos la disponibilidad de Stripe (históricamente ~99,99 % en su API, con degradaciones parciales de latencia varias veces al año) más la tasa de errores transitorios de red que no controlamos. Un 99,9 % nos dejaría en 🔴 con cada incidente de Stripe, incluso sin culpa nuestra. 99,0 % deja margen para un incidente de Stripe de ~1 h al mes **y** nos obliga a mitigar todo lo demás.

### SLI

| | Definición |
|---|---|
| **Numerador (good events)** | Intentos con `outcome` en `approved` o `declined` (rechazo legítimo del emisor: fondos insuficientes, tarjeta denegada, CVC incorrecto) **y** duración ≤ 3 s. |
| **Denominador (valid events)** | Todos los intentos de autorización **excepto**: (1) `invalid_request`, errores de validación nuestros antes de llamar a Stripe (formulario incompleto); (2) `requires_action`, el cliente tiene que completar un reto 3DS en su banco y el tiempo depende de él; (3) tráfico de la probe sintética (`synthetic="true"`). |
| **Malos (cuentan contra el budget)** | `failed` (error de API de Stripe, 5xx, 429, timeout, error de red, clave inválida) y `queued` (breaker abierto: el pedido queda `PENDING_PAYMENT` y el cliente recibe "confirmaremos tu pago por email"), **y** cualquier aprobado o rechazado que tarde más de 3 s. |
| **Ventana** | 28 días rolling. |

```promql
# SLI de Payments: good / valid (SLO ≥ 0.99)
sum(increase(payment_authorization_duration_seconds_bucket{outcome=~"approved|declined", synthetic!="true", le="3"}[28d]))
/
sum(increase(payment_authorization_duration_seconds_count{outcome!~"invalid_request|requires_action", synthetic!="true"}[28d]))
```

> El histograma `payment_authorization_duration_seconds` lleva la etiqueta `outcome` y tiene un bucket exacto en `le="3"`. Para `queued` se registra la duración hasta que se encola (es "malo" igualmente, porque el filtro del numerador lo excluye).

### Error budget calculado

| Cálculo | Valor |
|---|---|
| Intentos de autorización válidos / día | ~2.200 (2.000 pedidos + reintentos tras un rechazo) |
| Intentos válidos en 28 d | 2.200 × 28 = **61.600** |
| Error budget (1 %) en intentos | **616 intentos** malos en 28 días |
| Equivalente en tiempo (fallo total) | 1 % × 40.320 min = **403 min** (~6,7 h) en 28 días |
| Caída completa de Stripe de 1 h en horario punta (~180 intentos/h) | ~180 intentos = **29 % del budget** → sigue en 🟡 |
| Degradación de latencia de Stripe (p95 4 s) durante 3 h en horario normal | ~40 % de ~270 intentos lentos ≈ 108 → **18 % del budget** |

### Alertas de burn rate

Definidas en [`runbooks/03-stripe-payment-gateway-degraded/alert.yaml`](../runbooks/03-stripe-payment-gateway-degraded/alert.yaml).

| Alerta | Ventana larga / corta | Burn rate | Umbral de malos | Mínimo de tráfico | Destino |
|---|---|---|---|---|---|
| `PaymentsAuthBurnRateFast` | 1 h / 5 min | 14,4× | > 14,4 % | > 20 intentos en 1 h | PagerDuty |
| `PaymentsAuthBurnRateSlow` | 6 h / 30 min | 6× | > 6 % | > 100 intentos en 6 h | PagerDuty |

---

## Tratamiento de fallos externos (Stripe)

**Decisión: los fallos de Stripe CUENTAN contra nuestro SLO (opción A).** Además, medimos aparte un SLI de salud de Stripe, solo para tener visibilidad.

```promql
# SLI de visibilidad (no tiene objetivo ni consecuencias en la política): éxito técnico de la API de Stripe
sum(rate(payment_provider_requests_total{provider="stripe", outcome=~"success|card_error"}[1h]))
/
sum(rate(payment_provider_requests_total{provider="stripe"}[1h]))
```

**Por qué cuentan:**
1. **El cliente no distingue** "falló Stripe" de "falló AcmeOMS". El abandono del checkout es el mismo y el ingreso perdido también. Si el SLO mide la experiencia del usuario (regla 4 del enunciado), no puede excluir la causa más probable de mala experiencia en el pago.
2. **Si no contaran, no tendríamos incentivo para mitigar.** Contar los fallos de Stripe es lo que justifica el circuit breaker, los timeouts ajustados, los reintentos con idempotency key y la cola de reintentos de `PENDING_PAYMENT` (runbook 03). Con la opción B, todo eso sería "coste sin beneficio en el SLO".
3. **Nos obliga a una conversación honesta con negocio:** cuando un incidente de Stripe consume el 30 % del budget, la pregunta "¿necesitamos un PSP secundario?" se responde con números, no con opiniones. Hoy la respuesta es no (ADR-006: el coste de PCI y de integración no compensa un 0,3 % de intentos al año).

**Por qué medimos aparte el SLI de Stripe:**
En el post-mortem hay que poder decir "de los 616 intentos de budget, Stripe consumió X y nosotros Y". Si X domina de forma sistemática, la política de error budget **no congela** nuestras features (ver la excepción en [`error-budget-policy.md`](error-budget-policy.md)), pero sí abre la conversación sobre el PSP secundario.

**Por qué `queued` (breaker abierto) cuenta como malo:**
Con el breaker abierto, el cliente **sí** tiene su pedido (Orders responde 202 y no gasta budget de Orders), pero **no** sabe si ha pagado. Es una degradación elegante (OPS-007), no un éxito. Contarlo como bueno haría que forzar el breaker "arreglara" el SLO de Payments sin mejorar nada para el cliente.

---

## Notas

- **Coordinación con el SLO de Orders:** un incidente de Stripe con el breaker abierto gasta budget de **Payments** pero **no** de Orders (202 ≠ 5xx). Sin breaker, gastaría ambos. El breaker es justo lo que protege el SLO más estricto (99,9 %) a costa del más laxo (99 %), y es una decisión de diseño deliberada.
- **Revisión trimestral con producto y finanzas:** se revisa el desglose del consumo (nuestro frente a Stripe) y la tasa de conversión de los `PENDING_PAYMENT` (¿cuántos acaban cobrándose?). Si un `PENDING_PAYMENT` acaba cobrado en > 95 % de los casos, se puede discutir sacar `queued` de los malos y moverlo a una categoría "degradado".
- **Qué cuenta como intento:** solo el intento **síncrono** del cliente. Los reintentos de la cola `payments.retry_queue` no vuelven a sumar en el SLI: el cliente ya vivió ese intento como "malo" (`queued`) y contarlo otra vez duplicaría el castigo. Su éxito se mide aparte, con la conversión de `PENDING_PAYMENT` en cobrado.
- **Caducidad de la cola:** los reintentos caducan a las **20 h**, por debajo de las 24 h durante las que Stripe guarda las idempotency keys. Así ningún reintento puede llegar con una clave ya olvidada por Stripe (riesgo de doble cobro). Pasadas las 20 h, el pedido se cancela y se avisa al cliente por email.
- **Criterio para reabrir ADR-006 (PSP secundario):** si en un trimestre Stripe consume **> 50 % del budget** de Payments, medido con el SLI de visibilidad, se reabre la decisión con números de ingreso perdido.
- **Lo que este SLO NO mide:** la captura del cobro (asíncrona, tras confirmar el pedido) ni los reembolsos. Ambos tienen sus propias métricas en [`instrumentation.md`](../observability/instrumentation.md), pero no tienen SLO: el cliente no espera delante de la pantalla por ellos.
