# OMS Operations: trabajo final del Bloque 5

Documentación operativa del **OMS (AcmeOMS)**: lo que convierte la plataforma "viva" del Bloque 4 (GCP: Cloud SQL, Memorystore, Cloud Run, Load Balancer, CI/CD con WIF) en una plataforma **gobernable**.
El enunciado completo está en [`../Trabajo - enunciado.md`](../Trabajo%20-%20enunciado.md).

## Qué hay en la entrega

| Entregable del enunciado | Dónde | Estado |
|---|---|---|
| Runbook obligatorio `01-db-pool-saturated` | [`runbooks/01-db-pool-saturated/`](runbooks/01-db-pool-saturated/) | ✅ runbook + alertas + 3 post-mortems |
| 2 runbooks a elegir del catálogo | [`02-api-p95-high`](runbooks/02-api-p95-high/) · [`03-stripe-payment-gateway-degraded`](runbooks/03-stripe-payment-gateway-degraded/) | ✅ |
| **Bonus:** 4.º y 5.º runbook | [`04-outbox-backlog-creciente`](runbooks/04-outbox-backlog-creciente/) · [`05-redis-cache-down`](runbooks/05-redis-cache-down/) | ✅ |
| Runbooks adicionales (resto del catálogo y modos de fallo propios) | `06` a `12`, ver [índice de runbooks](runbooks/README.md) | ✅ |
| 2 SLOs con SLI exacto | [`slos/slos-orders.md`](slos/slos-orders.md) (2 SLOs) · [`slos/slos-payments.md`](slos/slos-payments.md) + [recording rules](slos/slo-recording-rules.yaml) | ✅ |
| Política de error budget | [`slos/error-budget-policy.md`](slos/error-budget-policy.md) | ✅ |
| GameDay plan | [`gameday/gameday-plan.md`](gameday/gameday-plan.md) (valida el runbook 01) | ✅ plan · ejecución el 25/11/2026 |
| Documento de instrumentación | [`observability/instrumentation.md`](observability/instrumentation.md) | ✅ |
| **Bonus:** ethical risk assessment | [`ethics/ethical-risk-assessment.md`](ethics/ethical-risk-assessment.md) | ✅ |
| **Bonus:** runbook automatizado (idempotente, `--dry-run`) | [`scripts/db-pool-saturated.sh`](scripts/db-pool-saturated.sh) | ✅ probado con un `psql` simulado |
| Presentación del trabajo (15 diapositivas, con notas del orador para el vídeo) | [`../doc/OMS-Operations-Bloque5.pptx`](../doc/OMS-Operations-Bloque5.pptx) | ✅ |
| **Bonus:** dashboards de Grafana en JSON | [`observability/grafana/`](observability/grafana/) (6 dashboards, generados desde código) | ✅ |

## Estructura

```
oms-operations/
├── runbooks/        ← 12 runbooks (01 obligatorio, 02-03 elegidos, 04-05 bonus, 06-12 adicionales)
│   ├── README.md    ← índice, árbol de decisión, CONVENCIONES (nombres GCP, setup de sesión, feature flags)
│   └── _template/   ← plantillas (conservan sus TODO a propósito)
├── slos/            ← 3 SLOs, política de error budget y recording rules
├── observability/   ← plan de instrumentación + dashboards de Grafana
├── gameday/         ← GameDay #1 sobre el runbook 01
├── scripts/         ← automatización V3 del runbook 01
└── ethics/          ← evaluación de riesgos éticos del asistente IA (bonus)
```

## Cómo arrancar

1. **Lee el enunciado** completo (`../Trabajo - enunciado.md`).
2. **Instrumentación primero**: [`observability/instrumentation.md`](observability/instrumentation.md) fija el nombre de cada métrica y por qué existe. Todo lo demás cita esas métricas.
3. **SLOs y política**: [`slos/`](slos/). Las alertas de burn rate salen de aquí.
4. **Runbooks**: antes de nada, las convenciones y el *setup de sesión* de [`runbooks/README.md`](runbooks/README.md); después, el runbook que toque según el árbol de decisión.
5. **GameDay**: [`gameday/gameday-plan.md`](gameday/gameday-plan.md).

## Verificación antes de entregar

