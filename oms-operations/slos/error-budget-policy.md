# Política de error budget: equipo OMS

> Una página. Niveles numéricos. Consecuencias explícitas. Sin ambigüedad.
> **Aprobada por:** Engineering Manager OMS + Product Owner checkout · **Vigente desde:** 2026-03-23 · **Próxima revisión:** 2026-12-15

---

## SLOs sobre los que aplica

| SLO | Objetivo | Error budget (28 d rolling) | Dónde se calcula |
|---|---|---|---|
| **Orders 1:** disponibilidad de `POST /api/orders` (**gobierna la política**) | 99,9 % no-5xx | **70 peticiones ≈ 40 min** | [`slos-orders.md`](slos-orders.md) |
| Orders 2: latencia de `GET /api/orders*` | 95 % < 250 ms · 99 % < 800 ms | 1,19 M / 238.000 consultas lentas (≈ 2.016 / 403 min) | [`slos-orders.md`](slos-orders.md) |
| Payments: autorización con respuesta definitiva en < 3 s | 99,0 % | **616 intentos ≈ 403 min** | [`slos-payments.md`](slos-payments.md) |

**Regla de agregación:** el nivel del equipo es **el peor** de los tres SLOs. El budget restante se publica en el panel "Error budget" de [`oms-overview`](https://grafana.example.com/d/oms-overview) y lo lee el bot de `#oms-alerts` cada lunes a las 09:30.

```promql
# Budget restante del SLO 1 de Orders (1 = intacto, 0 = agotado, < 0 = en deuda)
1 - ( (1 - slo:orders_availability:ratio_28d) / 0.001 )
```

---

## Niveles y consecuencias

| Nivel | Budget restante | Despliegues | Sprint | Quién decide / comunica |
|---|---|---|---|---|
| 🟢 **Verde** | **> 50 %** | Sin restricción (siempre dentro de la ventana OPS-002: 09:00–18:00, no viernes) | Features con normalidad | — |
| 🟡 **Amarillo** | **10 % – 50 %**, o **un único incidente que consume > 20 %** | Permitidos. Toda PR de feature lleva una nota de "impacto en disponibilidad" firmada por el revisor. Canary obligatorio (runbook 07), sin saltar etapas. | ≥ **20 %** de la capacidad a fiabilidad (acciones de post-mortems abiertas) | El on-call lo anuncia en `#oms-releases`. Se revisa en el standup de cada lunes. |
| 🔴 **Rojo** | **< 10 %** | **Freeze de features.** Solo se mergea lo que reduce riesgo: fixes, timeouts, retries, alertas, runbooks, flags. Máximo **1 despliegue al día**. | ≥ **50 %** de la capacidad a fiabilidad | El EM informa a producto en el sync semanal con cifras de budget y causas |
| ⚫ **Agotado** | **≤ 0 %** | Freeze total salvo `safety-fix`, hasta que el budget vuelva a > 10 % (la ventana rolling lo recupera sola al salir los incidentes de los 28 d) | 100 % a fiabilidad + **post-mortem obligatorio** del periodo en ≤ 5 días laborables | Decisión escalada a la VP de Ingeniería (ver abajo) |

**Por qué la regla de "un incidente > 20 % → amarillo":** con un budget de solo 70 peticiones, un incidente de 15 minutos lo puede vaciar. Esperar a cruzar el 50 % significaría reaccionar tarde, cuando el siguiente incidente ya nos deja en rojo. Un único evento que se lleva más de un 20 % es señal de que **el sistema tiene un modo de fallo sin mitigar**, y eso merece atención aunque el total aún esté holgado.

**Decisión en ⚫ (VP de Ingeniería, en ≤ 48 h):**
1. **Relajar el SLO** (por ejemplo, 99,9 % → 99,5 %) solo si producto acepta por escrito el coste en negocio y se actualiza NFR-AVAIL-001 en la spec.
2. **Reasignar capacidad** de otro equipo durante 1–2 sprints para acelerar la estabilización.
3. **Mantener el SLO** y aceptar que el equipo no entrega features hasta salir de ⚫.

---

## Excepciones

- **`safety-fix`:** las PRs con esta etiqueta se mergean en cualquier nivel. Requieren aprobación del on-call de la semana y que la PR enlace el incidente o el post-mortem que la justifica.
- **Causa externa demostrada (Stripe, incidente de GCP con status oficial):** el consumo **cuenta** en el SLO, pero **no activa el freeze** si se cumplen las dos condiciones: (1) los runbooks de mitigación (03, 09) se ejecutaron en < 15 min y (2) la parte atribuible a nosotros, medida con el SLI de visibilidad de Stripe, es < 50 % del consumo. Sí obliga a revisar en el siguiente trimestral si hace falta más redundancia.
- **Black Friday y campañas grandes (> 3× tráfico):** freeze preventivo de features **desde 5 días antes**, sea cual sea el nivel. El budget de esos días es para el tráfico, no para los despliegues.

---

## On-call y contacto

- **Rotación:** los **2 SREs** del equipo (OPS-001), semanas alternas de lunes 09:00 a lunes 09:00. Fuera del horario laboral solo paginan las alertas `severity: critical` (OPS-007). Las `warning` esperan a las 09:00 del siguiente día laborable.
- **Contacto:** PagerDuty, servicio `OMS Production` → teléfono y push. Escalado automático al **EM de guardia** si nadie hace *acknowledge* en 10 min, y a la **VP de Ingeniería** a los 30 min.
- **Canales:** `#incident-response` (incidente activo), `#oms-alerts` (warnings y estado del budget), `#oms-releases` (cambios de nivel y freezes).
- **Compensación:** cada noche paginada da medio día libre. Si el on-call recibe más de **2 páginas por noche** de media en una semana, la siguiente semana pasa automáticamente a 🟡 (el toil también es budget).

---

## Revisión

Cada **trimestre** con producto. Si los supuestos del usuario cambian (más tráfico, más exigencia, nuevos países), **se ajusta el SLO antes que la política**. Los umbrales del 50 % y el 10 % se revisan si en un trimestre el equipo pasa > 30 % del tiempo en 🔴: o el SLO es demasiado estricto para la arquitectura, o la arquitectura necesita inversión. Ambas cosas son decisiones de negocio, no del on-call.
