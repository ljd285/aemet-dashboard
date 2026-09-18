"""
Genera un dashboard HTML estático con datos climatológicos históricos y
predicción de AEMET OpenData para las estaciones/municipios definidos en
config.py.

Uso:
    export AEMET_API_KEY="tu_clave"
    python fetch_aemet_dashboard.py

El resultado se escribe en docs/index.html (esa carpeta es la que se publica
como GitHub Pages, ver README.md).
"""

import os
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import requests
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from config import STATIONS, DIAS_HISTORICO

BASE_URL = "https://opendata.aemet.es/opendata/api"
API_KEY = os.environ.get("AEMET_API_KEY")

if not API_KEY:
    sys.exit(
        "ERROR: define la variable de entorno AEMET_API_KEY con tu clave "
        "gratuita de AEMET OpenData (https://opendata.aemet.es/centrodedescargas/altaUsuario)."
    )

HEADERS = {"api_key": API_KEY, "Accept": "application/json"}


def aemet_get(endpoint, retries=5):
    """Llama a un endpoint de AEMET.

    AEMET responde primero con un JSON pequeño que contiene la URL real de
    los datos (campo 'datos'), así que hace falta una segunda petición para
    obtener el contenido de verdad.

    AEMET limita las peticiones por minuto con la misma clave (HTTP 429).
    Si se supera el límite, se espera cada vez más tiempo antes de
    reintentar (10s, 20s, 40s, 60s...), porque el límite tarda hasta un
    minuto en liberarse.
    """
    espera = 10
    for intento in range(retries):
        resp = requests.get(f"{BASE_URL}{endpoint}", headers=HEADERS, timeout=30)
        if resp.status_code == 429:
            print(f"Límite de peticiones de AEMET alcanzado, esperando {espera}s (intento {intento + 1}/{retries})...")
            time.sleep(espera)
            espera = min(espera * 2, 60)
            continue
        resp.raise_for_status()
        meta = resp.json()
        if meta.get("estado") != 200:
            raise RuntimeError(f"AEMET devolvió un error: {meta}")
        data_resp = requests.get(meta["datos"], timeout=30)
        data_resp.raise_for_status()
        data_resp.encoding = "latin-1"  # AEMET no siempre declara bien el charset
        return data_resp.json()
    raise RuntimeError(f"Demasiados reintentos (HTTP 429) para {endpoint}")


def resolver_idema(busqueda_nombre):
    """Busca el código IDEMA de una estación climatológica por su nombre."""
    estaciones = aemet_get("/valores/climatologicos/inventarioestaciones/todasestaciones")
    candidatas = [e for e in estaciones if busqueda_nombre.upper() in e["nombre"].upper()]
    if not candidatas:
        raise ValueError(f"No se encontró ninguna estación que contenga '{busqueda_nombre}'")
    if len(candidatas) > 1:
        nombres = ", ".join(f"{c['nombre']} ({c['indicativo']})" for c in candidatas)
        print(f"Aviso: varias estaciones coinciden con '{busqueda_nombre}': {nombres}. Se usa la primera.")
    return candidatas[0]["indicativo"], candidatas[0]["nombre"]


def obtener_historico(idema, dias):
    """Descarga los valores climatológicos diarios de los últimos `dias` días.

    Los valores diarios de AEMET pasan por un proceso de validación antes de
    publicarse, así que los últimos días no están disponibles todavía y una
    petición cuyo rango llegue hasta "hoy" falla por completo (404) en vez
    de devolver solo lo que sí existe. Por eso se aplica un margen de
    seguridad (MARGEN_DIAS) y el rango termina unos días antes de hoy.

    AEMET limita además cada petición a un máximo de ~1 año, así que se
    trocea el rango si hiciera falta (por defecto no hace falta, con 90
    días).
    """
    MARGEN_DIAS = 5
    fecha_fin = datetime.utcnow() - timedelta(days=MARGEN_DIAS)
    fecha_ini = fecha_fin - timedelta(days=dias)
    registros = []
    cursor = fecha_ini
    while cursor < fecha_fin:
        siguiente = min(cursor + timedelta(days=364), fecha_fin)
        ini_str = cursor.strftime("%Y-%m-%dT00:00:00UTC")
        fin_str = siguiente.strftime("%Y-%m-%dT23:59:59UTC")
        endpoint = (
            f"/valores/climatologicos/diarios/datos/"
            f"fechaini/{ini_str}/fechafin/{fin_str}/estacion/{idema}"
        )
        try:
            registros.extend(aemet_get(endpoint))
        except Exception as exc:
            print(f"Aviso: no se pudo descargar el tramo {ini_str} - {fin_str}: {exc}")
        cursor = siguiente + timedelta(days=1)
    return registros


def obtener_prediccion(municipio):
    """Descarga la predicción diaria (7 días) para un municipio."""
    data = aemet_get(f"/prediccion/especifica/municipio/diaria/{municipio}")
    return data[0]


def historico_a_dataframe(registros):
    df = pd.DataFrame(registros)
    if df.empty:
        return df
    df["fecha"] = pd.to_datetime(df["fecha"])
    for col in ["tmax", "tmin", "prec"]:
        if col in df.columns:
            # AEMET usa coma decimal y "Ip" para precipitación inapreciable
            df[col] = df[col].astype(str).str.replace(",", ".", regex=False)
            df[col] = df[col].str.replace("Ip", "0", regex=False)
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("fecha")


