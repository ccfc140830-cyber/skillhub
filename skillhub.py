#!/usr/bin/env python3
"""SkillHub: gestor local de skills de GitHub para Claude.

Qué hace
  - Mantiene un catálogo de las skills (carpetas con SKILL.md) de los repos que tú eliges.
  - Detecta skills nuevas, cambiadas o retiradas comparando firmas de contenido (SHA de git).
  - Instala, actualiza y desinstala skills en una carpeta de destino (por defecto la de Claude Code),
    verificando cada archivo contra el SHA de git del repositorio y moviendo lo reemplazado a una papelera.
  - Revisa estáticamente los archivos antes de instalar (señales heurísticas, no un veredicto).
  - Exporta .zip para cargar skills manualmente en claude.ai.
  - Ofrece interfaz web local (solo 127.0.0.1, con token de sesión) y línea de comandos.

Solo usa la biblioteca estándar de Python 3.9+. Credencial opcional: variable de entorno GITHUB_TOKEN
(solo para consultas a la API; nunca se escribe en registros ni archivos).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import hmac
import http.server
import io
import json
import logging
import os
import posixpath
import random
import re
import secrets
import shutil
import socketserver
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

VERSION = "1.0.1"
# Carpeta de recursos: junto al script, dentro de un .pyz, o en el paquete de PyInstaller (sys._MEIPASS).
APP_DIR = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent)

HOME = Path(os.environ.get("SKILLHUB_HOME") or (Path.home() / ".skillhub"))
MARCA = ".skillhub.json"

_PRUEBA = os.environ.get("SKILLHUB_TEST") == "1"  # solo las pruebas pueden redirigir las URL de origen
API_BASE = os.environ.get("SKILLHUB_API_BASE", "https://api.github.com") if _PRUEBA else "https://api.github.com"
RAW_BASE = os.environ.get("SKILLHUB_RAW_BASE", "https://raw.githubusercontent.com") if _PRUEBA else "https://raw.githubusercontent.com"
CODELOAD_BASE = os.environ.get("SKILLHUB_CODELOAD_BASE", "https://codeload.github.com") if _PRUEBA else "https://codeload.github.com"

MAX_ARCHIVOS = int(os.environ.get("SKILLHUB_MAX_ARCHIVOS", "300"))
MAX_BYTES_SKILL = int(os.environ.get("SKILLHUB_MAX_BYTES_SKILL", str(25 * 1024 * 1024)))
MAX_BYTES_ARCHIVO = 10 * 1024 * 1024
MAX_TARBALL = int(os.environ.get("SKILLHUB_MAX_TARBALL", str(150 * 1024 * 1024)))

LOCK = threading.RLock()  # serializa toda mutación de catálogo, destino y papelera
log = logging.getLogger("skillhub")


# ───────────────────────── Errores tipados ─────────────────────────
class ErrorSkillHub(Exception):
    """Error esperado, con mensaje apto para mostrar al usuario."""


class ErrorRed(ErrorSkillHub):
    pass


class LimiteTasa(ErrorSkillHub):
    pass


class Truncado(ErrorSkillHub):
    pass


class Choque(ErrorSkillHub):
    pass


class RequiereConfirmacion(ErrorSkillHub):
    pass


class ErrorIntegridad(ErrorSkillHub):
    pass


# ───────────────────────── Utilidades de archivos y datos ─────────────────────────
def leer_recurso(rel: str) -> bytes:
    """Lee un recurso empaquetado (ui.html, seed/…) tanto desde carpeta como desde un .pyz."""
    try:
        return (APP_DIR / rel).read_bytes()
    except OSError:
        cargador = globals().get("__loader__")
        if cargador is not None and hasattr(cargador, "get_data"):
            try:
                return cargador.get_data(str(APP_DIR / rel))
            except OSError:
                pass
        raise ErrorSkillHub(f"Falta el recurso {rel}; reinstala SkillHub desde el paquete original.")


def ahora() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def escribir_json_atomico(ruta: Path, datos) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=ruta.name + ".", suffix=".tmp", dir=str(ruta.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=1)
        os.replace(tmp, ruta)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def leer_json(ruta: Path, defecto):
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return defecto
    except json.JSONDecodeError as e:
        raise ErrorSkillHub(f"El archivo {ruta} no es JSON válido ({e}). Corrígelo o bórralo para restaurar la semilla.")


def registrar(evento: str, **datos) -> None:
    """Registro estructurado (JSON por línea). Nunca recibe credenciales."""
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        linea = json.dumps({"ts": ahora(), "evento": evento, **datos}, ensure_ascii=False)
        with open(HOME / "registro.log", "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except OSError as e:
        log.warning("no se pudo escribir el registro: %s", e)


def git_blob_sha(datos: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(datos) + datos).hexdigest()


def firma_entradas(entradas) -> str:
    """Firma de contenido de una skill: SHA-1 de las líneas 'ruta:shagit' ordenadas por ruta."""
    return hashlib.sha1("\n".join(f"{r}:{s}" for r, s in sorted((e[0], e[1]) for e in entradas)).encode()).hexdigest()


_RESERVADOS_WIN = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def nombre_carpeta_seguro(nombre: str) -> str:
    n = re.sub(r"[^A-Za-z0-9._-]+", "-", nombre or "").strip(".-")
    if not n or n.lower().split(".")[0] in _RESERVADOS_WIN or len(n) > 100:
        raise ErrorSkillHub(f"El nombre de skill «{nombre}» no sirve como nombre de carpeta.")
    return n


def ruta_relativa_segura(rel: str) -> str:
    """Rechaza rutas que puedan escapar de la carpeta de destino (también en Windows)."""
    if not rel or rel.startswith("/") or "\\" in rel or len(rel) > 240:
        raise ErrorIntegridad(f"Ruta no permitida en el origen: {rel!r}")
    for parte in rel.split("/"):
        if parte in ("", ".", "..") or re.search(r'[\x00-\x1f<>:"|?*]', parte) or parte.endswith((" ", ".")):
            raise ErrorIntegridad(f"Ruta no permitida en el origen: {rel!r}")
    return rel


# ───────────────────────── Lectura de SKILL.md ─────────────────────────
def leer_frontmatter(texto: str) -> dict:
    """Lee name y description del bloque YAML inicial (subconjunto suficiente para SKILL.md)."""
    m = re.match(r"\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", texto, re.S)
    if not m:
        return {}
    lineas = m.group(1).splitlines()
    out: dict[str, str] = {}
    i = 0
    while i < len(lineas):
        km = re.match(r"^([A-Za-z0-9_-]+):[ \t]*(.*)$", lineas[i])
        i += 1
        if not km:
            continue
        clave, valor = km.group(1), km.group(2).strip()
        if valor in (">", "|", ">-", "|-", ">+", "|+", ""):
            bloque = []
            while i < len(lineas) and (lineas[i][:1] in (" ", "\t") or not lineas[i].strip()):
                bloque.append(lineas[i].strip())
                i += 1
            valor = " ".join(x for x in bloque if x)
        else:
            while i < len(lineas) and lineas[i][:1] in (" ", "\t"):
                valor += " " + lineas[i].strip()
                i += 1
            if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
                comilla = valor[0]
                valor = valor[1:-1]
                if comilla == '"':
                    valor = valor.replace('\\"', '"')
        out[clave] = re.sub(r"\s+", " ", valor).strip()
    return out


# ───────────────────────── Red ─────────────────────────
def _cab(d) -> dict:
    return {k.lower(): v for k, v in dict(d).items()}


def http_get(url: str, headers: dict | None = None, timeout: int = 30, max_bytes: int = 50_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": f"skillhub/{VERSION}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            largo = r.headers.get("Content-Length")
            if largo and largo.isdigit() and int(largo) > max_bytes:
                raise ErrorRed(f"La respuesta supera el límite de {max_bytes} bytes")
            datos = r.read(max_bytes + 1)
            if len(datos) > max_bytes:
                raise ErrorRed(f"La respuesta supera el límite de {max_bytes} bytes")
            return r.status, _cab(r.headers), datos
    except urllib.error.HTTPError as e:
        return e.code, _cab(e.headers), e.read(20000)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ErrorRed(f"Sin conexión con {urllib.parse.urlsplit(url).netloc}: {e}")


def get_con_reintento(url: str, headers=None, intentos: int = 4, **kw):
    """Reintenta solo fallos transitorios (red, 5xx) con retroceso exponencial y jitter."""
    espera = 0.6
    for n in range(1, intentos + 1):
        try:
            st, cab, datos = http_get(url, headers, **kw)
            if st < 500:
                return st, cab, datos
            ultimo: Exception = ErrorRed(f"HTTP {st}")
        except ErrorRed as e:
            if "supera el límite" in str(e):
                raise
            ultimo = e
        if n == intentos:
            raise ultimo
        time.sleep(espera + random.uniform(0, espera / 2))
        espera *= 2
    raise ErrorRed("agotados los intentos")


def api_json(ruta: str):
    cab = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        cab["Authorization"] = "Bearer " + tok
    st, h, datos = get_con_reintento(API_BASE + ruta, cab)
    if st in (403, 429) and h.get("x-ratelimit-remaining") == "0":
        reset = h.get("x-ratelimit-reset", "")
        cuando = dt.datetime.fromtimestamp(int(reset), dt.timezone.utc).strftime("%H:%M UTC") if reset.isdigit() else "más tarde"
        raise LimiteTasa(f"Límite de la API de GitHub agotado (se restablece hacia las {cuando}). Define GITHUB_TOKEN para ampliarlo.")
    if st == 404:
        raise ErrorSkillHub(f"No encontrado en GitHub: {ruta.split('?')[0]} (¿repositorio privado, renombrado o ref inexistente?)")
    if st != 200:
        raise ErrorSkillHub(f"GitHub respondió {st} en {ruta.split('?')[0]}")
    try:
        return json.loads(datos)
    except json.JSONDecodeError as e:
        raise ErrorSkillHub(f"Respuesta de GitHub ilegible: {e}")


def validar_repo(repo: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo or ""):
        raise ErrorSkillHub("El repositorio debe tener la forma «dueño/repositorio».")
    return repo


def validar_ref(ref: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_./-]{1,100}", ref or "") or ".." in ref:
        raise ErrorSkillHub("Referencia (rama, etiqueta o commit) no válida.")
    return ref


# ───────────────────────── Origen: listado de archivos de un repo ─────────────────────────
class Listado:
    def __init__(self, commit: str, archivos: dict, irregulares: list, meta: dict, contenidos: dict | None = None, origen: str = "api"):
        self.origen = origen
        self.commit = commit
        self.archivos = archivos  # ruta -> (sha, tamaño)
        self.irregulares = irregulares
        self.meta = meta
        self.contenidos = contenidos or {}  # ruta SKILL.md -> bytes (solo modo tarball)


def listar_api(repo: str, ref: str) -> Listado:
    meta_api = api_json(f"/repos/{repo}")
    commit = api_json(f"/repos/{repo}/commits/{urllib.parse.quote(ref, safe='/')}")["sha"]
    arbol = api_json(f"/repos/{repo}/git/trees/{commit}?recursive=1")
    if arbol.get("truncated"):
        raise Truncado("El árbol del repositorio es demasiado grande para la API")
    archivos, irreg = {}, []
    for e in arbol.get("tree", []):
        if e.get("type") == "blob" and e.get("mode") in ("100644", "100755"):
            archivos[e["path"]] = (e["sha"], int(e.get("size", 0)))
        elif e.get("type") in ("blob", "commit"):
            irreg.append(e["path"])
    lic = (meta_api.get("license") or {}).get("spdx_id")
    meta = {"licencia": lic or "sin LICENSE", "archivada": bool(meta_api.get("archived")), "pushed_at": meta_api.get("pushed_at")}
    return Listado(commit, archivos, irreg, meta)


def listar_tarball(repo: str, ref: str) -> Listado:
    st, _, datos = get_con_reintento(f"{CODELOAD_BASE}/{repo}/tar.gz/{urllib.parse.quote(ref, safe='/')}", timeout=120, max_bytes=MAX_TARBALL)
    if st != 200:
        raise ErrorSkillHub(f"No se pudo descargar {repo} ({st}); si pesa más de {MAX_TARBALL // 1048576} MB usa GITHUB_TOKEN para listar por API")
    archivos, irreg, contenidos = {}, [], {}
    with tarfile.open(fileobj=io.BytesIO(datos), mode="r:gz") as t:
        for m in t:
            partes = m.name.split("/", 1)
            if len(partes) < 2 or not partes[1]:
                continue
            if m.isfile():
                b = t.extractfile(m).read()
                archivos[partes[1]] = (git_blob_sha(b), len(b))
                if partes[1] == "SKILL.md" or partes[1].endswith("/SKILL.md"):
                    contenidos[partes[1]] = b
            elif m.issym() or m.islnk():
                irreg.append(partes[1])
    return Listado(ref, archivos, irreg, {"licencia": None, "archivada": False, "pushed_at": None}, contenidos, "tarball")


def agrupar_skills(archivos: dict, irregulares: list) -> dict:
    """Devuelve {dir: {'entradas': [[rel, sha, tamaño]], 'irregulares': n}} excluyendo skills anidadas."""
    dirs = sorted({("" if p == "SKILL.md" else p[: -len("/SKILL.md")]) for p in archivos if p == "SKILL.md" or p.endswith("/SKILL.md")})
    res = {}
    for d in dirs:
        pref = d + "/" if d else ""
        hijos = [o for o in dirs if o and o != d and (o.startswith(pref) if d else True)]
        ents = []
        for p, (sha, tam) in archivos.items():
            if p.startswith(pref) and not any(p.startswith(o + "/") for o in hijos):
                ents.append([p[len(pref):], sha, tam])
        irr = sum(1 for p in irregulares if p.startswith(pref) and not any(p.startswith(o + "/") for o in hijos))
        res[d or "."] = {"entradas": sorted(ents), "irregulares": irr}
    return res


def leer_skill_md(repo: str, commit: str, ruta: str) -> bytes:
    url = f"{RAW_BASE}/{repo}/{commit}/{urllib.parse.quote(ruta)}"
    st, _, datos = get_con_reintento(url, timeout=30, max_bytes=MAX_BYTES_ARCHIVO)
    if st != 200:
        raise ErrorSkillHub(f"No se pudo leer {ruta} ({st})")
    return datos


# ───────────────────────── Catálogo, fuentes, usuario ─────────────────────────
def iniciar_datos() -> None:
    """Copia la semilla a HOME la primera vez; no sobrescribe nada existente."""
    HOME.mkdir(parents=True, exist_ok=True)
    for nombre in ("catalog.json", "fuentes.json", "perfiles.json"):
        destino = HOME / nombre
        if not destino.exists():
            destino.write_bytes(leer_recurso(f"seed/{nombre}"))


def cargar_catalogo() -> dict:
    return leer_json(HOME / "catalog.json", {"version": 1, "fuentes": {}, "skills": []})


def guardar_catalogo(cat: dict) -> None:
    cat["generado"] = ahora()
    escribir_json_atomico(HOME / "catalog.json", cat)


def cargar_fuentes() -> list:
    return leer_json(HOME / "fuentes.json", [])


def cargar_usuario() -> dict:
    u = leer_json(HOME / "usuario.json", {})
    u.setdefault("skills", {})
    return u


def cargar_config() -> dict:
    c = leer_json(HOME / "config.json", {})
    c.setdefault("destino", str(Path.home() / ".claude" / "skills"))
    return c


CATEGORIAS_PALABRAS = [
    (r"cwicr|cost|estimat|budget|price|unit-price|qto|takeoff|boq", "Costos y presupuestos"),
    (r"clash|bim|ifc|rvt|revit|ids-|cad|dwg|dgn|aps|acc", "Análisis y validación BIM"),
    (r"schedule|gantt|4d|critical|look-ahead|delay", "Planificación y cronograma"),
    (r"safety|quality|punch|inspection|incident|closeout|daily|field", "Obra, calidad y cierre"),
    (r"data|etl|csv|json|sql|pandas|parquet|vector", "Datos, ETL y estándares"),
    (r"contract|legal|document|rfi|submittal|minutes", "Documentos y contratos"),
]


def categoria_inicial(nombre: str, desc: str) -> str:
    t = f"{nombre} {desc}".lower()
    for pat, cat in CATEGORIAS_PALABRAS:
        if re.search(pat, t):
            return cat
    return "Sin categoría"


def marcar_duplicados(skills: list) -> None:
    visto = set()
    for s in sorted(skills, key=lambda x: (x["repo"], x["dir"])):
        k = (s["repo"], s["name"])
        s["dup"] = k in visto
        visto.add(k)


# ───────────────────────── Sincronización ─────────────────────────
def sincronizar_fuente(cat: dict, fuente: dict, modo: str = "auto") -> dict:
    repo, ref = validar_repo(fuente["repo"]), validar_ref(fuente.get("ref", "HEAD"))
    listado = None
    if modo in ("auto", "api"):
        try:
            listado = listar_api(repo, ref)
        except (LimiteTasa, Truncado) as e:
            if modo == "api":
                raise
            log.info("API no disponible para %s (%s); se usa el tarball", repo, e)
    if listado is None:
        listado = listar_tarball(repo, ref)
    grupos = agrupar_skills(listado.archivos, listado.irregulares)
    previos = {s["dir"]: s for s in cat["skills"] if s["repo"] == repo}
    resumen = {"nuevas": [], "modificadas": [], "retiradas": [], "sin_cambio": 0, "errores": []}

    pendientes = []  # skills cuyo SKILL.md hay que leer (nuevas o con firma distinta)
    for d, g in grupos.items():
        sig = firma_entradas(g["entradas"])
        s = previos.get(d)
        if s is None or s["sig"] != sig or not s.get("name"):
            pendientes.append((d, g, sig, s))
        else:
            s["origen"] = "activa"
            s["irregulares"] = g["irregulares"]
            resumen["sin_cambio"] += 1

    def contenido(d: str) -> bytes:
        ruta = "SKILL.md" if d == "." else f"{d}/SKILL.md"
        if ruta in listado.contenidos:
            return listado.contenidos[ruta]
        return leer_skill_md(repo, listado.commit, ruta)

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futuros = {ex.submit(contenido, d): (d, g, sig, s) for d, g, sig, s in pendientes}
        for fut in cf.as_completed(futuros):
            d, g, sig, s = futuros[fut]
            try:
                fm = leer_frontmatter(fut.result().decode("utf-8", "replace"))
            except ErrorSkillHub as e:
                resumen["errores"].append({"dir": d, "error": str(e)})
                continue
            nombre = fm.get("name") or (d.rsplit("/", 1)[-1] if d != "." else repo.split("/")[1])
            desc = fm.get("description", "")
            if s is None:
                s = {"id": f"{repo}|{d}", "repo": repo, "dir": d, "name": nombre, "en": desc, "es": "",
                     "cat": categoria_inicial(nombre, desc), "alc": fuente.get("alcance", "soporte"), "dup": False}
                cat["skills"].append(s)
                s["cambio"] = "nueva"
                resumen["nuevas"].append(s["id"])
            else:
                s["cambio"] = "modificada"
                resumen["modificadas"].append(s["id"])
                if desc and desc != s.get("en"):
                    s["es"] = ""  # la traducción quedó desfasada respecto del texto original
                s["name"], s["en"] = nombre, desc or s.get("en", "")
            s.update({"sig": sig, "files": g["entradas"], "archivos": len(g["entradas"]),
                      "bytes": sum(e[2] for e in g["entradas"]), "irregulares": g["irregulares"], "origen": "activa"})
            s["cambio_ts"] = ahora()
    for d, s in previos.items():
        if d not in grupos and s.get("origen") != "retirada":
            s["origen"], s["cambio"], s["cambio_ts"] = "retirada", "retirada", ahora()
            resumen["retiradas"].append(s["id"])
    marcar_duplicados(cat["skills"])
    estado = cat["fuentes"].setdefault(repo, {})
    estado.update({"commit": listado.commit, "sincronizado": ahora(), "estado": "ok", "error": None,
                   "skills": len(grupos), "modo": listado.origen})
    if listado.meta.get("licencia"):
        estado["licencia"] = listado.meta["licencia"]
    if listado.meta.get("pushed_at"):
        estado["pushed_at"] = listado.meta["pushed_at"]
    estado["archivada"] = listado.meta.get("archivada", False)
    return resumen


def sincronizar(repos: list | None = None, modo: str = "auto", progreso=None) -> dict:
    with LOCK:
        cat = cargar_catalogo()
        fuentes = [f for f in cargar_fuentes() if not repos or f["repo"] in repos]
        total = {"fuentes": {}, "nuevas": 0, "modificadas": 0, "retiradas": 0}
        for n, f in enumerate(fuentes):
            if progreso:
                progreso(n, len(fuentes), f"Sincronizando {f['repo']}")
            try:
                r = sincronizar_fuente(cat, f, modo)
            except ErrorSkillHub as e:
                cat["fuentes"].setdefault(f["repo"], {}).update({"estado": "error", "error": str(e), "sincronizado": ahora()})
                total["fuentes"][f["repo"]] = {"error": str(e)}
                registrar("sync_error", repo=f["repo"], error=str(e))
                continue
            total["fuentes"][f["repo"]] = r
            for k in ("nuevas", "modificadas", "retiradas"):
                total[k] += len(r[k])
            registrar("sync", repo=f["repo"], nuevas=len(r["nuevas"]), modificadas=len(r["modificadas"]), retiradas=len(r["retiradas"]))
        guardar_catalogo(cat)
        if progreso:
            progreso(len(fuentes), len(fuentes), "Sincronización terminada")
        return total


# ───────────────────────── Destino e instaladas ─────────────────────────
def firma_disco(carpeta: Path) -> str:
    entradas = []
    for p in sorted(carpeta.rglob("*")):
        if p.is_file() and not p.is_symlink() and p.name != MARCA:
            entradas.append((p.relative_to(carpeta).as_posix(), git_blob_sha(p.read_bytes())))
    return firma_entradas(entradas)


def escanear_destino(destino: Path) -> dict:
    res = {}
    if not destino.is_dir():
        return res
    for hijo in sorted(destino.iterdir()):
        if hijo.name.startswith(".") or hijo.is_symlink() or not hijo.is_dir() or not (hijo / "SKILL.md").is_file():
            continue
        marca = None
        try:
            marca = json.loads((hijo / MARCA).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        res[hijo.name] = {"carpeta": hijo.name, "marca": marca, "gestionada": bool(marca and marca.get("id")),
                          "firma_disco": firma_disco(hijo)}
    return res


def estado_skills(cat: dict, destino: Path) -> dict:
    """id -> {estado, carpeta, choque}. estado: no_instalada|instalada|actualizable|modificada."""
    esc = escanear_destino(destino)
    por_id = {v["marca"]["id"]: v for v in esc.values() if v["gestionada"]}
    res = {}
    for s in cat["skills"]:
        try:
            carpeta = nombre_carpeta_seguro(s["name"])
        except ErrorSkillHub:
            carpeta = None
        inst = por_id.get(s["id"])
        choque = None
        if inst is None and carpeta and carpeta in esc:
            otro = esc[carpeta]
            choque = (otro["marca"] or {}).get("id") or "externa"
        if inst:
            modificada = inst["firma_disco"] != inst["marca"].get("sig")
            desactualizada = inst["marca"].get("sig") != s.get("sig")
            est = "modificada" if modificada else ("actualizable" if desactualizada else "instalada")
            res[s["id"]] = {"estado": est, "carpeta": inst["carpeta"], "choque": None, "desactualizada": desactualizada}
        else:
            res[s["id"]] = {"estado": "no_instalada", "carpeta": carpeta, "choque": choque, "desactualizada": False}
    return res


# ───────────────────────── Preparación verificada y revisión ─────────────────────────
def buscar_skill(cat: dict, ident: str) -> dict:
    for s in cat["skills"]:
        if s["id"] == ident:
            return s
    raise ErrorSkillHub(f"No existe la skill {ident!r} en el catálogo.")


def preparar(cat: dict, s: dict) -> Path:
    """Descarga los archivos de la skill al área de preparación y los verifica contra el SHA de git."""
    if s.get("origen") == "retirada":
        raise ErrorSkillHub("Esta skill fue retirada del repositorio de origen; no se puede instalar.")
    if s.get("irregulares"):
        raise ErrorSkillHub("La skill contiene enlaces simbólicos o submódulos; por seguridad no se instala.")
    files = s.get("files") or []
    if not files:
        raise ErrorSkillHub("El catálogo no tiene la lista de archivos de esta skill; sincroniza primero.")
    if len(files) > MAX_ARCHIVOS or sum(f[2] for f in files) > MAX_BYTES_SKILL or any(f[2] > MAX_BYTES_ARCHIVO for f in files):
        raise ErrorSkillHub(f"La skill supera los límites de seguridad ({MAX_ARCHIVOS} archivos, {MAX_BYTES_SKILL // 1048576} MB).")
    base = HOME / "p" / hashlib.sha1(s["id"].encode()).hexdigest()[:10] / s["sig"][:12]
    if (base / ".completa").exists():
        return base
    commit = cat["fuentes"].get(s["repo"], {}).get("commit") or "HEAD"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    prefijo = "" if s["dir"] == "." else s["dir"] + "/"
    try:
        for rel, sha, tam in files:
            ruta_relativa_segura(rel)
            st, _, datos = get_con_reintento(f"{RAW_BASE}/{s['repo']}/{commit}/{urllib.parse.quote(prefijo + rel)}", timeout=60, max_bytes=MAX_BYTES_ARCHIVO)
            if st != 200:
                raise ErrorSkillHub(f"No se pudo descargar {prefijo + rel} ({st}). Si el repositorio cambió, sincroniza de nuevo.")
            if git_blob_sha(datos) != sha:
                raise ErrorIntegridad(f"El archivo {rel} no coincide con el SHA registrado: el origen cambió o la descarga está alterada. Sincroniza y reintenta.")
            destino = base / rel
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(datos)
        (base / ".completa").write_text(ahora(), encoding="utf-8")
    except BaseException:
        shutil.rmtree(base, ignore_errors=True)
        raise
    return base


REGLAS = [
    ("alto", "Descarga y ejecuta código (curl/wget a shell)", r"(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b"),
    ("alto", "Ejecución de texto como comando (iex / Invoke-Expression)", r"\b(Invoke-Expression|iex)\b\s*[\(\$]"),
    ("alto", "Decodifica base64 para ejecutar", r"base64\s+(-d|--decode)|b64decode\([^)]*\)\s*\)?\s*(exec|eval)|eval\(\s*atob"),
    ("alto", "Borrado recursivo de rutas amplias", r"rm\s+-rf?\s+(/|~|\$HOME|\*)|Remove-Item[^\n]*-Recurse[^\n]*(\\\*|C:\\)"),
    ("alto", "Texto que intenta manipular al agente", r"ignore (all |any )?(the )?(previous|prior|above) instructions|do not (tell|inform|mention)[^\n]{0,30}user|no le (digas|informes)[^\n]{0,30}usuario"),
    ("alto", "Acceso a llaves o credenciales del sistema", r"\.ssh[/\\]|id_rsa|\.aws[/\\]credentials|\.npmrc|\.git-credentials|Login Data"),
    ("medio", "Ejecuta procesos del sistema", r"\b(subprocess|os\.system|os\.popen|child_process|Runtime\.getRuntime|Start-Process|ShellExecute)\b"),
    ("medio", "Evalúa código dinámico", r"\b(eval|exec)\s*\("),
    ("medio", "Hace solicitudes de red", r"\b(requests\.(get|post|put)|urllib\.request|http\.client|fetch\(|axios|XMLHttpRequest|socket\.socket|Invoke-WebRequest|Invoke-RestMethod|curl\s|wget\s)"),
    ("medio", "Borra o modifica archivos", r"\b(shutil\.rmtree|os\.remove|os\.unlink|unlink\(|Remove-Item|fs\.rm(Sync)?|rmdir)\b"),
    ("medio", "Lee variables de entorno o claves", r"\b(os\.environ|process\.env|getenv)\b|API[_-]?KEY|SECRET|PASSWORD"),
    ("medio", "Instala dependencias", r"\b(pip3?\s+install|npm\s+(i|install)|npx\s|uvx\s|yarn\s+add|dotnet\s+add\s+package|winget\s+install|choco\s+install)\b"),
]
_REGLAS_C = [(n, t, re.compile(p, re.I)) for n, t, p in REGLAS]
EXT_BINARIA = {".exe", ".dll", ".so", ".dylib", ".msi", ".jar", ".scr", ".com", ".bin", ".pyd"}
EXT_SCRIPT = {".sh", ".bat", ".cmd", ".ps1", ".vbs", ".py", ".js", ".ts", ".mjs", ".rb", ".pl"}


def revisar(base: Path) -> dict:
    hallazgos, scripts, binarios, n_arch, n_bytes = [], [], [], 0, 0
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.name == ".completa":
            continue
        rel = p.relative_to(base).as_posix()
        datos = p.read_bytes()
        n_arch += 1
        n_bytes += len(datos)
        ext = p.suffix.lower()
        if ext in EXT_BINARIA or datos[:2] == b"MZ" or datos[:4] == b"\x7fELF":
            binarios.append(rel)
            hallazgos.append({"nivel": "alto", "regla": "Incluye un archivo ejecutable o binario", "archivo": rel, "linea": 0, "texto": ""})
            continue
        if ext in EXT_SCRIPT:
            scripts.append(rel)
        if b"\x00" in datos[:4096]:
            continue
        for n, linea in enumerate(datos.decode("utf-8", "replace").splitlines(), 1):
            if len(linea) > 2000:
                linea = linea[:2000]
            for nivel, texto, rx in _REGLAS_C:
                if rx.search(linea):
                    hallazgos.append({"nivel": nivel, "regla": texto, "archivo": rel, "linea": n, "texto": linea.strip()[:160]})
                    break
    orden = {"alto": 2, "medio": 1}
    nivel = "bajo"
    for h in hallazgos:
        if orden.get(h["nivel"], 0) > orden.get(nivel, 0):
            nivel = h["nivel"]
    hallazgos.sort(key=lambda h: (-orden.get(h["nivel"], 0), h["archivo"], h["linea"]))
    return {"nivel": nivel, "total_hallazgos": len(hallazgos), "hallazgos": hallazgos[:60], "scripts": scripts, "binarios": binarios,
            "archivos": n_arch, "bytes": n_bytes,
            "aviso": "Señales automáticas por patrones: no sustituyen leer los archivos antes de instalar."}


def revisar_skill(ident: str) -> dict:
    with LOCK:
        cat = cargar_catalogo()
        s = buscar_skill(cat, ident)
        return {"id": ident, "name": s["name"], **revisar(preparar(cat, s))}


# ───────────────────────── Instalar, actualizar, desinstalar ─────────────────────────
def mover_a_papelera(carpeta: Path, motivo: str) -> Path:
    destino = HOME / "papelera" / f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(2)}-{carpeta.name}"
    destino.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(carpeta), str(destino))
    registrar("papelera", carpeta=carpeta.name, hacia=destino.name, motivo=motivo)
    return destino


def instalar(ident: str, destino: Path, confirmadas: set | None = None, forzar_modificada: bool = False) -> dict:
    confirmadas = confirmadas or set()
    with LOCK:
        cat = cargar_catalogo()
        s = buscar_skill(cat, ident)
        carpeta = nombre_carpeta_seguro(s["name"])
        base = preparar(cat, s)
        rev = revisar(base)
        if rev["nivel"] in ("medio", "alto") and ident not in confirmadas:
            raise RequiereConfirmacion(f"La revisión de «{s['name']}» marcó riesgo {rev['nivel']}; confírmala antes de instalar.")
        destino.mkdir(parents=True, exist_ok=True)
        final = destino / carpeta
        previa = None
        if final.exists():
            try:
                marca = json.loads((final / MARCA).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                marca = None
            if not marca or marca.get("id") != s["id"]:
                raise Choque(f"Ya existe una carpeta «{carpeta}» en el destino que no fue instalada por SkillHub desde esta skill ({(marca or {}).get('id', 'externa')}). No se sobrescribe.")
            if firma_disco(final) != marca.get("sig") and not forzar_modificada:
                raise RequiereConfirmacion(f"«{carpeta}» tiene cambios locales; actualizar los descartaría (quedarían en la papelera). Confirma para continuar.")
            previa = final
        tmp = destino / f".skillhub-tmp-{secrets.token_hex(4)}"
        try:
            shutil.copytree(base, tmp, ignore=shutil.ignore_patterns(".completa"))
            (tmp / MARCA).write_text(json.dumps({"id": s["id"], "repo": s["repo"], "dir": s["dir"], "name": s["name"], "sig": s["sig"],
                                                  "commit": cat["fuentes"].get(s["repo"], {}).get("commit"), "instalada": ahora(),
                                                  "skillhub": VERSION}, ensure_ascii=False, indent=1), encoding="utf-8")
            respaldo = mover_a_papelera(previa, "actualización") if previa else None
            try:
                os.replace(tmp, final)
            except OSError:
                if respaldo:
                    shutil.move(str(respaldo), str(final))
                raise
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
        registrar("instalar", id=s["id"], carpeta=carpeta, sig=s["sig"], riesgo=rev["nivel"], actualizacion=bool(previa))
        return {"id": s["id"], "carpeta": carpeta, "riesgo": rev["nivel"], "actualizacion": bool(previa)}


def desinstalar(ident: str, destino: Path) -> dict:
    with LOCK:
        cat = cargar_catalogo()
        s = buscar_skill(cat, ident)
        est = estado_skills(cat, destino)[s["id"]]
        if est["estado"] == "no_instalada":
            raise ErrorSkillHub(f"«{s['name']}» no está instalada por SkillHub en el destino.")
        carpeta = destino / est["carpeta"]
        mover_a_papelera(carpeta, "desinstalación")
        registrar("desinstalar", id=s["id"], carpeta=est["carpeta"])
        return {"id": s["id"], "carpeta": est["carpeta"], "papelera": True}


def exportar_zip(ids: list) -> bytes:
    """Zip con una carpeta por skill, listo para cargar manualmente en claude.ai."""
    with LOCK:
        cat = cargar_catalogo()
        buf = io.BytesIO()
        usados = set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for ident in ids:
                s = buscar_skill(cat, ident)
                carpeta = nombre_carpeta_seguro(s["name"])
                if carpeta in usados:
                    raise Choque(f"Dos skills seleccionadas usan el nombre «{carpeta}».")
                usados.add(carpeta)
                base = preparar(cat, s)
                for p in sorted(base.rglob("*")):
                    if p.is_file() and p.name != ".completa":
                        z.write(p, f"{carpeta}/{p.relative_to(base).as_posix()}")
        return buf.getvalue()


# ───────────────────────── Tareas en segundo plano ─────────────────────────
class Tarea:
    def __init__(self, tipo: str):
        self.id = secrets.token_hex(6)
        self.tipo, self.estado = tipo, "en_curso"
        self.hecho, self.total, self.mensajes, self.resultado, self.error = 0, 0, [], None, None
        self.t0 = time.time()

    def avance(self, hecho, total, msg=""):
        self.hecho, self.total = hecho, total
        if msg:
            self.mensajes.append(msg)

    def a_dict(self):
        return {"id": self.id, "tipo": self.tipo, "estado": self.estado, "hecho": self.hecho, "total": self.total,
                "mensajes": self.mensajes[-30:], "resultado": self.resultado, "error": self.error}


TAREAS: dict[str, Tarea] = {}


def lanzar(tipo: str, fn) -> Tarea:
    t = Tarea(tipo)
    TAREAS[t.id] = t
    for k in [k for k, v in TAREAS.items() if v.estado != "en_curso" and time.time() - v.t0 > 3600]:
        TAREAS.pop(k, None)

    def correr():
        try:
            t.resultado = fn(t)
            t.estado = "terminada"
        except ErrorSkillHub as e:
            t.estado, t.error = "error", str(e)
        except Exception as e:  # noqa: BLE001 - frontera de hilo: se registra con contexto y se informa
            log.exception("fallo inesperado en tarea %s", tipo)
            registrar("error_inesperado", tarea=tipo, error=f"{type(e).__name__}: {e}")
            t.estado, t.error = "error", f"Error inesperado ({type(e).__name__}); revisa el registro."

    threading.Thread(target=correr, daemon=True).start()
    return t


def tarea_lote(accion: str, ids: list, destino: Path, confirmadas: set, forzar: bool):
    def fn(t: Tarea):
        res = {"ok": [], "errores": []}
        for n, ident in enumerate(ids):
            t.avance(n, len(ids), f"{accion}: {ident.split('|')[-1]}")
            try:
                if accion == "desinstalar":
                    res["ok"].append(desinstalar(ident, destino))
                else:
                    res["ok"].append(instalar(ident, destino, confirmadas, forzar))
            except ErrorSkillHub as e:
                res["errores"].append({"id": ident, "tipo": type(e).__name__, "error": str(e)})
        t.avance(len(ids), len(ids), "Listo")
        return res
    return fn


# ───────────────────────── Vista combinada para la interfaz ─────────────────────────
def vista_catalogo() -> dict:
    cat, usr, cfg = cargar_catalogo(), cargar_usuario(), cargar_config()
    est = estado_skills(cat, Path(cfg["destino"]).expanduser())
    items = []
    for s in cat["skills"]:
        e = est[s["id"]]
        u = usr["skills"].get(s["id"], {})
        items.append({"id": s["id"], "repo": s["repo"], "dir": s["dir"], "name": s["name"], "en": s.get("en", ""),
                      "es": u.get("es") or s.get("es", ""), "cat": u.get("c") or s["cat"], "cat0": s["cat"], "alc": s["alc"],
                      "dup": s.get("dup", False), "archivos": s.get("archivos", 0), "bytes": s.get("bytes", 0),
                      "origen": s.get("origen", "activa"), "cambio": s.get("cambio"), "cambio_ts": s.get("cambio_ts"),
                      "irregulares": s.get("irregulares", 0), "estado": e["estado"], "desactualizada": e["desactualizada"],
                      "choque": e["choque"], "marca": u.get("s", ""), "nota": u.get("n", "")})
    esc = escanear_destino(Path(cfg["destino"]).expanduser())
    externas = [{"carpeta": k, "gestionada": v["gestionada"]} for k, v in esc.items() if not v["gestionada"]]
    fuentes = cargar_fuentes()
    for f in fuentes:
        f.update({k: v for k, v in cat["fuentes"].get(f["repo"], {}).items()})
    return {"items": items, "fuentes": fuentes, "externas": externas, "config": cfg, "generado": cat.get("generado"),
            "perfiles": cargar_perfiles(), "version": VERSION}


def cargar_perfiles() -> list:
    return leer_json(HOME / "perfiles.json", [])


# ───────────────────────── Servidor local ─────────────────────────
class Servidor(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def crear_manejador(token: str, hosts: set):
    ui_html = leer_recurso("ui.html").decode("utf-8")

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "SkillHub"

        def log_message(self, *a):  # silencio: el registro estructurado es registrar()
            pass

        def _cabeceras_base(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")

        def _enviar(self, codigo, cuerpo: bytes, tipo="application/json; charset=utf-8", extra=None):
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(cuerpo)))
            self._cabeceras_base()
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(cuerpo)

        def _json(self, datos, codigo=200):
            self._enviar(codigo, json.dumps(datos, ensure_ascii=False).encode("utf-8"))

        def _error(self, codigo, msg, tipo="Error"):
            self._json({"error": msg, "tipo": tipo}, codigo)

        def _host_ok(self):
            return self.headers.get("Host", "") in hosts

        def _token_ok(self, query):
            t = self.headers.get("X-Token") or (query.get("t", [""])[0])
            return hmac.compare_digest(t.encode(), token.encode())

        def _cuerpo(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ErrorSkillHub("Cuerpo de solicitud demasiado grande.")
            if n and "application/json" not in (self.headers.get("Content-Type") or ""):
                raise ErrorSkillHub("Se esperaba application/json.")
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(u.query)
            if not self._host_ok():
                return self._error(403, "Host no permitido")
            if u.path == "/":
                if not self._token_ok(q):
                    return self._error(403, "Abre SkillHub con el enlace que imprime el programa (incluye el token de sesión).")
                csp = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
                       "font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
                return self._enviar(200, ui_html.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": csp})
            if not self._token_ok(q):
                return self._error(401, "Token de sesión inválido")
            try:
                if u.path == "/api/catalogo":
                    return self._json(vista_catalogo())
                if u.path == "/api/registro":
                    ruta = HOME / "registro.log"
                    lineas = ruta.read_text(encoding="utf-8").splitlines()[-300:] if ruta.exists() else []
                    return self._json({"lineas": [json.loads(x) for x in reversed(lineas) if x.strip()]})
                m = re.fullmatch(r"/api/tareas/([0-9a-f]{12})", u.path)
                if m:
                    t = TAREAS.get(m.group(1))
                    return self._json(t.a_dict()) if t else self._error(404, "Tarea desconocida")
                if u.path == "/api/zip":
                    ids = [x for x in q.get("id", []) if x]
                    if not ids:
                        return self._error(400, "Falta id")
                    datos = exportar_zip(ids)
                    nombre = "skills.zip" if len(ids) > 1 else nombre_carpeta_seguro(buscar_skill(cargar_catalogo(), ids[0])["name"]) + ".zip"
                    return self._enviar(200, datos, "application/zip", {"Content-Disposition": f'attachment; filename="{nombre}"'})
                return self._error(404, "No encontrado")
            except ErrorSkillHub as e:
                return self._error(400, str(e), type(e).__name__)
            except Exception as e:  # noqa: BLE001 - frontera HTTP
                log.exception("fallo en GET %s", u.path)
                registrar("error_inesperado", ruta=u.path, error=f"{type(e).__name__}: {e}")
                return self._error(500, "Error interno; revisa el registro.")

        def do_POST(self):
            u = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(u.query)
            if not self._host_ok():
                return self._error(403, "Host no permitido")
            origen = self.headers.get("Origin")
            if origen and origen.split("://", 1)[-1] not in hosts:
                return self._error(403, "Origen no permitido")
            if not self._token_ok(q):
                return self._error(401, "Token de sesión inválido")
            try:
                d = self._cuerpo()
                cfg = cargar_config()
                destino = Path(cfg["destino"]).expanduser()
                if u.path == "/api/usuario":
                    ident = str(d.get("id", ""))
                    patch = d.get("patch", {})
                    with LOCK:
                        usr = cargar_usuario()
                        cur = usr["skills"].get(ident, {})
                        for k_in, k_out, validar in (("marca", "s", lambda v: v in ("", "evaluar", "instalar", "descartar")),
                                                      ("cat", "c", lambda v: len(v) <= 80), ("nota", "n", lambda v: len(v) <= 2000),
                                                      ("es", "es", lambda v: len(v) <= 1000)):
                            if k_in in patch:
                                v = str(patch[k_in]).strip()
                                if not validar(v):
                                    raise ErrorSkillHub(f"Valor no válido para {k_in}")
                                cur[k_out] = v
                        cur = {k: v for k, v in cur.items() if v}
                        if cur:
                            usr["skills"][ident] = cur
                        else:
                            usr["skills"].pop(ident, None)
                        escribir_json_atomico(HOME / "usuario.json", usr)
                    return self._json({"ok": True})
                if u.path == "/api/usuario-lote":
                    ids = [str(x) for x in d.get("ids", [])][:2000]
                    marca = str(d.get("marca", ""))
                    if marca not in ("", "evaluar", "instalar", "descartar"):
                        raise ErrorSkillHub("Marca no válida")
                    with LOCK:
                        usr = cargar_usuario()
                        for ident in ids:
                            cur = usr["skills"].get(ident, {})
                            cur["s"] = marca
                            cur = {k: v for k, v in cur.items() if v}
                            if cur:
                                usr["skills"][ident] = cur
                            else:
                                usr["skills"].pop(ident, None)
                        escribir_json_atomico(HOME / "usuario.json", usr)
                    return self._json({"ok": True, "n": len(ids)})
                if u.path == "/api/revisar":
                    return self._json(revisar_skill(str(d.get("id", ""))))
                if u.path in ("/api/instalar", "/api/actualizar", "/api/desinstalar"):
                    ids = [str(x) for x in d.get("ids", [])][:500]
                    if not ids:
                        raise ErrorSkillHub("No hay skills seleccionadas.")
                    accion = u.path.rsplit("/", 1)[1]
                    t = lanzar(accion, tarea_lote(accion, ids, destino, set(map(str, d.get("confirmadas", []))), bool(d.get("forzar"))))
                    return self._json({"tarea": t.id})
                if u.path == "/api/actualizar-todas":
                    cat = cargar_catalogo()
                    ids = [i for i, e in estado_skills(cat, destino).items() if e["estado"] in ("actualizable",)]
                    t = lanzar("actualizar", tarea_lote("actualizar", ids, destino, set(map(str, d.get("confirmadas", []))), False))
                    return self._json({"tarea": t.id, "n": len(ids)})
                if u.path == "/api/sync":
                    repos = [str(x) for x in d.get("repos", [])] or None
                    t = lanzar("sincronizar", lambda tt: sincronizar(repos, "auto", tt.avance))
                    return self._json({"tarea": t.id})
                if u.path == "/api/vistas":
                    with LOCK:
                        cat = cargar_catalogo()
                        ids = set(map(str, d.get("ids", [])))
                        for s in cat["skills"]:
                            if s["id"] in ids:
                                s["cambio"] = None
                        guardar_catalogo(cat)
                    return self._json({"ok": True})
                if u.path == "/api/fuentes":
                    accion = d.get("accion")
                    with LOCK:
                        fuentes = cargar_fuentes()
                        repo = validar_repo(str(d.get("repo", "")))
                        if accion == "agregar":
                            if any(f["repo"] == repo for f in fuentes):
                                raise ErrorSkillHub("Esa fuente ya está en la lista.")
                            fuentes.append({"repo": repo, "ref": validar_ref(str(d.get("ref") or "HEAD")), "alcance": "soporte",
                                            "tema": str(d.get("tema", ""))[:80], "licencia": "", "esfuerzo": ""})
                        elif accion == "quitar":
                            fuentes = [f for f in fuentes if f["repo"] != repo]
                        else:
                            raise ErrorSkillHub("Acción no válida")
                        escribir_json_atomico(HOME / "fuentes.json", fuentes)
                    return self._json({"ok": True})
                if u.path == "/api/perfiles":
                    with LOCK:
                        perfiles = cargar_perfiles()
                        accion = d.get("accion")
                        pid = re.sub(r"[^a-z0-9-]+", "-", str(d.get("id") or d.get("nombre", "")).lower()).strip("-")[:60]
                        if not pid:
                            raise ErrorSkillHub("El perfil necesita un nombre.")
                        perfiles = [p for p in perfiles if p["id"] != pid]
                        if accion == "guardar":
                            perfiles.append({"id": pid, "nombre": str(d.get("nombre", pid))[:80], "descripcion": str(d.get("descripcion", ""))[:300],
                                             "skills": [str(x) for x in d.get("skills", [])][:300], "sugerido": False})
                        elif accion != "borrar":
                            raise ErrorSkillHub("Acción no válida")
                        escribir_json_atomico(HOME / "perfiles.json", perfiles)
                    return self._json({"ok": True})
                if u.path == "/api/config":
                    nuevo = Path(str(d.get("destino", "")).strip()).expanduser()
                    if not str(nuevo) or not nuevo.is_absolute():
                        raise ErrorSkillHub("El destino debe ser una ruta absoluta.")
                    try:
                        nuevo.mkdir(parents=True, exist_ok=True)
                        prueba = nuevo / f".skillhub-prueba-{secrets.token_hex(3)}"
                        prueba.write_text("ok", encoding="utf-8")
                        prueba.unlink()
                    except OSError as e:
                        raise ErrorSkillHub(f"No se puede escribir en esa carpeta: {e}")
                    with LOCK:
                        cfg["destino"] = str(nuevo)
                        escribir_json_atomico(HOME / "config.json", cfg)
                    registrar("config", destino=str(nuevo))
                    return self._json({"ok": True})
                return self._error(404, "No encontrado")
            except json.JSONDecodeError:
                return self._error(400, "JSON inválido")
            except ErrorSkillHub as e:
                return self._error(400, str(e), type(e).__name__)
            except Exception as e:  # noqa: BLE001 - frontera HTTP
                log.exception("fallo en POST %s", u.path)
                registrar("error_inesperado", ruta=u.path, error=f"{type(e).__name__}: {e}")
                return self._error(500, "Error interno; revisa el registro.")

    return H


def servir(puerto: int = 8765, abrir: bool = True) -> None:
    iniciar_datos()
    token = secrets.token_urlsafe(24)
    srv = None
    for p in range(puerto, puerto + 20):
        hosts = {f"127.0.0.1:{p}", f"localhost:{p}"}
        try:
            srv = Servidor(("127.0.0.1", p), crear_manejador(token, hosts))
            puerto = p
            break
        except OSError:
            continue
    if srv is None:
        raise ErrorSkillHub(f"No hay puertos libres entre {puerto} y {puerto + 19}.")
    url = f"http://127.0.0.1:{puerto}/?t={token}"
    print(f"SkillHub {VERSION} en {url}\nCtrl+C para salir. El enlace incluye un token de sesión: no lo compartas.", flush=True)
    if abrir:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


# ───────────────────────── Línea de comandos ─────────────────────────
def resolver(cat: dict, texto: str) -> dict:
    exactos = [s for s in cat["skills"] if s["id"] == texto]
    if exactos:
        return exactos[0]
    por_nombre = [s for s in cat["skills"] if s["name"] == texto and not s.get("dup") and s.get("origen") != "retirada"]
    if len(por_nombre) == 1:
        return por_nombre[0]
    if not por_nombre:
        raise ErrorSkillHub(f"No encuentro «{texto}». Usa «listar» para ver nombres e identificadores.")
    raise ErrorSkillHub("Nombre ambiguo; usa el identificador completo:\n  " + "\n  ".join(s["id"] for s in por_nombre))


def informe_cambios(total: dict) -> str:
    cat = cargar_catalogo()
    por_id = {s["id"]: s for s in cat["skills"]}
    lineas = [f"# Cambios en las fuentes de skills ({ahora()})", ""]
    hay = False
    for repo, r in total["fuentes"].items():
        if "error" in r:
            lineas += [f"## {repo}", f"- Error al sincronizar: {r['error']}", ""]
            hay = True
            continue
        if not (r["nuevas"] or r["modificadas"] or r["retiradas"]):
            continue
        hay = True
        lineas.append(f"## {repo}")
        for clave, titulo in (("nuevas", "Nuevas"), ("modificadas", "Modificadas"), ("retiradas", "Retiradas")):
            for ident in r[clave]:
                s = por_id.get(ident, {})
                lineas.append(f"- {titulo}: `{s.get('name', ident)}` ({s.get('dir', '')})")
        lineas.append("")
    if not hay:
        lineas.append("Sin cambios desde la última sincronización.")
    return "\n".join(lineas) + "\n"


def main(argv=None) -> int:
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(errors="replace")  # consolas cmd con codificación limitada
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="skillhub", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("ui", help="abre la interfaz web local (por defecto)")
    p.add_argument("--puerto", type=int, default=8765)
    p.add_argument("--sin-navegador", action="store_true")
    p = sub.add_parser("sync", help="sincroniza el catálogo con GitHub")
    p.add_argument("--repo", action="append")
    p.add_argument("--modo", choices=["auto", "api", "tarball"], default="auto")
    p.add_argument("--informe", help="escribe un informe Markdown de cambios en esta ruta")
    p = sub.add_parser("listar", help="lista skills con su estado")
    p.add_argument("--solo", choices=["instaladas", "actualizables", "nuevas"])
    sub.add_parser("estado", help="resumen del destino y el catálogo")
    for n, ayuda in (("revisar", "revisión estática previa"), ("instalar", "instala una skill"), ("desinstalar", "desinstala una skill")):
        p = sub.add_parser(n, help=ayuda)
        p.add_argument("skill")
        if n == "instalar":
            p.add_argument("--confirmo-riesgo", action="store_true", help="acepta riesgo medio/alto tras revisar")
    p = sub.add_parser("actualizar", help="actualiza una skill o todas")
    p.add_argument("skill", nargs="?")
    p.add_argument("--todas", action="store_true")
    p.add_argument("--confirmo-riesgo", action="store_true")
    p = sub.add_parser("zip", help="exporta un .zip para claude.ai")
    p.add_argument("skill", nargs="+")
    p.add_argument("--salida", default="skills.zip")
    sub.add_parser("programar", help="muestra cómo programar la sincronización diaria")
    a = ap.parse_args(argv)
    iniciar_datos()
    try:
        cfg = cargar_config()
        destino = Path(cfg["destino"]).expanduser()
        if a.cmd in (None, "ui"):
            servir(getattr(a, "puerto", 8765), not getattr(a, "sin_navegador", False))
        elif a.cmd == "sync":
            total = sincronizar(a.repo, a.modo, lambda h, t, m: print(f"[{h}/{t}] {m}"))
            print(f"Nuevas: {total['nuevas']} · modificadas: {total['modificadas']} · retiradas: {total['retiradas']}")
            for repo, r in total["fuentes"].items():
                if "error" in r:
                    print(f"  ! {repo}: {r['error']}")
            if a.informe:
                Path(a.informe).write_text(informe_cambios(total), encoding="utf-8")
        elif a.cmd == "listar":
            v = vista_catalogo()
            for s in v["items"]:
                if a.solo == "instaladas" and s["estado"] == "no_instalada":
                    continue
                if a.solo == "actualizables" and s["estado"] != "actualizable":
                    continue
                if a.solo == "nuevas" and s["cambio"] != "nueva":
                    continue
                print(f"{s['estado']:<13} {s['name']:<40} {s['repo']}")
        elif a.cmd == "estado":
            v = vista_catalogo()
            c = {}
            for s in v["items"]:
                c[s["estado"]] = c.get(s["estado"], 0) + 1
            print(f"Destino: {destino}\nCatálogo: {len(v['items'])} skills · {c}\nExternas en el destino (no gestionadas): {len(v['externas'])}")
        elif a.cmd == "revisar":
            r = revisar_skill(resolver(cargar_catalogo(), a.skill)["id"])
            print(f"Riesgo {r['nivel']} · {r['archivos']} archivos · {r['total_hallazgos']} señales\n{r['aviso']}")
            for h in r["hallazgos"][:25]:
                print(f"  [{h['nivel']}] {h['regla']} — {h['archivo']}:{h['linea']}  {h['texto']}")
        elif a.cmd == "instalar":
            ident = resolver(cargar_catalogo(), a.skill)["id"]
            print(instalar(ident, destino, {ident} if a.confirmo_riesgo else set()))
        elif a.cmd == "desinstalar":
            print(desinstalar(resolver(cargar_catalogo(), a.skill)["id"], destino))
        elif a.cmd == "actualizar":
            cat = cargar_catalogo()
            if a.todas:
                ids = [i for i, e in estado_skills(cat, destino).items() if e["estado"] == "actualizable"]
            elif a.skill:
                ids = [resolver(cat, a.skill)["id"]]
            else:
                raise ErrorSkillHub("Indica una skill o usa --todas.")
            for ident in ids:
                try:
                    print(instalar(ident, destino, set(ids) if a.confirmo_riesgo else set()))
                except ErrorSkillHub as e:
                    print(f"  ! {ident}: {e}")
            print(f"{len(ids)} skill(s) procesadas.")
        elif a.cmd == "zip":
            cat = cargar_catalogo()
            Path(a.salida).write_bytes(exportar_zip([resolver(cat, x)["id"] for x in a.skill]))
            print(f"Escrito {a.salida}")
        elif a.cmd == "programar":
            py = sys.executable
            if getattr(sys, "frozen", False):
                script = ""
            elif sys.argv[0].endswith(".pyz"):
                script = f' \\"{Path(sys.argv[0]).resolve()}\\"'
            else:
                script = f' \\"{APP_DIR / "skillhub.py"}\\"'
            print("Windows (Programador de tareas, diario a las 08:00; ejecútalo tú en una consola):\n"
                  f'  schtasks /Create /SC DAILY /ST 08:00 /TN "SkillHub sync" /TR "\\"{py}\\"{script} sync"\n'
                  "Linux/macOS (crontab -e):\n"
                  f"  0 8 * * * \"{py}\"{script.replace(chr(92), '')} sync")
        return 0
    except ErrorSkillHub as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _entrada() -> None:
    sys.exit(main())


if __name__ == "__main__":
    _entrada()
