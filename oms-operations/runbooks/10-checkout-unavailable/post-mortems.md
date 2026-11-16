# Post-mortems del runbook: checkout-unavailable

> *Escenarios simulados para el trabajo del Bloque 5, construidos sobre la arquitectura real del OMS.*
> Evolución del runbook de triage a lo largo de 3 caídas del checkout con causas en capas distintas: V1 manual → V2 árbol con comandos → V3 triage automático y prevención en el borde.
> Formato blameless: se analizan sistemas y procesos, no personas. Los on-call se identifican por rol.

---

## Versión 1: mitigación manual (PM #1)

**Fecha del incidente:** jueves 23 de abril de 2026, 13:05 → 14:02 CEST
**Duración del incidente:** 57 minutos (primer bloqueo a las 13:05 → checkout funcionando a las 14:02)
**Detectado por:** chat de soporte a las 13:31, escalado al on-call a las 13:38. **Ninguna alerta disparó.**
**On-call:** SRE de guardia (semana 17)
**Impacto:** ~230 intentos de `POST /api/orders` (incluidos reintentos de clientes), **~190 rechazados con 429** por Cloud Armor. ~95 clientes afectados y **~70 pedidos menos** que el jueves anterior. **SLO 1 de Orders: 0 % consumido**, porque un 429 es un 4xx y el SLI lo excluye. **NFR-AVAIL-001: 57 min de un presupuesto de 43 min al mes → abril incumplido.**

### Lo que ocurrió
La semana anterior Stripe Radar había detectado un intento de *card testing* contra nuestro checkout. Para frenarlo se añadió por Terraform una regla `rate_based_ban` en `oms-edge-policy` (prioridad 900) sobre `POST /api/orders`: 5 peticiones por minuto y baneo de 10 min. El PR se fusionó a las 12:58. El módulo de Terraform tenía `enforce_on_key = "ALL"` por defecto y nadie lo cambió a `"IP"`: el límite era **5 pedidos por minuto para todos los clientes juntos**. A la hora de comer se superó en minutos y Cloud Armor baneó el checkout entero en ciclos de 10 min. El SPA mostraba el JSON de error en crudo.

### Cómo se mitigó (improvisado, sin runbook)
El on-call miró el dashboard del SLO (verde), los 5xx (cero), el pool (normal) y Stripe (normal). Durante 15 min todo decía que el sistema estaba sano. A las 13:55, buscando en los logs del LB, vio `statusDetails="denied_by_security_policy"`, encontró la regla y la **borró** con `gcloud compute security-policies rules delete 900`. El checkout volvió a las 14:02. Hubo que reconstruir la regla (bien) desde Terraform al día siguiente.

### Aprendizajes y acciones
- **El SLO no ve el borde.** Un bloqueo de Cloud Armor es un 4xx y no consume SLO, pero el cliente no puede comprar igual. → Probe sintética `checkout-e2e` desde europe-west1 y alerta `CheckoutUnavailable` sobre ella. *(Hecho, 30/04)*
- **No había forma rápida de saber "en qué capa".** El on-call revisó la app de arriba abajo antes de mirar el LB. → Runbook V1 con un árbol de capas. *(Hecho, 05/05)*
- **Borrar la regla destruyó la evidencia.** → Mitigar poniendo la regla en `--preview`, no borrándola.
- **El cliente vio un error en crudo.** → Flag `checkout.maintenance-page` con mensaje amable y carrito guardado. *(Hecho, 20/05)*
- **Las reglas de Cloud Armor entran sin probar contra tráfico real.** → Pendiente: estrategia de despliegue para reglas del borde.
- **El NFR y el SLO miden cosas distintas.** Se documenta en el runbook: el SLO 1 mide la app y la probe mide el checkout completo para NFR-AVAIL-001.

### Versión 1 del runbook tras este post-mortem

> **Runbook v1 (texto literal):**
> "Si dispara CheckoutUnavailable, revisar por capas: Cloud Armor y LB, Cloud Run, Postgres, pool,
> Stripe. Si es Cloud Armor, poner la regla en preview. Si es otra capa, ir a su runbook. Si no se
> encuentra en 15 min, activar checkout.maintenance-page."

(Lista las capas, pero no da comandos ni orden. Por eso hay V2.)

