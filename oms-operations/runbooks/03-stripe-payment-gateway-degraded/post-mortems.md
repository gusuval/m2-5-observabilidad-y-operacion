# Post-mortems del runbook: stripe-payment-gateway-degraded

> *Escenarios simulados para el trabajo del Bloque 5, construidos sobre la arquitectura real del OMS.*
> Evolución del runbook a lo largo de 3 degradaciones de Stripe: V1 manual → V2 con comandos y breaker forzado por flag → V3 controlador automático.
> Formato blameless: se analizan sistemas y procesos, no personas. Los on-call se identifican por rol.

---

## Versión 1: mitigación manual (PM #1)

**Fecha del incidente:** martes 16 de junio de 2026, 19:42 → 21:10 CEST
**Duración del incidente:** 88 minutos (primer 503 a las 19:42 → verificación a las 21:10)
**Detectado por:** `DBConnectionPoolSaturated` a las 19:47 y quejas en el chat de soporte a las 19:50. No existía ninguna alerta sobre Stripe.
**On-call:** SRE de guardia (semana 25)
**Impacto:** ~235 intentos de `POST /api/orders` en la franja, **~140 con 5xx** (502/503). SLO 1 de Orders: budget de ~70 fallos → **consumido al 200 %** (⚫, freeze de features hasta el 30/06). SLO de Payments: ~110 autorizaciones malas → **18 % del budget** (616). Además, **7 clientes con doble retención** en su tarjeta: reintentaron tras un 503 que en realidad sí había autorizado (las retenciones caducaron a los 7 días sin cobro). 23 tickets de soporte.

### Lo que ocurrió
Stripe tuvo una degradación en su API (errores 5xx intermitentes y latencias de 5–20 s). Nuestro adapter usaba el timeout **por defecto del SDK, 80 s**. Como la llamada a Stripe va dentro de la transacción del pedido (arquitectura §5.1), cada pedido retenía una conexión de Postgres hasta 80 s. En 5 minutos el pool de `oms-orders` llegó al 100 % y empezaron los 503 también en pedidos que nunca llegaban a Stripe. El circuit breaker existía en el código (OPS-007), pero solo contaba excepciones HTTP 5xx: **los timeouts no contaban como fallo** y nunca se abrió.

### Cómo se mitigó (improvisado, sin runbook)
La alerta que paginó fue la del pool, así que el on-call siguió el runbook 01: no había zombis ni bloqueos, todo eran conexiones `active`. A los 25 min vio en Cloud Trace que el span `payments.authorize` duraba decenas de segundos. Consultó status.stripe.com (el incidente se publicó a las 20:05, 23 min después del inicio). Planteó "pasar a otro PSP" y descubrió que no existe ninguno integrado (ADR-006). Al final hizo un deploy fuera de ventana con `STRIPE_TIMEOUT_MS=3000`: los 503 por pool desaparecieron y quedaron 502 rápidos (solo fallaba el pago, no todo el checkout). Esperó a que Stripe se recuperase a las 21:02 y cerró a las 21:10.

### Aprendizajes y acciones
- **Un timeout de 80 s convierte un problema de un tercero en una caída nuestra.** → Timeout explícito de 2,5 s por llamada y timeouts contados como fallo en el breaker. *(Hecho, 18/06)*
- **No había forma de vender sin Stripe.** → Flag `payments.stripe-circuit-force-open` + estado `PENDING_PAYMENT` (HTTP 202) + cola `payments.retry_queue` con worker en `oms-outbox-relay`. *(Hecho, 28/07; 6 semanas de trabajo de Payments)*
- **Doble retención por reintento del cliente.** → `Idempotency-Key: auth-<order_id>` en todas las llamadas a Stripe y cabecera `Idempotency-Key` del SPA en `POST /api/orders` para que un doble clic no cree dos pedidos. *(Hecho, 07/07)*
- **Nos enteramos por el pool, no por Stripe.** → Alertas `StripeGatewayDegraded` y `StripeCircuitOpen`, probe `stripe-api` desde europe-west1 y suscripción de status.stripe.com a `#oms-alerts`. *(Hecho, 25/06)*
- **La latencia de Stripe no debe retener una conexión de BD.** → OMS-1402: sacar la llamada a Stripe de la transacción (reserva + pedido `PENDING` → commit → autorizar → actualizar). Cambia el flujo §5.1 de la arquitectura; se registró como enmienda de ADR-003. *(Hecho, 04/08)*
- **"Cambiar de PSP" no es una opción operativa.** Se documenta en el runbook el porqué (el `pm_…` es un token de Stripe) para que nadie vuelva a perder tiempo ahí a las 3 AM.

