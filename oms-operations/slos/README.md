# SLOs y política de error budget

> 2 SLOs (uno por módulo crítico) + política de error budget de una página.

## Ficheros

| Fichero | Qué contiene |
|---|---|
| [`slos-orders.md`](slos-orders.md) | SLO 1: disponibilidad de `POST /api/orders` (99,9 %) · SLO 2: latencia de consulta de pedidos (95 % < 250 ms, 99 % < 800 ms) |
| [`slos-payments.md`](slos-payments.md) | Autorización con respuesta definitiva en < 3 s (99,0 %). Los fallos de Stripe cuentan |
| [`error-budget-policy.md`](error-budget-policy.md) | Niveles 🟢🟡🔴⚫ con umbrales numéricos y consecuencias |
| [`slo-recording-rules.yaml`](slo-recording-rules.yaml) | Recording rules de los SLI a 28 d, budget restante y alertas de cambio de nivel de la política |

## Criterio del oficio

Un SLO bueno cumple las 4:
- Tiene un **SLI exacto** con numerador, denominador y ventana
- Mide lo que el **usuario** experimenta, no la salud de la infra
- Su **error budget** está calculado en minutos al mes (no es porcentaje abstracto)
- Tiene una **alerta de burn rate** que dispara antes del incumplimiento real
