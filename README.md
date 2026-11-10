# SkillHub

Gestor local para las skills de tus repositorios de GitHub: catálogo con filtros, instalación y desinstalación, actualización por firma de contenido, revisión previa de archivos y vigilancia semanal en la nube.

Solo usa la biblioteca estándar de Python 3.9+. Interfaz con la paleta de Constructora Capital.

## Formas de llevarla a otro PC

| Forma | Qué necesita | Cómo |
|---|---|---|
| Archivo único `skillhub.pyz` | Python 3.9+ instalado | `py -3 skillhub.pyz` (Windows) o `python3 skillhub.pyz` |
| Carpeta completa | Python 3.9+ instalado | `iniciar.bat` / `iniciar.sh` |
| Ejecutable `.exe` | Nada instalado | Se genera en GitHub Actions con `compilar.yml` (ver abajo) |

Funciona para cualquier usuario del PC: no tiene rutas ni nombres fijos; guarda sus datos en la carpeta personal (`~/.skillhub`) y no necesita permisos de administrador.

**En un PC de trabajo**, antes de usarla:
- Revisa la política de TI: SkillHub descarga archivos de GitHub y los copia a tu carpeta personal. Si tu empresa restringe software o skills de terceros, pide autorización.
- Debe alcanzar `api.github.com`, `raw.githubusercontent.com` y `codeload.github.com` (y `fonts.googleapis.com` para la tipografía, opcional). Usa el proxy configurado en Windows o en las variables `HTTPS_PROXY`; no admite proxies con autenticación interactiva. Con inspección TLS, apunta `SSL_CERT_FILE` al certificado de la empresa si hace falta.
- Un `.exe` sin firma puede ser bloqueado por SmartScreen, un antivirus o AppLocker. Si pasa, usa el `.pyz` con el Python autorizado de la empresa.
- Ejecútala desde una carpeta de tu usuario (no desde una carpeta de red).
- Windows limita las rutas a 260 caracteres: si una skill falla por rutas largas, instala en un destino corto (Ajustes).

### Generar el .exe (GitHub Actions)

1. Sube esta carpeta a un repositorio tuyo.
2. Pestaña Actions → «Compilar ejecutables» → Run workflow (o sube una etiqueta `v1.0.1` para publicar una Release).
3. Descarga `skillhub-windows.exe` y su `.sha256` de los artefactos o de la Release. Cada compilación corre las pruebas y arranca el ejecutable antes de publicarlo.

## Inicio rápido

| Sistema | Cómo abrirlo |
|---|---|
| Windows | doble clic en `iniciar.bat` (o `py -3 skillhub.py` en una consola) |
| Linux / macOS | `./iniciar.sh` (o `python3 skillhub.py`) |

Se abre el navegador en `http://127.0.0.1:8765/?t=<token>`. El enlace lleva un token de sesión nuevo en cada arranque; no lo compartas. La primera vez se copia a la carpeta de datos `~/.skillhub` el catálogo semilla (390 skills de 8 repositorios) y las 99 fuentes (tus 97 stars y 2 repos propios). Si ya tenías una versión anterior, las fuentes que faltan se agregan solas al arrancar; las que quites no reaparecen (se cambia con la variable `SKILLHUB_HOME`).

**Antes de instalar por primera vez, pulsa «Sincronizar ahora»**: fija cada repositorio a su commit actual y verifica los archivos contra él.

## Qué hace cada sección

- **Panel:** conteos, última sincronización, avisos (cambios locales, choques de nombre, errores de fuentes) y «Actualizar todas».
- **Catálogo:** búsqueda y filtros por repositorio, categoría, alcance, estado de instalación y tu marca (Evaluar / Instalar / Descartar). Por skill: instalar, actualizar, desinstalar, revisar archivos, exportar `.zip`, tu categoría, tu traducción y tu nota. Selección múltiple para marcar, instalar, exportar o guardar un perfil.
- **Instaladas:** lo que SkillHub instaló y las carpetas externas del destino (que nunca toca).
- **Novedades:** skills nuevas, cambiadas o retiradas en el origen y actualizaciones pendientes.
- **Perfiles:** conjuntos para instalar de una vez. Los cinco «sugeridos» (BIM coordinación, costos y APU, obra y control, datos y conversión BIM, Revit API) los armé a partir de tu perfil; edítalos o crea los tuyos desde el Catálogo.
- **Fuentes:** todos los repositorios que SkillHub revisa, con filtros (con skills, sin skills, sin sincronizar, con error), buscador, licencia, actividad y estado. Puedes agregar uno con `dueño/repositorio` (se sincroniza solo) o **importar** las stars y repos de cualquier usuario de GitHub. En el Catálogo, el filtro «Repositorio» lista todas las fuentes, también las que no tienen skills.
- **Registro** y **Ajustes** (carpeta de destino).

## Cómo se mantiene actualizado