---

## Versión 2: árbol de triage con comandos (PM #2)

**Fecha del incidente:** miércoles 15 de julio de 2026, 10:12 → 10:31 CEST
**Duración del incidente:** 19 minutos (alerta 10:15 → mitigado 10:27 → verificado 10:31)
**Detectado por:** `CheckoutUnavailable` a las 10:15 (`signal=probe`, 2 de 3 fallos, y `signal=5xx` con 38 %).
**On-call:** SRE de guardia (semana 29), otra persona distinta a la de PM #1.
**Impacto:** ~45 intentos de `POST /api/orders`, **17 con 5xx** (503 por `lock_timeout`) → **24 % del budget del SLO 1** (🟡). 19 min de indisponibilidad parcial para NFR-AVAIL-001.

### Qué se vio durante esta segunda ocurrencia
A las 10:10 el equipo de backoffice lanzó el script `oms-backoffice-import` para actualizar el stock de 3.000 SKUs **en una única transacción** que tardó ~4 min y bloqueó filas de `inventory.stock_items`. Los pedidos que reservaban esos productos esperaban el lock y fallaban. El runbook V1 sirvió para no perderse, pero:
1. **Empezó por Cloud Armor** (el sesgo del PM #1) y el on-call perdió 4 min sin comandos para descartarla.
2. **No tenía una pregunta que separase capas en un paso.** Un único `gcloud logging read` de los códigos del LB habría mostrado `response_sent_by_backend` (la app responde, el borde está bien) en 30 s.
3. **No contemplaba los locks de inventory.** El on-call llegó por intuición al runbook 01, cuyo paso 3 encontró la sesión bloqueante de `oms-backoffice-import`, y la terminó con la Acción 1b.
4. **No preguntaba "¿qué ha cambiado?"**, que es la pregunta que más incidentes resuelve.

### Qué cambió en el runbook (V1 → V2)
- **Paso 1 nuevo: códigos del LB con `statusDetails`.** Una tabla dice qué capa es en 1 min, antes de recorrer ramas.
- **Paso 2 nuevo: qué ha cambiado en 2 h** (revisiones de Cloud Run, logs de auditoría de borde/SQL/secretos y eventos de Unleash).
- **Ramas A–H con comando y destino**, incluidas las nuevas: **E (locks de inventory)**, G (login) y H (solo falla la probe).
- **Acciones propias ordenadas** (preview de Cloud Armor → revertir tráfico → página de mantenimiento → failover regional) y **plantillas de comunicación** para status page, soporte y negocio.
- **Verificación con negocio**: pedidos/min frente a la semana pasada, además de probe y 5xx.

### Acciones derivadas
- `oms-backoffice-import` en lotes de 100 filas con `SET lock_timeout = '2s'`. *(Hecho, 22/07)*
- Ejecutar el triage automáticamente cuando dispara la alerta: los pasos 0–2 son siempre los mismos y no requieren criterio.
- Cerrar la acción pendiente del PM #1: reglas de Cloud Armor en preview antes de aplicarlas.

---

## Versión 3: triage automático y prevención en el borde (PM #3)

**Fecha:** martes 22 de septiembre de 2026, 16:02 → 16:06 CEST. **Incidente de 4 min, sin intervención manual en la mitigación.**

### Qué pasó (y qué no)
**Lo que no pasó (15/09):** un PR endurecía la regla WAF preconfigurada `sqli-v33-stable` (sensibilidad 2). Entró en `preview` y, a las 24 h, el chequeo `armor-preview-check` del pipeline encontró que habría bloqueado el **3,1 % de los `POST /api/orders`**: direcciones como "C/ O'Donnell 12 -- 3º B". El PR no se promocionó. Sin este control habría sido el PM #1 otra vez, ahora con un bloqueo parcial y más difícil de ver.
**Lo que sí pasó (22/09):** la revisión `oms-orders-00131` entró en canary al 10 % con un error que devolvía 500 en todos sus pedidos. A las 16:05 disparó `CheckoutUnavailable` (`signal=5xx`, 9 %). En 40 s, `oms-ops-checkout-triage` publicó en `#incident-response`: *"Capa B · 94 % de los 5xx en oms-orders-00131 (creada hace 7 min) · runbook 07"*. El auto-rollback del canary (runbook 07) ya había devuelto el tráfico a la revisión anterior a las 16:05. El on-call solo verificó y cerró. **5 errores 5xx → 7 % del budget del SLO 1.**

### Qué cambió en el runbook (V2 → V3)
- **Los pasos 0–2 y las ramas A–H están automatizados** en `oms-ops-checkout-triage`. El runbook abre con "lee la tarjeta de triage y confirma con el comando de la rama".
- **Prevención en el borde:** toda regla nueva de Cloud Armor entra en `preview` 24 h y el pipeline solo la promociona si `armor-preview-check` no encuentra denegaciones en `/api/orders` desde > 20 IPs distintas.
- **Nueva alerta** `CheckoutTriageAutomationStale` (autotest cada 6 h) y `CheckoutProbeMissing`: las dos piezas que nos dan visibilidad también necesitan vigilancia.

### El runbook como automatización
- **Triage:** Alertmanager (webhook) → servicio `oms-ops-alert-webhook` → `gcloud run jobs execute oms-ops-checkout-triage` cuando dispara `CheckoutUnavailable` u `OrdersAvailabilityBurnRateFast`. El job lanza en paralelo las consultas de los pasos 0–2 y de las ramas A–H (Prometheus, Cloud Logging, Cloud Run Admin API, Unleash), puntúa cada capa y publica la tarjeta con la capa sospechosa, la evidencia y el enlace al runbook. **No ejecuta acciones**: solo diagnostica.
- **Autotest:** Cloud Scheduler `oms-ops-checkout-triage-selftest` (`0 */6 * * *`, `Europe/Madrid`) ejecuta el job en modo `--dry-run` contra el estado actual y publica `ops_job_last_success_timestamp_seconds{job="checkout-triage-selftest"}`.
- **Prevención:** job `armor-preview-check` del pipeline de Terraform (consulta `jsonPayload.previewSecurityPolicy.outcome="DENY"` en los logs del LB de las últimas 24 h).
- **Owner:** equipo SRE. **Salud:** `CheckoutTriageAutomationStale` (> 7 h sin autotest) y `CheckoutProbeMissing`.

### Métrica del runbook a lo largo de la evolución

| | V1 (manual) | V2 (árbol con comandos) | V3 (triage automático + preview) |
|---|---|---|---|
| Detección | Soporte, +26 min | Alerta (probe + 5xx), +3 min | Alerta (5xx), +3 min |
| Tiempo hasta localizar la capa | 50 min | 12 min | 40 s (tarjeta de triage) |
| Duración total | 57 min | 19 min | 4 min |
| Peticiones fallidas | ~190 (429) | 17 (5xx) | 5 (5xx) |
| SLO 1 consumido | 0 % (ciego al borde) | 24 % (🟡) | 7 % |
| NFR-AVAIL-001 (43 min/mes) | 57 min, incumplido | 19 min (parcial) | 4 min (parcial) |
| Toil del on-call | Alto (revisar todo a ciegas) | Medio (recorrer el árbol) | Bajo (confirmar la tarjeta) |
| Siguiente mejora | Probe y árbol de capas | Automatizar triage, preview en Cloud Armor | Llevar la probe a móvil/SPA real (hoy solo prueba la API) |

---

## Conclusión

Este runbook no arregla nada por sí mismo: su valor es **acortar el tiempo entre "algo va mal" y "sé qué capa es"**. En V1 aprendimos la lección más incómoda del bloque: un SLO perfectamente verde mientras nadie podía comprar, porque el SLI mide la app y el fallo estaba en el borde. De ahí sale la probe end-to-end y la distinción explícita entre SLO 1 y NFR-AVAIL-001. En V2 el árbol se convirtió en preguntas con comando, y la pregunta que más tiempo ahorra resultó ser la más simple: "¿qué código recibe el cliente en el balanceador?". En V3 la máquina hace las preguntas y el humano decide. El cambio de más valor no fue el triage automático sino el `preview` de Cloud Armor: el incidente del PM #1 ya no puede repetirse sin que antes lo vea el pipeline. Queda pendiente que la probe cubra el SPA real (JavaScript, Stripe.js en el navegador), porque hoy prueba la API y no el botón que pulsa el cliente.
