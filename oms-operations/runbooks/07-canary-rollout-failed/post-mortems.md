# Post-mortems del runbook: canary-rollout-failed

> *Escenarios simulados para el trabajo del Bloque 5, construidos sobre la arquitectura real del OMS.*
> Evolución del runbook a lo largo de 3 ocurrencias del incidente: V1 manual → V2 con comandos → V3 automatizada.
> Formato blameless: se analizan sistemas y procesos, no personas. Los on-call se identifican por rol.

---

## Versión 1: mitigación manual (PM #1)

**Fecha del incidente:** martes 26 de mayo de 2026, 12:10 → 12:51 CEST
**Duración del incidente:** 41 minutos (deploy a las 12:10 → verificación a las 12:51)
**Detectado por:** `OrdersAvailabilityBurnRateFast` a las 12:22, 12 min después del deploy. No había ninguna alerta ligada a los despliegues.
**On-call:** SRE de guardia (semana 22)
**Impacto:** ~110 intentos de `POST /api/orders` en la franja; el 35 % (los que llevaban cupón de la campaña de primavera) fallaron con 500: **~38 pedidos fallidos, el 54 % del budget del SLO 1 de Orders** (~70 en 28 d). La política pasó de 🟢 a 🟡.

### Lo que ocurrió
La release 2026.05.3 de `oms-orders` cambió la validación de cupones. Para clientes sin cuenta (checkout de invitado), el código accedía al perfil del cliente sin comprobar que existía y lanzaba una excepción. El pipeline de entonces hacía `gcloud run deploy` sin más: Cloud Run pasó el 100 % del tráfico a la revisión nueva en cuanto superó el health check. El diseño del anexo preveía "rollback automático si error rate > 5 % durante 5 min tras el deploy", pero nunca se implementó en la migración a Cloud Run del Bloque 4.

### Cómo se mitigó (improvisado, sin runbook)
1. El on-call vio la burn rate y los 500 en `POST /api/orders`. Tardó 6 min en relacionarlo con el deploy de las 12:10 (lo vio por casualidad en `#oms-releases`).
2. No sabía cómo volver a la versión anterior sin redesplegar. Relanzó en GitLab el pipeline del tag anterior: build, tests y deploy, **19 minutos**.
3. A las 12:47 la revisión anterior (reconstruida) volvió a servir el 100 %. Verificó 4 min y cerró.

### Aprendizajes y acciones
- **Revertir no requiere reconstruir nada:** la revisión anterior seguía existiendo en Cloud Run. `gcloud run services update-traffic --to-revisions=<anterior>=100` lo habría hecho en 30 s. → Runbook V1 con ese comando. *(Hecho, 27/05)*
- **El 100 % de golpe convierte cada bug en un incidente para todos los clientes.** → Canary con tags: `gcloud run deploy --no-traffic --tag canary` + `update-traffic --to-tags canary=10`. *(Hecho, 10/06)*
- **No había alerta ligada al deploy:** detectamos por el SLO, tarde. → Alerta `CanaryRolloutFailed` comparando errores por `revision`. *(Hecho, 10/06)*
- El "rollback automático" del anexo nunca existió y nadie lo había comprobado. → Revisar qué controles del diseño original están realmente implementados.

### Versión 1 del runbook tras este post-mortem

> **Runbook v1 (texto literal):**
> "Si los errores suben después de un deploy: buscar la revisión anterior con `gcloud run revisions list --service=<svc>`
> y pasarle todo el tráfico con `gcloud run services update-traffic <svc> --to-revisions=<anterior>=100`."

(Revierte rápido, pero no dice cómo saber si la culpa es del deploy ni qué hacer con las migraciones. Por eso hay V2.)

---

## Versión 2: diagnóstico y comandos exactos (PM #2)

