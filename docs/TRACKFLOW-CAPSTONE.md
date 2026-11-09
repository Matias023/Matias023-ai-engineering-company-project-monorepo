# TrackFlow — Capstone de Ingeniería de IA

## 1. Descripción

TrackFlow es una plataforma de operaciones logísticas desarrollada en el contexto del programa de Ingeniería de IA de 4Geeks Academy.

El proyecto integra una API, una interfaz administrativa, procesamiento de documentos, flujos asíncronos y funcionalidades de inteligencia artificial.

El dominio empresarial comprende almacenamiento, distribución de última milla y logística inversa.

## 2. Componentes tecnológicos

| Componente | Tecnología |
|---|---|
| Backend | Python, FastAPI |
| Frontend | React, Vite |
| Persistencia | PostgreSQL |
| Procesamiento asíncrono | Celery |
| Mensajería | Redis |
| Inteligencia artificial | Google Gemini |
| Notificaciones | Server-Sent Events (SSE) |
| Control de versiones | Git y GitHub |

## 3. Flujo RFP

El sistema implementa un flujo para procesar solicitudes de propuestas comerciales (RFP).

Etapas:

1. Recepción autenticada de documentos PDF.
2. Conversión del documento a Markdown.
3. Clasificación mediante inteligencia artificial.
4. Identificación de departamentos involucrados.
5. Análisis departamental de requisitos.
6. Síntesis de resultados.
7. Generación y evaluación de propuestas comerciales.
8. Revisión y aprobación humana.
9. Consolidación del documento final.

Las etapas enumeradas forman parte del flujo previsto. Su presencia en esta lista no implica que todas hayan superado una prueba integral.

## 4. Evidencia de ejecución

El 9 de noviembre de 2026 se realizó una demostración local del flujo RFP.

Ticket de demostración:

`f1b58601-8e3f-4ab8-a5c7-26fa83be4e5b`

Cliente: Distribuciones Horizonte S.L.

Resultados verificados:

- Recepción de PDF y conversión a Markdown.
- Clasificación mediante Gemini.
- Identificación y análisis de warehouse, lastmile y reverse.
- Síntesis de resultados departamentales.
- Visualización de resultados en el backoffice.
- Conexión SSE autenticada HTTP 200.
- Generación comercial completada para los tres departamentos.
- Recuperación selectiva de Lastmile y Reverse tras errores HTTP 429.
- Autenticación de tres cuentas departamentales de demostración.
- Matriz de autorización: 9/9 verificaciones correctas.
- Inicio de revisión humana de Warehouse, estado `pending`.
- Pruebas automatizadas seleccionadas: 53/53 PASS.
- Sintaxis Python: 5/5 PASS.
- Compilación TypeScript y Vite: PASS.
- `git diff --check`: PASS.

Los resultados corresponden a un entorno local de demostración.
No constituyen evidencia de aprobación comercial definitiva.

## 5. Limitaciones verificadas

Durante la generación inicial, Gemini devolvió errores HTTP 429.

Se implementó un mecanismo de reserva de solicitudes mediante Redis
para espaciar llamadas a Gemini, junto con un reintento técnico selectivo
para generaciones fallidas y su correspondiente control en el backoffice.

La recuperación posterior de Lastmile y Reverse fue verificada en PostgreSQL.

Estado final comprobado:

| Departamento | Generación | Aprobación |
|---|---|---|
| warehouse | completed | pending |
| lastmile | completed | no registrada |
| reverse | completed | no registrada |

El ticket permanece en `esperando_aprobación`.

La consulta de `verified_commercial_metrics` no mostró confirmaciones
para ninguno de los tres departamentos.

Para la consolidación final se requieren aprobaciones humanas y
métricas comerciales documentadas, incluyendo capacidad mensual,
volumen mensual y tiempos de devoluciones.

No se inventaron valores comerciales ni se registraron aprobaciones
para eludir las validaciones.

La consolidación definitiva y la generación del documento final
no fueron verificadas en este escenario.

El mecanismo de control de solicitudes fue probado en el escenario
local; no se afirma una garantía universal sobre cuotas externas.

## 6. Seguridad y buenas prácticas

El proyecto incorpora autenticación para acceder a funcionalidades protegidas y controles orientados a preservar las aprobaciones departamentales.

Los resultados generados requieren revisión humana.

No deben publicarse claves de API, credenciales de base de datos ni archivos `.env` con secretos.

## 7. Estructura principal

- `uis/backoffice/`: interfaz administrativa.
- `services/api/`: API FastAPI y tareas Celery.
- `data/pipelines/rfp_intake/`: procesamiento y evaluación RFP.
- `docs/`: documentación.
- `audit/`: evidencias de auditoría de seguridad.

## 8. Trabajo pendiente

- Obtener métricas comerciales verificables y sus referencias documentales.
- Completar las aprobaciones humanas autorizadas de los departamentos.
- Validar el arbitraje y la consolidación del documento final.
- Ejecutar las pruebas integrales adicionales que correspondan.
- Publicar los cambios recientes en el repositorio GitHub personal.
- Preparar y entregar el video de demostración.

La generación de los tres borradores y la recuperación selectiva
de Lastmile y Reverse sí fueron verificadas.

## 9. Referencias internas

- `CONTEXT-trackflow.es.md`
- `README.es.md`
- `README.md`
- `audit/after/OWASP_A01_RFP_AFTER.md`

---

Documento de estado técnico basado en las evidencias disponibles al 9 de noviembre de 2026.