```bash
# TODO pendientes (solo deben quedar los de runbooks/_template/, que son plantillas)
grep -rln "TODO" . --exclude-dir=_template --exclude=README.md | wc -l   # → 0

# Cada runbook tiene los 3 ficheros mínimos
for d in runbooks/[0-9]*/; do
  for f in runbook.md alert.yaml post-mortems.md; do
    [ -f "$d/$f" ] && echo "✓ $d$f" || echo "✗ FALTA $d$f"
  done
done

# Cada alerta tiene runbook_url
grep -L "runbook_url" runbooks/[0-9]*/alert.yaml slos/*.yaml   # → vacío

# Cada SLO usa good/valid events (no salud de infra)
grep -L "good\|valid" slos/slos-*.md                            # → vacío

# Sintaxis YAML de todas las reglas y JSON de los dashboards
for f in runbooks/[0-9]*/alert.yaml slos/*.yaml; do python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" "$f" && echo "✓ $f"; done
for f in observability/grafana/*.json; do jq -e .uid "$f" >/dev/null && echo "✓ $f"; done
# Con promtool disponible: promtool check rules runbooks/*/alert.yaml slos/slo-recording-rules.yaml

# Script del bonus
bash -n scripts/db-pool-saturated.sh && scripts/db-pool-saturated.sh --help | head -3
```

---

## Decisiones

### Los 3 trade-offs principales

**1. El SLO de `POST /api/orders` está en 99,9 % y no en el 99,5 % que sugería la plantilla, aunque eso deja un budget de solo 70 peticiones en 28 días.**
NFR-AVAIL-001 compromete el checkout al 99,9 % con multi-AZ y failover automático. Un SLO interno más laxo que la spec no avisaría a tiempo de que estamos incumpliendo el compromiso. El coste es real: con ~2.500 peticiones al día, **un solo 5xx es el 1,4 % del budget** y el incidente V1 del runbook 01 lo agotó (⚫, freeze de 2 semanas). Lo compensamos de tres formas: (a) las alertas de burn rate exigen un **mínimo de tráfico** en la ventana para que un 5xx aislado de madrugada no despierte a nadie; (b) la política de error budget pasa a 🟡 cuando **un único incidente consume más del 20 %**, no solo al cruzar el 50 %; y (c) la mitigación de Stripe se diseñó para devolver **202 (`PENDING_PAYMENT`) en lugar de 5xx** (ver trade-off 2). A cambio, aceptamos explícitamente que el equipo pasará más tiempo en 🟡 que con un 99,5 %.

**2. Los fallos de Stripe cuentan contra el SLO de Payments (99,0 %), y el circuit breaker protege el SLO de Orders a costa del de Payments.**
Excluir a Stripe facilitaba cumplir el SLO, pero el cliente no distingue "falló Stripe" de "falló AcmeOMS" y nos quitaba el incentivo para mitigar. Al contarlo, el circuit breaker, los timeouts, los reintentos con idempotency key y la cola de `PENDING_PAYMENT` tienen una justificación medible. La parte delicada fue decidir que `queued` (breaker abierto) cuenta como **malo** para Payments: el cliente tiene su pedido, pero no sabe si ha pagado. Contarlo como bueno haría que "forzar el breaker" arreglara el SLO sin mejorar nada para el cliente. Para no castigar al equipo por algo que no controla, la política tiene una excepción acotada: el consumo de budget por un incidente de Stripe **cuenta, pero no congela features** si los runbooks se ejecutaron en < 15 min y nuestra parte del consumo es < 50 %, medida con un SLI de visibilidad separado.

