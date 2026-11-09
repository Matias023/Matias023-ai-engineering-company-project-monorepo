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

El 9 de noviembre de 2026 se realizó una demostración local del procesamiento RFP.

Ticket de demostración:

`f1b58601-8e3f-4ab8-a5c7-26fa83be4e5b`

Cliente:

Distribuciones Horizonte S.L.

Resultados verificados:

- Recepción del documento PDF.
- Conversión a Markdown.
- Clasificación mediante Gemini.
- Identificación de warehouse, lastmile y reverse.
- Análisis de los tres departamentos.
- Síntesis departamental.
- Visualización de resultados en el backoffice.
- Conexión SSE autenticada HTTP 200.
- Generación comercial completada para warehouse.

## 5. Limitaciones verificadas

Durante la generación de propuestas comerciales, Gemini devolvió errores HTTP 429 por superar el límite de solicitudes del nivel gratuito.

Estado registrado en PostgreSQL:

| Departamento | Generación | Revisión |
|---|---|---|
| warehouse | completed | needs_human_review |
| lastmile | failed | pendiente |
| reverse | failed | pendiente |

La generación comercial completa, las aprobaciones humanas y la consolidación definitiva no fueron verificadas de extremo a extremo en esta demostración.

Los borradores generados por IA deben revisarse antes de representar compromisos comerciales.

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

- Recuperación selectiva de generaciones fallidas.
- Control de cuota y solicitudes a Gemini.
- Validación completa de aprobaciones humanas.
- Comprobación de la consolidación final.
- Validación integral adicional.

## 9. Referencias internas

- `CONTEXT-trackflow.es.md`
- `README.es.md`
- `README.md`
- `audit/after/OWASP_A01_RFP_AFTER.md`

---

Documento de estado técnico basado en las evidencias disponibles al 9 de noviembre de 2026.