**Token de GitHub (recomendado).** Sin token, GitHub limita a 60 consultas por hora y cada fuente gasta 2 o 3: se sincronizan unas 20 por hora. Con token (5000 por hora) se revisan las 99 de una vez. En PowerShell, solo para esa sesión: `$env:GITHUB_TOKEN = gh auth token; py -3 skillhub.pyz`. Al agotarse el límite, la sincronización se detiene y deja el resto pendiente («Sincronizar las que faltan»).

Cada skill tiene una **firma**: SHA-1 de las líneas `ruta:sha-git` de sus archivos. `Sincronizar` consulta la API de GitHub (3 llamadas por repositorio; con `GITHUB_TOKEN` el límite sube) y compara firmas, sin descargar los repositorios. Si la API está limitada, usa el tarball de `codeload` solo en repositorios de tamaño conocido y de hasta 30 MB (con `sync --modo tarball` el tope es 150 MB; el repositorio de DDC pesa unos 330 MB y necesita la API).

Estados de una skill instalada: **Instalada** · **Actualización disponible** (la firma del origen cambió) · **Modificada localmente** (editaste archivos; actualizar los descarta, pero van a la papelera).

Para sincronizar solo cada día: ejecuta `python skillhub.py programar` y copia el comando que imprime (usa `schtasks` en Windows o `crontab`). SkillHub no crea tareas programadas por su cuenta.

## Vigilancia en la nube (GitHub Actions)

1. Crea un repositorio tuyo (por ejemplo `ccfc140830-cyber/skillhub`) y sube esta carpeta.
2. En Settings → Actions → General, permite que los flujos escriban en el repositorio.
3. El flujo `.github/workflows/vigilar-fuentes.yml` corre los lunes a las 07:00 (Colombia) o a mano desde la pestaña Actions: sincroniza, guarda `datos/catalog.json` y abre un issue si hay cambios o errores. Es solo un aviso: tu catálogo local se actualiza con «Sincronizar ahora».

## Seguridad

- El servidor escucha solo en `127.0.0.1`, exige el token en cada llamada, valida `Host` y `Origin` (contra DNS rebinding y peticiones de otras páginas) y limita el tamaño de las solicitudes.
- Cada archivo se descarga del commit fijado y se compara con su SHA de git. Si no coincide, no se instala.
- Se rechazan rutas con `..`, absolutas, con `\` o caracteres reservados de Windows, enlaces simbólicos y submódulos; límite de 300 archivos y 25 MB por skill.
- Nunca sobrescribe una carpeta que no instaló él. Lo reemplazado o desinstalado se mueve a `papelera/` dentro de la carpeta de datos; para recuperarlo, copia la carpeta de vuelta al destino.
- La **revisión previa** busca patrones (descarga y ejecución, procesos, red, borrado, lectura de claves, binarios, frases que intentan manipular al agente). Son señales, no un veredicto: con riesgo medio o alto exige tu confirmación, y no sustituye leer los archivos.
- `GITHUB_TOKEN` es opcional, solo se envía a `api.github.com` y no se escribe en registros ni archivos.

## Destino y claude.ai

El destino por defecto es `~/.claude/skills` (skills personales de Claude Code); cámbialo en Ajustes, por ejemplo a `.claude/skills` de un proyecto. claude.ai no lee carpetas locales: usa «Exportar .zip» (una carpeta por skill) y cárgalo manualmente desde los ajustes de capacidades.

## Pruebas

`python pruebas_skillhub.py` ejecuta 69 comprobaciones contra un GitHub simulado (sincronización, integridad por SHA, choques, actualización, papelera, rutas peligrosas, token, host y origen del servidor, fusión de la semilla, importación de stars y límite de la API). No necesita red.

## Límites conocidos

- No se probó en Windows ni en Python 3.9 (sí en Linux con Python 3.12). Los lanzadores `.bat` y `.sh` y el comando de `programar` están sin probar en sus sistemas.
- Los flujos de GitHub Actions (`vigilar-fuentes.yml` y `compilar.yml`) están validados como YAML, pero no se ejecutaron. El `.exe` de Windows no existe todavía: lo genera el segundo flujo. El mismo empaquetado con PyInstaller sí se probó en Linux (binario que arranca, sirve la interfaz y lee el catálogo).
- La sincronización por API se probó contra el simulador; contra la API real, el límite de este entorno impidió probarla. La vía por tarball y la instalación por `raw.githubusercontent.com` sí se probaron contra GitHub real.
- El uso con proxy corporativo, inspección TLS y PC con restricciones (AppLocker, antivirus) no se probó.
- La interfaz se probó en un navegador simulado (jsdom), no en pantalla.
- No mide la calidad de una skill ni si dos skills se contradicen; solo gestiona archivos.
- Las traducciones de las descripciones son resúmenes míos; las skills nuevas aparecen sin traducir.
