# Observability

> Plan de instrumentación: qué se monitoriza en cada módulo del OMS y por qué. Incluye los dashboards que lo hacen visible.

## Ficheros

| Fichero | Qué contiene |
|---|---|
| [`instrumentation.md`](instrumentation.md) | Tabla `quality attribute → métrica → SLI → alerta` por módulo (Catalog, Inventory, Orders, Payments, Billing, IAM, Outbox), infraestructura compartida, logs, trazas y coste |
| [`grafana/generate_dashboards.py`](grafana/generate_dashboards.py) | Genera los dashboards desde código: las queries son las mismas que las de los SLOs y alertas, y cada panel enlaza a su runbook |
| `grafana/*.json` | **Bonus:** 6 dashboards importables (schemaVersion 39): `oms-overview` (SLOs, budget, burn rate, golden signals), `oms-db-overview`, `oms-payments`, `oms-cache`, `oms-outbox`, `oms-deploys` |

## Importar los dashboards

```bash
cd observability/grafana
python3 generate_dashboards.py            # regenera los JSON tras cambiar una query
for f in oms-*.json; do
  jq '{dashboard: ., overwrite: true, folderUid: "oms"}' "$f" \
    | curl -sf -X POST "$GRAFANA_URL/api/dashboards/db" \
        -H "Authorization: Bearer $GRAFANA_TOKEN" -H "Content-Type: application/json" -d @- \
    && echo "importado $f"
done
```

## Regla del oficio

Cada métrica que existe en tu sistema debe poder defenderse así:
> "Esta métrica existe porque la spec dice X, y la usamos para verificar Y."

Si no puedes responder esa frase, **no instrumentes esa métrica**. La telemetría tiene coste, y el ruido entierra la señal.
