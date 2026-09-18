"""
Configuración de estaciones/municipios que se muestran en el dashboard.

Para añadir una nueva localidad en el futuro, agrega otra entrada a STATIONS.
No hace falta conocer el código IDEMA de la estación climatológica: si lo
dejas en None, el script lo busca automáticamente por el texto de
'busqueda_nombre' usando el inventario de estaciones de AEMET.

El campo 'municipio' es el código INE de 5 dígitos que usa AEMET para la
predicción por municipios (lo puedes ver en la URL de aemet.es/es/eltiempo/
prediccion/municipios/... -id<codigo>, o buscando el municipio en
https://www.aemet.es/es/eltiempo/prediccion/municipios).
"""

STATIONS = [
    {
        "nombre": "Valencia",
        "idema": "8416",            # Valencia, UPV — fijado a propósito: buscar solo
                                      # por "VALENCIA" es ambiguo (existen también
                                      # Valencia de Don Juan, Valencia de Alcántara, etc.)
        "busqueda_nombre": "VALENCIA, UPV",
        "municipio": "46250",       # código INE de Valencia capital
    },
    
    # Ejemplo de cómo añadir otra estación en el futuro:
    # {
    #     "nombre": "Madrid",
    #     "idema": None,
    #     "busqueda_nombre": "MADRID, RETIRO",
    #     "municipio": "28079",
    # },
]

# Días de histórico climatológico a mostrar en el dashboard
DIAS_HISTORICO = 90