### Versión 1 del runbook tras este post-mortem

> **Runbook v1 (texto literal):**
> "Si dispara StripeGatewayDegraded: mirar status.stripe.com. Si Stripe está caído, activar el flag
> payments.stripe-circuit-force-open. Cuando Stripe se recupere, desactivarlo. Avisar a soporte."

(Dice **qué** hacer, pero no cómo saber si es Stripe, ni cuándo ni cómo cerrar. Por eso hay V2.)

---

## Versión 2: diagnóstico, comandos y cierre gradual (PM #2)

**Fecha del incidente:** jueves 13 de agosto de 2026, 11:20 → 12:14 CEST
**Duración del incidente:** 54 minutos (alerta 11:26 → breaker forzado 11:34 → cola drenada y verificado 12:14)
**Detectado por:** `StripeGatewayDegraded` (`signal=latency`, p95 = 4,1 s) a las 11:26.
**On-call:** SRE de guardia (semana 33), otra persona distinta a la de PM #1.
**Impacto:** **19 peticiones 5xx** en `POST /api/orders` (13 antes de forzar el breaker y 6 en una reapertura posterior) → **27 % del budget de Orders** (🟡). ~85 autorizaciones malas (65 `queued` + 20 fallidas) → **14 % del budget de Payments**. **7 pedidos cancelados** por agotar reintentos.

### Qué se vio durante esta segunda ocurrencia
El flag y la cola funcionaron: con el breaker forzado, los clientes vieron "pedido recibido" en lugar de un error. Pero:
1. **8 minutos para decidir si era Stripe.** status.stripe.com seguía en verde y el runbook V1 no decía cómo comprobarlo de otra forma. El on-call revisó Cloud NAT y DNS sin comandos preparados.
2. **El cierre fue de golpe.** A las 11:50 status.stripe.com pasó a *Monitoring* y el on-call hizo `flag_off`. Stripe aún tenía picos: el breaker automático se reabrió en 3 instancias a las 11:53 (6 errores 5xx más mientras tanto).
3. **El drenaje fue a toda velocidad.** El worker lanzó los 65 cobros de la cola en < 1 min contra un Stripe a medio recuperar. El 30 % falló y la política (3 intentos, backoff de 1 min) dejó 7 pedidos agotados y cancelados. 4 de esos clientes se quejaron.
4. **Soporte se enteró por los clientes** del email "confirmaremos tu pago", y no sabía qué responder.

### Qué cambió en el runbook (V1 → V2)
- **"¿Es Stripe o somos nosotros?" con tres puntos de vista** (probe externa, bastión por nuestro NAT, status page) y una tabla de decisión. Diagnóstico por tipo de error (5xx, timeout, 401, 429) con su comando de logs y el `stripe_request_id` para el ticket a Stripe.
- **Cierre gradual del breaker** con el porcentaje del flag (`flag_rollout` 50 → 20 → 0) y vuelta a 100 si un escalón supera el 5 % de errores. Paso explícito de **rearmar** el flag al 100 % tras apagarlo.
- **Drenaje con control de ritmo**: nuevo flag `payments.retry-queue-paused` (la cola no drena mientras el breaker está forzado) y `PAYMENT_RETRY_MAX_PER_MIN=20`. Política de reintentos: 8 intentos con backoff exponencial y caducidad a las 20 h, por debajo de las 24 h que Stripe guarda la idempotencia.
- **Acción de reintento rápido** como flag (`payments.stripe-fast-retry`) solo para errores intermitentes, con aviso de no usarla con latencia.
- **Plantillas de comunicación** para soporte, producto y status page. Verificación que no se fía de la alerta resuelta (con el breaker forzado no hay llamadas a Stripe).

### Acciones derivadas
- Alerta `PaymentRetryQueueStuck` y alerta warning `StripeGatewayErrorsElevated`. *(Hecho, 20/08)*
- Recording rules y alertas de burn rate del SLO de Payments, para saber cuánto budget se lleva Stripe. *(Hecho, 27/08)*
- Evaluar un controlador que fuerce y cierre el breaker sin esperar al humano: el on-call tardó 8 min en algo que se decide con dos métricas.

---

## Versión 3: controlador automático del breaker (PM #3)

**Fecha:** martes 6 de octubre de 2026, 15:12 → 16:31 CEST (degradación de Stripe 15:12 → 16:05). **Sin impacto relevante para el cliente.**