**Fecha del incidente:** miércoles 1 de julio de 2026, 10:31 → 10:58 CEST
**Duración del incidente:** 27 minutos (promoción al 50 % a las 10:31 → verificado a las 10:58)
**Detectado por:** `APILatencyP95High` en `oms-orders` / `/api/orders/{id}` a las 10:41 (entonces era una única alerta a 250 ms; la versión warning/critical llegó con el PM #2 del runbook 02). `CanaryRolloutFailed` no disparó: solo miraba errores.
**On-call:** SRE de guardia (semana 27), distinta persona que en PM #1.
**Impacto:** sin errores. El detalle de pedido pasó de 140 ms a 680 ms de p95 para la mitad del tráfico durante 27 min: ~350 peticiones por encima de 250 ms, el **0,6 % del budget del objetivo del 95 % del SLO 2**. Poco budget, pero mucho riesgo: con la promoción al 100 % habría afectado a todos.

### Qué se vio durante esta segunda ocurrencia
La release 2026.07.1 añadía el seguimiento del envío al detalle de pedido. El código cargaba los envíos de cada línea por separado (N+1 queries: 25 queries por pedido). El canary existía, pero:
1. La promoción de 10 % a 50 % era **manual** y se hizo al minuto de llegar al 10 %, mirando solo el panel de errores. No había tiempo mínimo por etapa.
2. `CanaryRolloutFailed` solo comparaba 5xx. Una regresión de latencia pasaba sin alerta hasta que el p95 global superaba el umbral.
3. Con el runbook V1, el on-call revirtió en 1 min a las 10:45. Antes dudó 4 min: la release incluía una migración que añadía `orders.shipments.tracking_url` y preguntó en `#oms-sre` si había que "deshacerla" también. Nadie lo tenía claro.
4. Para encontrar la revisión anterior tuvo que comparar fechas en `gcloud run revisions list`. Con dos servicios desplegando a la vez, se equivocó de servicio en el primer intento (inocuo, pero confuso).

### Qué cambió en el runbook (V1 → V2)
- **Tags fijos** `stable`, `canary` y `previous`: los comandos usan `$STABLE` y `$CANARY` obtenidos del servicio, nunca nombres buscados a mano.
- **Criterio de latencia** en `CanaryRolloutFailed` (`reason="latency"`: p95 > 1,5× estable y > 250 ms) y **mínimo de 100 peticiones** para no parar releases sanas a primera hora.
- **Diagnóstico ordenado:** tráfico actual → pipeline vivo → ¿canary o entorno? → endpoints → logs → flags y migraciones de la release.
- **Flag antes que tráfico** cuando la funcionalidad nueva tiene flag de release (`release.<ticket>-<slug>`), obligatorio en la plantilla de MR para cambios de comportamiento.
- **Migraciones explícitas:** expand-and-contract, el esquema **no se revierte**; si hay una migración `contract` en la misma release, Acción 3 con aprobación del EM.
- **Etapas con tiempo mínimo** (10 min en 10 % y 10 min en 50 %) y job `check-deploy-window` que impone OPS-002 y la variable `DEPLOY_FREEZE`.

### Acciones derivadas
- Exportar `run_revision_traffic_percent{job, revision, tag}` desde la API de Cloud Run para que las alertas sepan qué revisión es cada cosa. *(Hecho, 08/07)*
- Fix de la N+1 con una única query con `JOIN` y test de rendimiento en CI. *(Hecho, release 2026.07.2)*
- Automatizar la decisión de promoción: un humano mirando un panel durante 10 min no es fiable.

---

## Versión 3: auto-rollback en el pipeline (PM #3)

**Fecha:** jueves 8 de octubre de 2026, 10:12 → 10:19 CEST. **El auto-rollback actuó antes de que nadie fuera paginado.**

### Qué pasó (o, mejor, qué NO pasó)
La release 2026.10.2 de `oms-orders` añadía el pago en 3 plazos detrás del flag `release.oms-2291-installments`. Con tarjeta regalo, el cálculo de plazos dividía por un importe nulo: 5xx en `POST /api/orders`. A las 10:12 la canary pasó al 10 %. A las 10:16 el job `canary-analysis` vio 3,1 % de 5xx en la canary frente a 0,1 % en la estable; tras 3 chequeos malos seguidos, a las 10:18:40 **apagó el flag de la release, devolvió el 100 % del tráfico a `stable` y marcó el pipeline en rojo**. Publicó el resumen en `#oms-releases` y abrió una issue con los `trace_id`. `CanaryRolloutFailed` llegó a *pending* pero no a *firing* (le faltaban 2 min del `for`).
**Impacto:** 4 `POST /api/orders` fallidos (10 % del tráfico durante 6 min): el **6 % del budget del SLO 1**. La corrección salió el lunes 12 por la ventana normal (no se despliega en viernes, OPS-002).

### Qué cambió en el runbook (V2 → V3)
- **`canary-analysis` es un job del pipeline** (imagen `oms-ci-tools`, etapa tras `canary-10` y tras `canary-50`). Cada 60 s durante 10 min ejecuta contra Prometheus **las mismas expresiones** que `CanaryRolloutFailed` (fichero compartido `ci/canary-queries.yaml`, así la alerta y el job no pueden divergir).
  - 3 chequeos malos seguidos → `flag_off` de los flags de la release, `update-traffic --to-revisions=<stable>=100`, pipeline en rojo, aviso en `#oms-releases` e issue en GitLab.
  - Menos de 100 peticiones en la canary → alarga la etapa hasta 30 min. **Nunca promociona a ciegas.**
  - 10 min limpios → pasa a la siguiente etapa sin humano.
- **La automatización se vigila:** en cada chequeo publica `ops_job_last_success_timestamp_seconds{job="canary-analysis"}` (OTLP al collector). `CanaryAnalysisNotRunning` avisa si hay una canary con tráfico y 5 min sin latido. `CanaryRolloutStuck` avisa de canaries olvidadas > 2 h.
- El on-call **ya no promociona ni revierte en el caso normal**: el runbook queda para cuando el job no está (pipeline caído, rollout manual de emergencia), para la variante "ya estaba al 100 %" y para la Acción 3 (migración `contract`).
- `CanaryRolloutFailed` se mantiene critical como red de seguridad: si dispara con el job vivo, el job tiene un bug.

### El runbook como automatización

- **Job:** `canary-analysis` en `.gitlab-ci.yml` del repo `oms/oms-app`; lógica en `ci/canary_analysis.py`, umbrales en `ci/canary-queries.yaml`.
- **Disparador:** cada etapa `canary-10` y `canary-50` del pipeline de release (solo en ventana OPS-002).
- **Owner:** equipo SRE (job y umbrales); el equipo de la release es responsable de declarar sus flags en la descripción del MR.
- **Salud:** `ops_job_last_success_timestamp_seconds{job="canary-analysis"}`, vigilada por `CanaryAnalysisNotRunning`.

### Métrica del runbook a lo largo de la evolución

| | V1 (manual) | V2 (comandos) | V3 (auto-rollback) |
|---|---|---|---|
| Tráfico expuesto a la release defectuosa | 100 % | 50 % | 10 % |
| Detección | Burn rate, 12 min después del deploy | Alerta de latencia, 10 min después de promocionar | `canary-analysis`, 4 min |
| Tiempo hasta revertir | 37 min (rebuild) | 14 min (incluye 4 min de duda) | 6 min 40 s, sin humano |
| Peticiones afectadas | ~38 pedidos fallidos | ~350 consultas lentas | 4 pedidos fallidos |
| Budget consumido | 54 % SLO 1 (🟢 → 🟡) | 0,6 % SLO 2 | 6 % SLO 1 |
| Toil del on-call | Alto (adivinar, rebuild) | Medio (comandos con tags) | Cero (lee el resumen en Slack) |
| Siguiente mejora | Canary + rollback por tráfico | Automatizar la decisión de promoción | Releases más pequeñas con flag por defecto |

---

## Conclusión

Lo que más cambió en este runbook no fue la velocidad de los comandos, sino **cuánta gente ve el fallo**. En V1 el 100 % de los clientes probó la release defectuosa y tardamos 37 min en revertir porque creíamos que había que reconstruir. En V2 la canary limitó la exposición, pero dependía de que un humano mirara el panel correcto durante el tiempo suficiente, y una regresión de latencia se coló hasta el 50 %. También aprendimos que la pregunta "¿y la migración?" paraliza al on-call si no está respondida por escrito: con expand-and-contract el esquema nunca se revierte. En V3 la decisión de promocionar o revertir la toma un job con los mismos umbrales que la alerta, y la release defectuosa solo llega al 10 % durante unos minutos. El aprendizaje incómodo fue el mismo que en los runbooks 01 y 02: **el auto-rollback también necesita su propia alerta**, porque una canary sin nadie que la mire es peor que no tener canary: da una falsa sensación de seguridad.
