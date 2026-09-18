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

# Paleta Material Design (tonos 500, salvo donde se indica)
MATERIAL = {
    "rojo": "#F44336",
    "azul": "#2196F3",
    "azul_claro": "#03A9F4",
    "indigo": "#3F51B5",
    "morado": "#673AB7",
    "teal": "#009688",
    "verde": "#4CAF50",
    "gris": "#607D8B",
    "fondo": "#FAFAFA",
    "texto": "#212121",
}


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
    publicarse (AEMET indica un retardo oficial de unos 4 días), así que una
    petición cuyo rango llegue hasta "hoy" puede fallar. Por eso se aplica un
    margen de seguridad (MARGEN_DIAS) y el rango termina unos días antes de hoy.

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
    """Convierte los registros diarios de AEMET en un DataFrame limpio.

    Campos usados (ver metadatos de AEMET): tmax/tmin (°C), prec (mm),
    velmedia/racha (m/s -> se convierten a km/h), hrmedia/hrmax/hrmin (%).
    """
    df = pd.DataFrame(registros)
    if df.empty:
        return df
    df.columns = [c.lower() for c in df.columns]
    df["fecha"] = pd.to_datetime(df["fecha"])

    columnas_numericas = ["tmax", "tmin", "tmed", "prec", "velmedia", "racha", "hrmedia", "hrmax", "hrmin"]
    for col in columnas_numericas:
        if col in df.columns:
            serie = df[col].astype(str).str.replace(",", ".", regex=False)
            if col == "prec":
                # "Ip" = precipitación inapreciable (< 0,1 mm)
                serie = serie.str.replace("Ip", "0", regex=False)
            df[col] = pd.to_numeric(serie, errors="coerce")

    # AEMET da la velocidad del viento en m/s; se pasa a km/h para que
    # coincida con las unidades de la predicción y con lo habitual en AEMET.
    for col in ["velmedia", "racha"]:
        if col in df.columns:
            df[col] = df[col] * 3.6

    return df.sort_values("fecha")


def _max_valor(lista, campo):
    """Extrae el valor numérico máximo de una lista de dicts de AEMET
    (p. ej. 'viento' o 'rachaMax' dentro de la predicción diaria)."""
    valores = []
    for item in lista or []:
        v = item.get(campo)
        if v is None or v == "":
            continue
        try:
            valores.append(float(str(v).replace(",", ".")))
        except (TypeError, ValueError):
            continue
    return max(valores) if valores else None


def prediccion_a_dataframe(prediccion):
    """Convierte la predicción diaria (7 días) de AEMET en un DataFrame.

    Por cada día se extrae: temperatura máx/mín, probabilidad de
    precipitación, viento (velocidad y racha máximas del día) y humedad
    relativa máx/mín.
    """
    filas = []
    for dia in prediccion.get("prediccion", {}).get("dia", []):
        fecha = dia["fecha"][:10]
        tmax = dia.get("temperatura", {}).get("maxima")
        tmin = dia.get("temperatura", {}).get("minima")

        probs = dia.get("probPrecipitacion", []) or []
        valores_precip = [p.get("value") for p in probs if p.get("value") is not None]
        prob_precip = max(valores_precip) if valores_precip else None

        humedad = dia.get("humedadRelativa", {}) or {}

        filas.append({
            "fecha": fecha,
            "tmax": tmax,
            "tmin": tmin,
            "prob_precip": prob_precip,
            "hum_max": humedad.get("maxima"),
            "hum_min": humedad.get("minima"),
            "viento_max": _max_valor(dia.get("viento"), "velocidad"),
            "racha_max": _max_valor(dia.get("rachaMax"), "value"),
        })

    df = pd.DataFrame(filas)
    if not df.empty:
        df["fecha"] = pd.to_datetime(df["fecha"])
        for col in ["tmax", "tmin", "prob_precip", "hum_max", "hum_min"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def construir_figura_historico(df):
    fig = make_subplots(
        rows=2, cols=2,
        subplot_titles=("Temperatura (°C)", "Precipitación (mm)", "Viento (km/h)", "Humedad relativa (%)"),
        vertical_spacing=0.2, horizontal_spacing=0.08,
    )
    if df.empty:
        fig.update_layout(height=550, template="plotly_white")
        return fig

    fig.add_trace(go.Scatter(x=df["fecha"], y=df["tmax"], name="Máxima", line=dict(color=MATERIAL["rojo"], width=2)), row=1, col=1)
    fig.add_trace(go.Scatter(x=df["fecha"], y=df["tmin"], name="Mínima", line=dict(color=MATERIAL["azul"], width=2)), row=1, col=1)

    if "prec" in df.columns:
        fig.add_trace(go.Bar(x=df["fecha"], y=df["prec"], name="Precipitación", marker_color=MATERIAL["azul_claro"], showlegend=False), row=1, col=2)

    if "velmedia" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["velmedia"], name="Vel. media", line=dict(color=MATERIAL["indigo"], width=2)), row=2, col=1)
    if "racha" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["racha"], name="Racha máx.", line=dict(color=MATERIAL["morado"], width=1.5, dash="dot")), row=2, col=1)

    if "hrmax" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["hrmax"], name="Humedad máx.", line=dict(color=MATERIAL["teal"], width=2)), row=2, col=2)
    if "hrmin" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["hrmin"], name="Humedad mín.", line=dict(color=MATERIAL["verde"], width=2)), row=2, col=2)

    fig.update_layout(height=600, template="plotly_white", legend=dict(orientation="h", y=-0.12), margin=dict(t=60, b=40))
    return fig


