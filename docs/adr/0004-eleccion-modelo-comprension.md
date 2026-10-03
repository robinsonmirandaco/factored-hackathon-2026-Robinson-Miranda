# ADR 4: elección del modelo de comprensión

- Estado: aceptada
- Fecha: 2026-10-03
- Fuentes: `docs/reports/comprension_prueba.md`, `docs/reports/comprension_prueba_sonnet.md`, `docs/reports/evaluacion.md` (declaraciones)

## Contexto

- Historia: TRZ-12 CA10 (selección de modelo sobre el mismo held-out, en calidad, costo y latencia).
- Cambio respecto del diseño: se comparó `claude-sonnet-5-5` (el Sonnet vigente) en lugar de `claude-sonnet-5`. Sonnet 5.5 no acepta `temperature`: corrió con muestreo por defecto y sin razonamiento (`thinking: between_tools`); Haiku corre con temperatura 0.
- Sonnet corrió con plazo de 15 s para medir su calidad sin respaldos por plazo; producción usa 5 s.

## Regla, fijada antes de ver resultados (2026-10-02)

Sonnet 5.5 reemplaza a Haiku 4.5 solo si, en su corrida sobre el held-out, supera la media de las 3 corridas de Haiku por al menos 2 puntos en F1 macro de intención o en exactitud de monto, sin empeorar la tasa de fragmentos fieles, con latencia p95 bajo 5 s y con un costo por caso de hasta el doble del de Haiku.

## Resultado sobre el held-out (376 casos)

| Criterio | Haiku 4.5 (3 corridas) | Sonnet 5.5 (1 corrida) | ¿Cumple Sonnet? |
| --- | --- | --- | --- |
| F1 macro de intención | 0,987 | 1,000 | No: +1,3 puntos, menos de 2 |
| Exactitud de monto | 100,0% (124) | 100,0% (124) | No: 0 puntos |
| Fragmentos fieles | 99,9% (885) | 100,0% (848) | Sí, no empeora |
| Latencia p95 | 2.427 ms (pagada la primera vez, ver abajo) | 2.705 ms | Sí |
| Costo por caso | 0,00117 USD | 0,00285 USD (2,4 veces) | No: pasa el doble |

Otras métricas, sin peso en la regla: fecha 94,7% contra 88,2%; canal 88,5% contra 84,4%; posesión de tarjeta 100,0% contra 98,8%; idioma 62,5% contra 74,2%; casos en alcance enviados fuera de alcance 0,9% contra 0,0%.

## Decisión

Según la regla, Haiku 4.5 se mantiene como modelo de comprensión.

## Notas

- La latencia de Haiku en su reporte sobre la prueba aparece en 0 porque sus lecturas salieron de la caché del arnés (las pagaron las corridas de TRAZO). La latencia pagada la primera vez por esas 1.128 lecturas es p50 1.465 ms y p95 2.427 ms (declarado en `docs/reports/evaluacion.md`).
- Sonnet: 1 corrida en lugar de 3 por presupuesto (opción A del plan, aprobada el 2026-10-02).
- Gasto de la comparación: 1,0724 USD para Sonnet; Haiku sin gasto nuevo (caché del arnés).
