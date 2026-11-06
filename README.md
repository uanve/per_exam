# Rumbo PER

Web digital para preparar el PER completo o la ampliación desde PNB.
El alumno busca una convocatoria por mes o título, contesta las preguntas
en la web y recibe su resultado con revisión de cada respuesta. No necesita
registrarse ni subir o abrir un PDF.

## Criterios de corrección

| Modalidad | Preguntas | Aciertos mínimos | Límites adicionales |
| --- | --- | --- | --- |
| PER completo | 45, UT1–11 | 32 | Balizamiento: 2 errores; RIPA: 5; carta: 2 |
| Ampliación desde PNB | 18, preguntas 28–45, UT7–11 | 15 | Carta: 2 errores |

Las preguntas en blanco cuentan como errores, también para los límites por
materia. La corrección se hace en el servidor; las soluciones no aparecen
en el HTML del examen antes de corregir. Es un modo de práctica sin
temporizador (el examen PER oficial dura como máximo 90 minutos).
Para carta de navegación se necesita la carta del Estrecho y el material
habitual, no incluidos en el PDF de ejemplo.

## Desarrollo local

Python 3.13:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export ADMIN_PASSWORD='tu-contraseña-larga-y-privada'
.venv/bin/flask --app wsgi run --host 127.0.0.1 --port 8080
```

Abre <http://localhost:8080>. Administración: <http://localhost:8080/admin>.
Sin `ADMIN_PASSWORD`, la web pública funciona pero la administración queda
desactivada. No hay contraseñas predeterminadas. Las variables se leen del
entorno: `.env.example` es una referencia, no se carga automáticamente.
En local, si falta `SECRET_KEY`, se genera una clave persistente en
`data/session.key`. No se debe subir `data/` ni `.env` al repositorio.

## Importar exámenes

1. Entra en administración con la contraseña del servidor.
2. Indica título y fecha de convocatoria y selecciona el PDF (hasta 15 MB).
3. La aplicación extrae y guarda en SQLite las preguntas, opciones, soluciones
   e ilustraciones. El PDF subido no se conserva ni se sirve a los alumnos.
4. Revisa el borrador y sus soluciones antes de publicarlo. Los borradores
   son privados y se pueden descartar.
5. Al publicar, la convocatoria aparece en el catálogo, en ambas modalidades.

El formato admitido es el del archivo incluido: primera página con 45
soluciones indicadas mediante **casillas de fondo rojo**, seguida de 45
preguntas numeradas `1.-` y cuatro opciones `a)`–`d)`, ordenadas según UT1–11.
Se conserva el texto bilingüe catalán/castellano y se extraen las imágenes
incrustadas. Se validan orden, cantidad de preguntas, opciones y soluciones.
Un PDF incompleto, escaneado, cifrado, con otra plantilla de soluciones o
sin texto extraíble se rechaza con un mensaje; no se adivinan respuestas.
Otros diseños, figuras vectoriales o cambios de maquetación pueden requerir
adaptar el importador. En administración se exige revisar el borrador antes de publicarlo.
No se ejecuta OCR ni se envían documentos a servicios externos.

## PDFs iniciales: importar al desplegar

Deja tus PDFs en la carpeta [`pdfs/`](./pdfs) y súbelos al repositorio.
En cada arranque, también después de un deploy en Railway, la aplicación
importa y **publica automáticamente** los PDFs nuevos. No necesitas usar el
panel de administración para estos archivos.

El nombre indica la fecha y el título que aparecerán en el catálogo:

```text
pdfs/
  2026-07__Julio 2026 - Modelo 3.pdf
  2026-11-06__Noviembre 2026 - Modelo 1.pdf
```

Usa `AAAA-MM__Titulo.pdf` si solo conoces el mes, o
`AAAA-MM-DD__Titulo.pdf` si conoces el día. Hay **dos guiones bajos**
entre la fecha y el título. Se admiten `.pdf` y `.PDF`, hasta 15 MB por archivo,
directamente dentro de la carpeta (no se recorren subcarpetas).
El PDF de ejemplo ya está colocado ahí con el mes de su portada.

Se aplica la misma validación de preguntas y soluciones que en administración.
Revisa los PDFs antes de subirlos: este flujo los publica directamente.
Si el nombre, fecha o contenido no son válidos, el arranque falla con el
nombre del archivo y el motivo en los logs de Railway; corrígelo y redespliega.
Los archivos importados correctamente antes del fallo ya quedan guardados.

Los duplicados se detectan por contenido (SHA-256), incluso si cambias el
nombre. Los siguientes despliegues no duplican ni modifican los exámenes
existentes y tampoco publican borradores ya existentes. Quitar un PDF del
repositorio no elimina su examen de la base de datos. No cambies el contenido
de un PDF para corregir sus metadatos: se trataría como un examen nuevo.
Las fechas de las importaciones desde administración siguen siendo las
que introduce el administrador.

## Railway desde GitHub

Este directorio es el repositorio `uanve/per_exam`; conecta ese repositorio
a un servicio de Railway. Si se usa un monorepo, establece este directorio
como Root Directory del servicio.

1. Sube los cambios al repositorio y conecta el servicio en Railway.
2. Railway construirá el `Dockerfile` y utilizará `railway.json`.
3. Añade un **volumen persistente montado en `/data`**. Sin él se perderán las
   importaciones al redesplegar. `DATA_DIR=/data` está definido en la imagen.
4. Configura estas variables en Railway:
   - `ADMIN_PASSWORD`: contraseña larga y única.
   - `SECRET_KEY`: secreto aleatorio estable. Puedes generarlo con
     `python3 -c "import secrets; print(secrets.token_hex(32))"`.
   - `TRUST_PROXY=1`: solo detrás del proxy de Railway.
5. Genera un dominio público HTTPS. El proceso escucha en el `PORT` que
   asigna Railway. `/health` comprueba que la base de datos responde.

En Railway se exige `SECRET_KEY` y las cookies son exclusivamente HTTPS.
El servidor usa un único worker sin hilos para serializar el importador PDF.
Mantén **una réplica** con su volumen SQLite; para varias réplicas habría que
migrar la persistencia a una base de datos compartida.
Las sesiones de administración duran una hora; se aplica CSRF y un máximo
de 10 intentos de acceso por dirección en una ventana de 15 minutos.

Haz copias del volumen. Para copiar SQLite en uso utiliza su API de backup
o detén el servicio: no copies solo el archivo principal ignorando su WAL.
Las respuestas de alumnos no se guardan en la base de datos: se conservan
temporalmente en su navegador hasta corregir. La web sigue funcionando sin
JavaScript, salvo el guardado local, el progreso y el botón de dejar en blanco.

## Pruebas

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Cubren el PDF real, sus 45 soluciones y 9 ilustraciones, umbrales de aprobado,
límites por materia, blancos, catálogo, administración, publicación,
persistencia, duplicados, validación de formularios e importación automática
de la carpeta inicial en bases de datos nuevas y existentes.

## Uso del material y dependencias

Publica únicamente PDFs que tengas derecho a reutilizar. PyMuPDF se distribuye
bajo AGPL/comercial: revisa sus condiciones y elige una licencia compatible
para la distribución de esta aplicación o una licencia comercial cuando
corresponda. Flask y Gunicorn tienen sus propias licencias.