def construir_figura_prediccion(df):
    fig = make_subplots(
        rows=2, cols=2,
        subplot_titles=("Temperatura prevista (°C)", "Prob. precipitación (%)", "Viento previsto (km/h)", "Humedad prevista (%)"),
        vertical_spacing=0.2, horizontal_spacing=0.08,
    )
    if df.empty:
        fig.update_layout(height=550, template="plotly_white")
        return fig

    fig.add_trace(go.Scatter(x=df["fecha"], y=df["tmax"], name="Máxima prevista", mode="lines+markers", line=dict(color=MATERIAL["rojo"], width=2)), row=1, col=1)
    fig.add_trace(go.Scatter(x=df["fecha"], y=df["tmin"], name="Mínima prevista", mode="lines+markers", line=dict(color=MATERIAL["azul"], width=2)), row=1, col=1)

    if "prob_precip" in df.columns:
        fig.add_trace(go.Bar(x=df["fecha"], y=df["prob_precip"], name="Prob. precipitación", marker_color=MATERIAL["azul_claro"], showlegend=False), row=1, col=2)

    if "viento_max" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["viento_max"], name="Viento previsto", mode="lines+markers", line=dict(color=MATERIAL["indigo"], width=2)), row=2, col=1)
    if "racha_max" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["racha_max"], name="Racha prevista", mode="lines+markers", line=dict(color=MATERIAL["morado"], width=1.5, dash="dot")), row=2, col=1)

    if "hum_max" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["hum_max"], name="Humedad máx. prevista", mode="lines+markers", line=dict(color=MATERIAL["teal"], width=2)), row=2, col=2)
    if "hum_min" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["hum_min"], name="Humedad mín. prevista", mode="lines+markers", line=dict(color=MATERIAL["verde"], width=2)), row=2, col=2)

    fig.update_layout(height=600, template="plotly_white", legend=dict(orientation="h", y=-0.12), margin=dict(t=60, b=40))
    return fig