**3. En los runbooks, el feature flag va siempre antes que el redeploy o el escalado. En el runbook 01, "pausar el batch" va antes que "ampliar el pool", y nunca se amplía el pool sin recalcular la capacidad.**
La reacción intuitiva ante un pool saturado es subir `DB_POOL_SIZE`. Lo pusimos en tercer lugar por dos motivos: (a) requiere un rolling update, mientras que un flag se aplica en ≤ 15 s y se revierte igual de rápido; y (b) **más conexiones contra una BD saturada empeoran el problema**: `max-instances × DB_POOL_SIZE` de todos los servicios tiene que quedar por debajo de 380 (`max_connections` 400 menos las reservadas). Por eso la Acción 3b sube el pool **y baja `max-instances` a la vez**, y el diagnóstico 4 prohíbe ampliar si la CPU de Cloud SQL es > 85 %. El mismo criterio (primero quitar carga sin usuario esperando, después añadir capacidad) se aplica en los runbooks 02, 05 y 12, y es la razón de que exista un [catálogo común de kill switches](runbooks/README.md#catálogo-de-feature-flags-operativos-kill-switches).

### Otras decisiones de interpretación

- **GCP y no AWS.** El anexo de arquitectura (Bloque 2) está escrito sobre AWS, pero el enunciado del Bloque 5 parte de la plataforma del Bloque 4 en GCP. Se mantienen los módulos, los ADR y la lógica del anexo, y se traducen los servicios (RDS → Cloud SQL, ElastiCache → Memorystore, ECS → Cloud Run, CloudFront → Cloud CDN + Cloud Armor). Regiones: `europe-west3` (primaria) y `europe-west1` (DR), ambas en la UE (REG-GDPR-001).
- **Umbrales de latencia del enunciado sobre los del anexo.** El enunciado fija p95 < 250 ms y p99 < 800 ms "en endpoints críticos"; el anexo da 400 ms para `POST /api/orders`. Como `POST` incluye la llamada síncrona a Stripe, el SLO de latencia se define sobre la **consulta** de pedidos (250 ms y 800 ms). Para `POST` se vigila el p99 < 800 ms y la latencia de Stripe queda dentro del SLO de Payments (< 3 s).
- **Un monolito, tres servicios de Cloud Run.** La misma imagen (ADR-001) se despliega como `oms-catalog`, `oms-orders` y `oms-outbox-relay` para escalar por perfil de carga. Es lo que hace posible la fórmula de capacidad del pool y que un pico de catálogo no se coma las conexiones del checkout.
- **Stripe fuera de la transacción ACID.** El §5.1 del anexo mete la autorización de Stripe dentro de la transacción del pedido. Operativamente, eso retiene una conexión de BD durante toda la llamada externa: cuando Stripe va lento, satura el pool (PM #1 del runbook 03). La acción derivada (OMS-1402) la saca de la transacción: se reserva y se crea el pedido en `PENDING_PAYMENT`, se autoriza fuera y se confirma en una segunda transacción. Es una enmienda a ADR-003 que propone la operación y aprueba arquitectura.
- **Carpetas de runbooks renombradas** para corregir erratas (`chache`, `could`, `inisponible`) y unificar el formato `NN-kebab-case`, porque el nombre de la carpeta es el slug de la `runbook_url` de cada alerta.
- **Post-mortems simulados.** Las historias V1 → V2 → V3 son escenarios construidos sobre la arquitectura real del OMS, con métricas coherentes con los SLOs. El GameDay **está planificado y no ejecutado**: su sección de resultados se rellena el día del ejercicio.

### Cambios al borrador de la IA (obligatorio, §8 del enunciado)

> Los documentos se redactaron a partir de un borrador generado con Claude (Claude Code). Esta sección recoge los **3 cambios concretos** que hice al revisarlo antes de aprobarlo.
> ✍️ **Pendiente de completar por el autor tras su revisión crítica.** Tiene que reflejar cambios reales: qué decía el borrador, qué cambié y por qué.

| # | Documento | Qué decía el borrador | Qué cambié | Por qué |
|---|---|---|---|---|
| 1 | oms-operations/runbooks/08-redis-cache-hit-ratio-low/runbook.md|**< 80 %** durante **15 minutos** | **< 70 %** durante **10 minutos**| Prefiero bajar el porcentade de aciertos y disminuir el tiempo|
| 2 | oms-operations/runbooks/08-redis-cache-hit-ratio-low/alert.yaml|       for: 15m           catalog:cache_hit_ratio:rate5m < 0.80|      for: 10m           catalog:cache_hit_ratio:rate5m < 0.70| Prefiero bajar el porcentade de aciertos y disminuir el tiempo|
| 3 | oms-operations/runbooks/11-catalog-unavailable/post-mortems.md| tumbar| tirar | Me parece mas adecuada la palabra a la realidad|

## Si te bloqueas

- Vuelve al vídeo correspondiente (mapeo en `../Trabajo - enunciado.md` §7)
- Canal Slack `#trabajo-bloque-5`
- Lee tus runbooks en voz alta como si fueras el on-call a las 3 AM
