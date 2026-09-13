import datetime
import io
import numpy as np
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="OptiLácteos - Motor Anti-Merma",
    page_icon="🧀",
    layout="wide",
)

st.title("🧀 OptiLácteos: Motor de Reposición Dinámica Anti-Merma")
st.markdown(
    "**Iniciativa 1: Abastecimiento Pull basado en demanda real y ventana"
    " vendible (CEDI X ➔ Red)**"
)

# -------------------------------------------------------------
# 1. BARRA LATERAL: PARÁMETROS LOGÍSTICOS
# -------------------------------------------------------------
st.sidebar.header("⚙️ Configuración Operativa")
uploaded_file = st.sidebar.file_uploader(
    "Subir archivo Excel (Datos_trabajo_final.xlsx)", type=["xlsx"]
)

st.sidebar.subheader("Reglas de Negocio")
pack_size = st.sidebar.number_input(
    "Tamaño de Caja Estándar (unidades)", value=10, step=1
)
lead_time_days = st.sidebar.slider(
    "Lead Time de Despacho (días)", min_value=1, max_value=3, value=1
)
cycle_days = st.sidebar.slider(
    "Días entre visitas de camión", min_value=2, max_value=7, value=4
)
vida_util_vendible = st.sidebar.number_input(
    "Vida Útil Vendible en Góndola (días)", value=20, step=1
)
nivel_servicio_z = st.sidebar.selectbox(
    "Nivel de Servicio Deseado",
    options=[1.28, 1.64, 1.96],
    index=0,
    format_func=lambda x: {1.28: "90%", 1.64: "95%", 1.96: "97.5%"}[x],
)

if uploaded_file is None:
  st.info(
      "👆 Sube el archivo original **Datos_trabajo_final.xlsx** en la barra"
      " lateral para procesar los datos en pantalla."
  )
  st.stop()


# -------------------------------------------------------------
# 2. PROCESAMIENTO DE DATOS
# -------------------------------------------------------------
@st.cache_data
def procesar_datos(file):
  raw_sales = pd.read_excel(file, sheet_name="Venta x tienda")
  raw_sales.columns = raw_sales.iloc[2]
  df_stores = (
      raw_sales.iloc[3:243]
      .dropna(subset=["Codigo Tienda"])
      .reset_index(drop=True)
  )

  date_cols = [
      c
      for c in df_stores.columns
      if isinstance(c, (pd.Timestamp, datetime.date, datetime.datetime))
  ]
  sales_matrix = (
      df_stores[date_cols].apply(pd.to_numeric, errors="coerce").fillna(0)
  )

  metadata = df_stores[["Codigo Tienda", "Tienda", "SKU", "DESCRPCIÓN"]].copy()
  metadata["Codigo Tienda"] = metadata["Codigo Tienda"].astype(int)

  # Mapear rutas
  try:
    raw_despacho = pd.read_excel(file, sheet_name="Despacho por tienda")
    rutas_map = (
        raw_despacho.groupby("CODIGO TIENDA")["RUTA"]
        .agg(lambda x: x.mode()[0] if not x.empty else "REG:01")
        .to_dict()
    )
    metadata["Ruta"] = metadata["Codigo Tienda"].map(rutas_map).fillna("REG:01")
  except Exception:
    metadata["Ruta"] = "REG:01"

  # Stock actual (si no viene la columna, simulamos un stock base de 4u)
  if "Stock_Actual_Gondola" in df_stores.columns:
    metadata["Stock_Gondola"] = (
        df_stores["Stock_Actual_Gondola"].fillna(0).astype(int)
    )
  else:
    metadata["Stock_Gondola"] = 4

  return metadata, sales_matrix, date_cols


metadata, sales_matrix, date_cols = procesar_datos(uploaded_file)

# Cálculos estadísticos
recent_dates = date_cols[-30:]
metadata["Venta_Prom_30D"] = sales_matrix[recent_dates].mean(axis=1).round(2)
metadata["Venta_Prom_Total"] = sales_matrix.mean(axis=1).round(2)
metadata["Desv_30D"] = sales_matrix[recent_dates].std(axis=1).round(2)

metadata["Demanda_Diaria"] = (
    0.70 * metadata["Venta_Prom_30D"] + 0.30 * metadata["Venta_Prom_Total"]
).round(2)

sigma_lt = np.where(
    metadata["Desv_30D"] > 0,
    metadata["Desv_30D"] * np.sqrt(lead_time_days),
    np.sqrt(metadata["Demanda_Diaria"] * lead_time_days),
)
metadata["Stock_Seguridad"] = np.ceil(nivel_servicio_z * sigma_lt).astype(int)