def construir_recuadro_extremos(df):
    """Tarjetas con el valor más alto y más bajo observado (histórico) de
    temperatura, viento, humedad y precipitación."""
    if df.empty:
        return ""

    def extremo(col, modo):
        if col not in df.columns or df[col].dropna().empty:
            return None
        idx = df[col].idxmax() if modo == "max" else df[col].idxmin()
        valor = df.loc[idx, col]
        fecha = df.loc[idx, "fecha"].strftime("%d/%m/%Y")
        return valor, fecha

    variables = [
        ("Temperatura", MATERIAL["rojo"], extremo("tmax", "max"), extremo("tmin", "min"), "°C"),
        ("Viento", MATERIAL["indigo"], extremo("racha", "max"), extremo("velmedia", "min"), "km/h"),
        ("Humedad", MATERIAL["teal"], extremo("hrmax", "max"), extremo("hrmin", "min"), "%"),
        ("Precipitación", MATERIAL["azul_claro"], extremo("prec", "max"), extremo("prec", "min"), "mm"),
    ]

    tarjetas = []
    for etiqueta, color, alto, bajo, unidad in variables:
        alto_html = f"{alto[0]:.1f} {unidad} <span class='fecha'>({alto[1]})</span>" if alto else "sin datos"
        bajo_html = f"{bajo[0]:.1f} {unidad} <span class='fecha'>({bajo[1]})</span>" if bajo else "sin datos"
        tarjetas.append(
            f'<div class="tarjeta" style="border-top-color:{color};">'
            f"<h3>{etiqueta}</h3>"
            f"<p>▲ Máx: {alto_html}</p>"
            f"<p>▼ Mín: {bajo_html}</p>"
            f"</div>"
        )
    return f'<div class="tarjetas">{"".join(tarjetas)}</div>'


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

        seccion = [f'<section class="estacion"><h2>{nombre}</h2>']
        seccion.append(construir_recuadro_extremos(df_hist))

        seccion.append('<h3 class="subtitulo">Histórico</h3>')
        if df_hist.empty:
            seccion.append('<p class="aviso">No se pudieron cargar datos históricos en esta ejecución.</p>')
        else:
            seccion.append(construir_figura_historico(df_hist).to_html(full_html=False, include_plotlyjs=False))

        seccion.append('<h3 class="subtitulo">Pronóstico (7 días)</h3>')
        if df_pred.empty:
            seccion.append('<p class="aviso">No se pudo cargar la predicción en esta ejecución.</p>')
        else:
            seccion.append(construir_figura_prediccion(df_pred).to_html(full_html=False, include_plotlyjs=False))

        seccion.append("</section>")
        bloques_html.append("".join(seccion))

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Dashboard AEMET</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
:root {{
    --md-rojo: {MATERIAL["rojo"]};
    --md-azul: {MATERIAL["azul"]};
    --md-azul-claro: {MATERIAL["azul_claro"]};
    --md-indigo: {MATERIAL["indigo"]};
    --md-morado: {MATERIAL["morado"]};
    --md-teal: {MATERIAL["teal"]};
    --md-verde: {MATERIAL["verde"]};
    --md-gris: {MATERIAL["gris"]};
}}
body {{
    font-family: "Roboto", -apple-system, Arial, sans-serif;
    margin: 0; padding: 2rem;
    background: {MATERIAL["fondo"]};
    color: {MATERIAL["texto"]};
}}
.contenedor {{ max-width: 1100px; margin: 0 auto; }}
h1 {{ font-size: 1.6rem; font-weight: 500; }}
h2 {{ font-size: 1.3rem; font-weight: 500; color: var(--md-indigo); border-bottom: 2px solid var(--md-indigo); padding-bottom: 0.3rem; }}
.subtitulo {{ font-size: 1.05rem; font-weight: 500; color: var(--md-gris); margin-top: 1.5rem; }}
.estacion {{ background: #fff; border-radius: 8px; padding: 1.5rem; margin-bottom: 2rem; box-shadow: 0 1px 4px rgba(0,0,0,0.12), 0 1px 2px rgba(0,0,0,0.08); }}
.tarjetas {{ display: flex; flex-wrap: wrap; gap: 1rem; margin: 1rem 0 1.5rem; }}
.tarjeta {{ flex: 1 1 200px; background: #fff; border-radius: 6px; border-top: 4px solid; padding: 0.8rem 1rem; box-shadow: 0 1px 3px rgba(0,0,0,0.15); }}
.tarjeta h3 {{ margin: 0 0 0.4rem; font-size: 0.95rem; font-weight: 500; }}
.tarjeta p {{ margin: 0.2rem 0; font-size: 0.9rem; }}
.tarjeta .fecha {{ color: var(--md-gris); font-size: 0.8rem; }}
.aviso {{ color: var(--md-gris); font-style: italic; }}
footer {{ margin-top: 2rem; color: var(--md-gris); font-size: 0.85rem; text-align: center; }}
</style>
</head>
<body>
<div class="contenedor">
<h1>Dashboard climatológico — AEMET OpenData</h1>
<p>Datos históricos ({DIAS_HISTORICO} días) y predicción a 7 días.</p>
{''.join(bloques_html)}
<footer>Generado automáticamente el {generado}. Fuente: AEMET OpenData.</footer>
</div>
</body>
</html>"""

    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Dashboard generado en docs/index.html")


if __name__ == "__main__":
    main()