def prediccion_a_dataframe(prediccion):
    filas = []
    for dia in prediccion.get("prediccion", {}).get("dia", []):
        fecha = dia["fecha"][:10]
        tmax = dia.get("temperatura", {}).get("maxima")
        tmin = dia.get("temperatura", {}).get("minima")
        probs = dia.get("probPrecipitacion", []) or []
        valores = [p.get("value") for p in probs if p.get("value") is not None]
        prob_precip = max(valores) if valores else None
        filas.append({"fecha": fecha, "tmax": tmax, "tmin": tmin, "prob_precip": prob_precip})
    df = pd.DataFrame(filas)
    if not df.empty:
        df["fecha"] = pd.to_datetime(df["fecha"])
    return df


def construir_figura(nombre, df_hist, df_pred):
    fig = make_subplots(
        rows=2,
        cols=1,
        row_heights=[0.65, 0.35],
        subplot_titles=(f"{nombre} — temperatura (histórico y predicción)", "Precipitación"),
        vertical_spacing=0.12,
    )

    if not df_hist.empty:
        fig.add_trace(
            go.Scatter(x=df_hist["fecha"], y=df_hist["tmax"], name="Máx. histórica", line=dict(color="#e4572e")),
            row=1, col=1,
        )
        fig.add_trace(
            go.Scatter(x=df_hist["fecha"], y=df_hist["tmin"], name="Mín. histórica", line=dict(color="#17bebb")),
            row=1, col=1,
        )
        if "prec" in df_hist.columns:
            fig.add_trace(
                go.Bar(x=df_hist["fecha"], y=df_hist["prec"], name="Precipitación histórica (mm)", marker_color="#2e86ab"),
                row=2, col=1,
            )

    if not df_pred.empty:
        fig.add_trace(
            go.Scatter(x=df_pred["fecha"], y=df_pred["tmax"], name="Máx. prevista", line=dict(color="#e4572e", dash="dot")),
            row=1, col=1,
        )
        fig.add_trace(
            go.Scatter(x=df_pred["fecha"], y=df_pred["tmin"], name="Mín. prevista", line=dict(color="#17bebb", dash="dot")),
            row=1, col=1,
        )
        if "prob_precip" in df_pred.columns:
            fig.add_trace(
                go.Bar(x=df_pred["fecha"], y=df_pred["prob_precip"], name="Prob. precipitación prevista (%)", marker_color="#a4d4ae"),
                row=2, col=1,
            )

    fig.update_layout(height=650, template="plotly_white", legend=dict(orientation="h", y=-0.18))
    return fig


def main():
    os.makedirs("docs", exist_ok=True)
    bloques_html = []
    generado = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    for estacion in STATIONS:
        idema = estacion.get("idema")
        nombre = estacion["nombre"]
        df_hist, df_pred = pd.DataFrame(), pd.DataFrame()

        # El histórico y la predicción se piden por separado: si uno de los
        # dos falla (p. ej. por el límite de peticiones de AEMET), el otro
        # se sigue mostrando en vez de perder todo el bloque de la estación.
        try:
            if not idema:
                idema, nombre_real = resolver_idema(estacion["busqueda_nombre"])
                nombre = estacion.get("nombre") or nombre_real
            print(f"Procesando histórico de {nombre} (idema={idema})...")
            historico = obtener_historico(idema, DIAS_HISTORICO)
            df_hist = historico_a_dataframe(historico)
        except Exception as exc:
            print(f"Aviso: no se pudo obtener el histórico de {nombre}: {exc}")

        time.sleep(2)  # pequeña pausa para no encadenar peticiones demasiado rápido

        try:
            print(f"Procesando predicción de {nombre}...")
            prediccion = obtener_prediccion(estacion["municipio"])
            df_pred = prediccion_a_dataframe(prediccion)
        except Exception as exc:
            print(f"Aviso: no se pudo obtener la predicción de {nombre}: {exc}")

        if df_hist.empty and df_pred.empty:
            bloques_html.append(
                f"<p>No se pudieron cargar datos de {nombre} en esta ejecución "
                "(probablemente el límite de peticiones de AEMET). Se reintentará "
                "automáticamente en la próxima actualización programada.</p>"
            )
        else:
            fig = construir_figura(nombre, df_hist, df_pred)
            bloques_html.append(fig.to_html(full_html=False, include_plotlyjs=False))

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Dashboard AEMET</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
body {{ font-family: -apple-system, Arial, sans-serif; margin: 2rem auto; max-width: 1000px; background:#fafafa; color:#222; }}
h1 {{ font-size: 1.5rem; }}
footer {{ margin-top: 2rem; color: #777; font-size: 0.85rem; }}
</style>
</head>
<body>
<h1>Dashboard climatológico — AEMET OpenData</h1>
<p>Datos históricos ({DIAS_HISTORICO} días) y predicción a 7 días.</p>
{''.join(bloques_html)}
<footer>Generado automáticamente el {generado}. Fuente: AEMET OpenData.</footer>
</body>
</html>"""

    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Dashboard generado en docs/index.html")


if __name__ == "__main__":
    main()