# ROP y límites
metadata["ROP"] = (
    np.ceil(metadata["Demanda_Diaria"] * lead_time_days)
    + metadata["Stock_Seguridad"]
).astype(int)
metadata["Capacidad_Max_20D"] = np.floor(
    metadata["Demanda_Diaria"] * vida_util_vendible
).astype(int)
metadata["Inventario_Objetivo"] = (
    metadata["ROP"] + np.ceil(metadata["Demanda_Diaria"] * cycle_days)
).astype(int)


# Motor de reposición
def calcular_despacho(row):
  stock = row["Stock_Gondola"]
  rop = row["ROP"]
  target = row["Inventario_Objetivo"]
  cap = row["Capacidad_Max_20D"]

  if stock > rop:
    return 0, 0, "BLOQUEADO (Stock suficiente)", "🔴 Bloqueado"

  faltante = max(0, target - stock)
  cajas = int(np.ceil(faltante / pack_size))
  cajas = max(1, cajas)
  unidades = cajas * pack_size

  if (stock + unidades) > cap:
    if stock + pack_size > cap:
      return 0, 0, "BLOQUEADO (Riesgo de Merma)", "🔴 Bloqueado"
    else:
      cajas_adj = int((cap - stock) // pack_size)
      return (
          cajas_adj,
          cajas_adj * pack_size,
          f"AJUSTADO ({cajas_adj} cajas)",
          "🟠 Ajustado",
      )

  return (
      cajas_cajas := cajas,
      unidades,
      f"APROBADO ({cajas} cajas)",
      "🟢 Aprobado",
  )


res = metadata.apply(calcular_despacho, axis=1)
metadata["Cajas_Despacho"] = [r[0] for r in res]
metadata["Unidades_Despacho"] = [r[1] for r in res]
metadata["Detalle_Motor"] = [r[2] for r in res]
metadata["Estado"] = [r[3] for r in res]

# -------------------------------------------------------------
# 3. KPI CARDS (MÉTRICAS EN PANTALLA)
# -------------------------------------------------------------
total_tiendas = len(metadata)
bloqueadas = (metadata["Cajas_Despacho"] == 0).sum()
aprobadas = (metadata["Cajas_Despacho"] > 0).sum()
cajas_totales = metadata["Cajas_Despacho"].sum()
cajas_push_antiguo = total_tiendas * 1  # 1 caja de 10 a todas

kpi1, kpi2, kpi3, kpi4 = st.columns(4)
kpi1.metric("🏪 Tiendas Totales", f"{total_tiendas}")
kpi2.metric(
    "⛔ Tiendas Bloqueadas (0 cajas)",
    f"{bloqueadas}",
    f"{bloqueadas/total_tiendas*100:.1f}% de la red",
    delta_color="inverse",
)
kpi3.metric("✅ Tiendas con Despacho", f"{aprobadas}")
kpi4.metric(
    "📦 Total Cajas a Enviar",
    f"{cajas_totales} cajas",
    f"{cajas_totales - cajas_push_antiguo} vs Modelo Push",
)

st.markdown("---")

# -------------------------------------------------------------
# 4. PESTAÑAS VISUALES EN PANTALLA
# -------------------------------------------------------------
tab1, tab2, tab3 = st.tabs([
    "📋 Planilla de Despacho en Vivo",
    "🚚 Resumen Consolidado por Ruta",
    "🔍 Auditoría y Diagnóstico por Tienda",
])

# --- PESTAÑA 1: TABLA EN VIVO ---
with tab1:
  st.subheader("Planilla Interactiva de Despacho")

  f1, f2, f3 = st.columns([1, 1, 2])
  with f1:
    rutas_list = ["TODAS"] + sorted(metadata["Ruta"].unique().tolist())
    r_filter = st.selectbox("Filtrar por Ruta:", rutas_list)
  with f2:
    est_list = ["TODOS"] + sorted(metadata["Estado"].unique().tolist())
    e_filter = st.selectbox("Filtrar por Estado:", est_list)
  with f3:
    t_search = st.text_input("🔎 Buscar tienda por nombre o código:")

  df_view = metadata.copy()
  if r_filter != "TODAS":
    df_view = df_view[df_view["Ruta"] == r_filter]
  if e_filter != "TODOS":
    df_view = df_view[df_view["Estado"] == e_filter]
  if t_search:
    df_view = df_view[
        df_view["Tienda"].str.contains(t_search.upper(), na=False)
        | df_view["Codigo Tienda"].astype(str).str.contains(t_search)
    ]

  cols_grid = [
      "Codigo Tienda",
      "Tienda",
      "Ruta",
      "Venta_Prom_30D",
      "Stock_Gondola",
      "ROP",
      "Capacidad_Max_20D",
      "Cajas_Despacho",
      "Unidades_Despacho",
      "Estado",
      "Detalle_Motor",
  ]

  # Mostramos la tabla en pantalla completa
  st.dataframe(
      df_view[cols_grid],
      column_config={
          "Codigo Tienda": st.column_config.NumberColumn(
              "Cód.", format="%d", width="small"
          ),
          "Venta_Prom_30D": st.column_config.NumberColumn(
              "Venta Prom. (u/día)", format="%.2f"
          ),
          "Stock_Gondola": st.column_config.NumberColumn("Stock Góndola"),
          "ROP": st.column_config.NumberColumn("Punto Reorden (ROP)"),
          "Capacidad_Max_20D": st.column_config.NumberColumn("Tope 20 Días"),
          "Cajas_Despacho": st.column_config.NumberColumn(
              "📦 Cajas", format="%d"
          ),
          "Unidades_Despacho": st.column_config.NumberColumn("Unidades"),
          "Estado": st.column_config.TextColumn("Decisión"),
          "Detalle_Motor": st.column_config.TextColumn(
              "Justificación Operativa"
          ),
      },
      hide_index=True,
      use_container_width=True,
  )

  # Botón de descarga adicional por si quieren llevárselo a Excel
  excel_buffer = io.BytesIO()
  with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
    metadata[cols_grid].to_excel(
        writer, sheet_name="Ordenes_Despacho", index=False
    )

  st.download_button(
      label="📥 Exportar esta tabla a Excel",
      data=excel_buffer.getvalue(),
      file_name="Planilla_Despacho_CD_Huachipa.xlsx",
      mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  )

# --- PESTAÑA 2: RESUMEN POR RUTA ---
with tab2:
  st.subheader("Carga Consolidada por Camión y Ruta")
  resumen_ruta = (
      metadata.groupby("Ruta")
      .agg(
          Tiendas_En_Ruta=("Codigo Tienda", "count"),
          Tiendas_Con_Despacho=("Cajas_Despacho", lambda x: (x > 0).sum()),
          Tiendas_Bloqueadas=("Cajas_Despacho", lambda x: (x == 0).sum()),
          Total_Cajas_Camion=("Cajas_Despacho", "sum"),
          Total_Unidades_Camion=("Unidades_Despacho", "sum"),
      )
      .reset_index()
  )

  st.dataframe(resumen_ruta, hide_index=True, use_container_width=True)

  # Gráfico interactivo de cajas por ruta
  st.bar_chart(
      resumen_ruta.set_index("Ruta")[
          ["Total_Cajas_Camion", "Tiendas_Bloqueadas"]
      ]
  )

# --- PESTAÑA 3: DIAGNÓSTICO DETALLADO POR TIENDA ---
with tab3:
  st.subheader("Ficha de Auditoría Individual")
  tienda_elegida = st.selectbox(
      "Selecciona una tienda para ver su análisis:",
      options=metadata["Codigo Tienda"].tolist(),
      format_func=lambda x: (
          f"{x} - {metadata.loc[metadata['Codigo Tienda']==x, 'Tienda'].values[0]}"
      ),
  )

  row_t = metadata[metadata["Codigo Tienda"] == tienda_elegida].iloc[0]

  c_info1, c_info2, c_info3 = st.columns(3)
  c_info1.info(f"**Tienda:** {row_t['Tienda']}\n\n**Ruta:** {row_t['Ruta']}")
  c_info2.metric(
      "Demanda Diaria",
      f"{row_t['Demanda_Diaria']} u/día",
      f"Promedio 30d: {row_t['Venta_Prom_30D']} u",
  )
  c_info3.metric("Decisión de Despacho", f"{row_t['Cajas_Despacho']} cajas de 10")

  st.markdown("### ¿Por qué tomó esta decisión el modelo?")
  st.write(f"- **Stock actual en piso:** `{row_t['Stock_Gondola']} unidades`")
  st.write(
      f"- **Punto de Reorden (ROP):** `{row_t['ROP']} unidades` (Mínimo"
      " necesario para no quebrar)"
  )
  st.write(
      f"- **Tope Anti-Merma (20 días de vida útil):**"
      f" `{row_t['Capacidad_Max_20D']} unidades`"
  )
  st.write(f"- **Diagnóstico final:** {row_t['Detalle_Motor']}")