### Qué pasó (y qué no)
Stripe volvió a dar errores 5xx en ~20 % de las llamadas. Los breakers automáticos se abrieron en algunas instancias a las 15:13. A las 15:15, con `StripeGatewayDegraded` activa 3 min, el controlador `oms-ops-stripe-breaker` forzó el breaker al 100 %, pausó la cola y publicó en `#incident-response`. Cuando el on-call abrió el portátil (15:19) solo tuvo que confirmar el diagnóstico con el paso 2 (probe externa caída, bastión con 503: era Stripe) y enviar la plantilla a soporte. A las 16:15, tras 10 min de `probe_success{probe="stripe-api"}` = 1, el controlador cerró en rampa (50 → 20 → 0 %, 5 min por escalón) y reanudó el drenaje a 20/min. **4 errores 5xx** en total (antes de que se abriera el breaker), **0 pedidos cancelados por la cola**. ~130 autorizaciones malas: **21 % del budget de Payments**, que es el coste de depender de un único PSP.

### Qué cambió en el runbook (V2 → V3)
- **Las Acciones 1 y 4 están automatizadas** en el controlador; el runbook pasa a ser "confirma, comunica y vigila". El on-call conserva el control: cualquier `flag_rollout` manual pausa el controlador durante 1 h (lo detecta por el autor del cambio en Unleash).
- **Nueva alerta** `StripeBreakerControllerStale`: una automatización que falla en silencio nos devuelve al V1 sin avisar.
- `StripeGatewayDegraded` sigue paginando: el controlador mitiga, pero la comunicación con soporte y producto es humana.

### El runbook como automatización
- **Qué hace:** cada minuto lee de Prometheus las dos ramas de `StripeGatewayDegraded` y `probe_success{probe="stripe-api"}`. Con 3 min seguidos en rojo: `flag_rollout payments.stripe-circuit-force-open 100` + `flag_on` del breaker y de `payments.retry-queue-paused`. Con 10 min seguidos de probe en verde: rampa 50 → 20 → 0 %, vuelta a 100 si un escalón supera el 5 % de errores, rearme del flag y `flag_off payments.retry-queue-paused`. Cada cambio se publica en `#incident-response` y como anotación en Grafana.
- **Disparador:** Cloud Scheduler `oms-ops-stripe-breaker` (`* * * * *`, zona `Europe/Madrid`) → Cloud Run Job `oms-ops-stripe-breaker` (europe-west3, SA `oms-ops-automation@` con token de Unleash limitado a los flags `payments.*`).
- **Owner:** equipo Payments (lógica) + SRE (operación del job).
- **Salud:** `ops_job_last_success_timestamp_seconds{job="stripe-breaker-controller"}`, vigilada por `StripeBreakerControllerStale` (> 10 min).

### Métrica del runbook a lo largo de la evolución

| | V1 (manual) | V2 (comandos + flag) | V3 (controlador) |
|---|---|---|---|
| Detección | Alerta de pool (efecto secundario) | `StripeGatewayDegraded` | `StripeGatewayDegraded` + controlador |
| Tiempo hasta proteger al cliente | 70 min (y solo a medias) | 8 min tras la alerta | 3 min tras la alerta, sin humano |
| 5xx en `POST /api/orders` | ~140 | 19 | 4 |
| Budget de Orders consumido | 200 % (⚫ freeze) | 27 % (🟡) | 6 % |
| Budget de Payments consumido | 18 % | 14 % | 21 % (la incidencia de Stripe fue más larga) |
| Pedidos perdidos / cancelados | ~60 + 7 dobles retenciones | 7 cancelados por la cola | 0 |
| Toil del on-call | Alto (diagnóstico a ciegas, deploy fuera de ventana) | Medio (comandos, rampa a mano) | Bajo (confirmar y comunicar) |
| Siguiente mejora | Breaker forzable y cola | Automatizar fuerza y rampa | Re-evaluar ADR-006 si Stripe supera el 50 % del budget trimestral |

---

## Conclusión

Este runbook enseña la diferencia entre **proteger la venta** y **proteger el SLO**. En V1 un timeout por defecto de 80 s convirtió un problema de Stripe en una caída de todo el checkout, y gastamos el doble del budget de Orders. En V2 el circuit breaker se convirtió en un **flag**: el pedido se acepta como `PENDING_PAYMENT` y la idempotencia de Stripe hace seguro reintentar más tarde. Aprendimos a la fuerza que cerrar el breaker de golpe y drenar a toda velocidad deshace lo ganado. En V3 una máquina toma la decisión que se toma con dos métricas y el humano hace lo que la máquina no sabe hacer: hablar con soporte y con producto. Lo que ningún runbook arregla es la tercera columna de Payments: mientras haya un único PSP (ADR-006), cada incidencia de Stripe se come budget de Payments, por bien que reaccionemos. Esa conversación es de arquitectura y de negocio, no de operación, y el runbook deja escrito cuándo abrirla.
