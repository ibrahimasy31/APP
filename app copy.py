
"""
Dashboard Ultra Évolué - Suivi mensuel des classes (Excel multi-feuilles)
Auteur: ChatGPT
Usage:
    pip install -r requirements.txt
    streamlit run app.py
"""

from __future__ import annotations

import io
import re
from streamlit_autorefresh import st_autorefresh
import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional
import time
import requests

import numpy as np
import pandas as pd
import streamlit as st
import plotly.express as px

# PDF (ReportLab)
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image as RLImage
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

st.set_page_config(
    page_title="IAID — Suivi des classes (Dashboard)",
    layout="wide",
    page_icon="📊",
)

st.markdown(
    """
    <style>
      /* --- Fond global --- */
      .stApp { background: #F6F8FC; }

      /* --- Sidebar --- */
      section[data-testid="stSidebar"] {
        background: #FFFFFF;
        border-right: 1px solid #E6EAF2;
      }

      /* --- Cartes KPI (metrics) --- */
      div[data-testid="stMetric"]{
        background: #FFFFFF;
        border: 1px solid #E6EAF2;
        padding: 14px 16px;
        border-radius: 18px;
        box-shadow: 0 10px 24px rgba(14, 30, 37, 0.06);
      }

      /* --- Titres d'onglets --- */
      button[data-baseweb="tab"]{
        font-weight: 700 !important;
        letter-spacing: 0.2px;
      }

      /* --- Dataframes / tables --- */
      div[data-testid="stDataFrame"]{
        background: #FFFFFF;
        border: 1px solid #E6EAF2;
        border-radius: 18px;
        padding: 6px;
        box-shadow: 0 10px 24px rgba(14, 30, 37, 0.04);
      }

      /* --- Boutons --- */
      .stDownloadButton button, .stButton button{
        border-radius: 14px !important;
        padding: 10px 14px !important;
        font-weight: 700 !important;
      }

      /* --- Bandeau IAID --- */
      .iaid-banner{
        background: linear-gradient(90deg, #0B3D91 0%, #1F6FEB 100%);
        color: white;
        padding: 16px 18px;
        border-radius: 18px;
        box-shadow: 0 10px 24px rgba(14, 30, 37, 0.10);
        margin-bottom: 14px;
      }
      .iaid-banner .title{
        font-size: 18px;
        font-weight: 800;
      }
      .iaid-banner .subtitle{
        font-size: 13px;
        opacity: 0.95;
        margin-top: 4px;
      }
      .iaid-badges{
        margin-top: 10px;
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
      }
      .iaid-badge{
        background: rgba(255,255,255,0.18);
        border: 1px solid rgba(255,255,255,0.25);
        padding: 5px 10px;
        border-radius: 999px;
        font-size: 12px;
        font-weight: 700;
      }

      /* --- Badges statut --- */
      .badge{
        display:inline-block;
        padding: 4px 10px;
        border-radius: 999px;
        font-weight: 800;
        font-size: 12px;
        border: 1px solid #E6EAF2;
      }
      .badge-ok{ background:#E9F7EF; color:#145A32; }
      .badge-warn{ background:#FEF5E7; color:#7D6608; }
      .badge-bad{ background:#FDEDEC; color:#922B21; }
    </style>
    """,
    unsafe_allow_html=True
)

st.markdown(
    """
    <div class="iaid-banner">
      <div class="title">Département IA &amp; Ingénierie des Données (IAID)</div>
      <div class="subtitle">
        Tableau de bord de pilotage mensuel — Suivi des enseignements par classe &amp; par matière
      </div>
      <div class="iaid-badges">
        <div class="iaid-badge">Excel multi-feuilles → Consolidation automatique</div>
        <div class="iaid-badge">KPIs • Alertes • Qualité</div>
        <div class="iaid-badge">Exports : PDF officiel + Excel consolidé</div>
      </div>
    </div>
    """,
    unsafe_allow_html=True
)



# -----------------------------
# Paramètres
# -----------------------------
MOIS_COLS = ["Oct", "Nov", "Déc", "Jan", "Fév", "Mars", "Avril", "Mai", "Juin", "Juil", "Août"]

# Pour l’ordre chrono (année académique)
MOIS_ORDER = {m:i for i,m in enumerate(MOIS_COLS, start=1)}

DEFAULT_THRESHOLDS = {
    "taux_vert": 0.90,
    "taux_orange": 0.60,
    "ecart_critique": -6,  # heures
    "max_non_demarre": 0.25,  # 25% matières non démarrées
}

# -----------------------------
# Utilitaires
# -----------------------------
def clean_colname(s: str) -> str:
    s = str(s)
    s = s.replace("\n", " ").replace('"', "").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [clean_colname(c) for c in df.columns]
    # Harmonisation fréquente
    rename_map = {
    # Matière / VHP
    "Matiere": "Matière",
    "Matière ": "Matière",
    "VHP ": "VHP",

    # Semestre
    "Semestre ": "Semestre",
    "Semester": "Semestre",

    # Mois (robuste)
    "Dec": "Déc",
    "Décembre": "Déc",
    "Fev": "Fév",
    "Février": "Fév",
    "Avr": "Avril",
    "Juillet": "Juil",
    "Juil.": "Juil",
    "Aout": "Août",
    "Août ": "Août",}
   
    df = df.rename(columns={k:v for k,v in rename_map.items() if k in df.columns})
    return df

def ensure_month_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for m in MOIS_COLS:
        if m not in df.columns:
            df[m] = 0
    return df

def to_numeric_safe(s: pd.Series) -> pd.Series:
    # support strings like "9h" or "9,5"
    def conv(x):
        if pd.isna(x):
            return np.nan
        if isinstance(x, (int, float, np.number)):
            return float(x)
        x = str(x).strip()
        x = x.replace(",", ".")
        x = re.sub(r"[^0-9\.\-]", "", x)
        if x == "":
            return np.nan
        try:
            return float(x)
        except:
            return np.nan
    return s.apply(conv)

def compute_metrics(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # Supprimer les colonnes calculées venant d'Excel (si présentes)
    for c in ["VHR", "Écart", "Taux", "Taux (%)"]:
        if c in df.columns:
            df = df.drop(columns=[c])

    df["VHP"] = to_numeric_safe(df["VHP"]).fillna(0)
    for m in MOIS_COLS:
        df[m] = to_numeric_safe(df[m]).fillna(0)

    df["VHR"] = df[MOIS_COLS].sum(axis=1)
    df["Écart"] = df["VHR"] - df["VHP"]
    df["Taux"] = np.where(df["VHP"] == 0, 0, df["VHR"] / df["VHP"])

    def status_row(vhr, vhp):
        if vhr <= 0:
            return "Non démarré"
        if vhr < vhp:
            return "En cours"
        return "Terminé"

    df["Statut_auto"] = [status_row(vhr, vhp) for vhr, vhp in zip(df["VHR"], df["VHP"])]

    # Garder l'ancien champ "Statut" si présent mais proposer "Statut_auto"
    if "Statut" not in df.columns:
        df["Statut"] = df["Statut_auto"]
    else:
        df["Statut"] = df["Statut"].astype(str).replace({"nan": ""}).fillna("")

    if "Observations" not in df.columns:
        df["Observations"] = ""

    # Nettoyage Matière
    df["Matière"] = df["Matière"].astype(str).str.replace("\n", " ").str.strip()
    df["Matière"] = df["Matière"].str.replace(r"\s+", " ", regex=True)

    # Indicateur "Matière" vide
    df["Matière_vide"] = df["Matière"].eq("") | df["Matière"].str.lower().eq("nan")

    return df

def unpivot_months(df: pd.DataFrame) -> pd.DataFrame:
    # Format long : Classe, Matière, VHP, Mois, Heures
    id_cols = [c for c in ["Classe", "Matière", "VHP", "VHR", "Écart", "Taux", "Statut_auto", "Statut", "Observations"] if c in df.columns]
    long = df.melt(id_vars=id_cols, value_vars=MOIS_COLS, var_name="Mois", value_name="Heures")
    long["Mois_idx"] = long["Mois"].map(MOIS_ORDER).fillna(0).astype(int)
    return long

def df_to_excel_bytes(sheets: Dict[str, pd.DataFrame]) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for name, sheet_df in sheets.items():
            sheet_df.to_excel(writer, sheet_name=name[:31], index=False)
    return output.getvalue()

def add_badges(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    def badge(statut: str) -> str:
        s = str(statut)
        if s == "Terminé":
            return '<span class="badge badge-ok">✅ Terminé</span>'
        if s == "En cours":
            return '<span class="badge badge-warn">🟠 En cours</span>'
        return '<span class="badge badge-bad">🔴 Non démarré</span>'
    out["Statut_badge"] = out["Statut_auto"].apply(badge)
    return out

def style_table(df: pd.DataFrame) -> pd.DataFrame:
    # On renvoie un dataframe "propre" (sans Styler)
    out = df.copy()

    # format % si Taux existe
    if "Taux" in out.columns and np.issubdtype(out["Taux"].dtype, np.number):
        out["Taux (%)"] = (out["Taux"] * 100).round(1)

    return out

# -----------------------------
# Lecture Excel multi-feuilles
# -----------------------------
@st.cache_data(show_spinner=False)
def load_excel_all_sheets(file_bytes: bytes) -> Tuple[pd.DataFrame, Dict[str, List[str]]]:
    """
    Retourne:
        - df concaténé (toutes feuilles)
        - quality_issues: dict feuille -> liste d'alertes structurelles
    """
    quality_issues: Dict[str, List[str]] = {}
    xls = pd.ExcelFile(io.BytesIO(file_bytes))
    frames = []

    for sheet in xls.sheet_names:
        try:
            df = pd.read_excel(xls, sheet_name=sheet)
        except Exception as e:
            quality_issues.setdefault(sheet, []).append(f"Lecture impossible: {e}")
            continue

        df = normalize_columns(df)

        # Détection colonnes minimales
        missing = []
        for col in ["Matière", "VHP"]:
            if col not in df.columns:
                missing.append(col)

        if missing:
            quality_issues.setdefault(sheet, []).append(f"Colonnes manquantes: {', '.join(missing)}")
            continue

        df = ensure_month_cols(df)

        # Avertissements légers
        if df.columns.duplicated().any():
            quality_issues.setdefault(sheet, []).append("Colonnes dupliquées détectées.")
        if df["Matière"].isna().mean() > 0.20:
            quality_issues.setdefault(sheet, []).append("Beaucoup de valeurs manquantes dans 'Matière' (>20%).")

        df["Classe"] = sheet
        frames.append(df)

    if not frames:
        return pd.DataFrame(), quality_issues

    all_df = pd.concat(frames, ignore_index=True)
    all_df = compute_metrics(all_df)

    # Qualité globale
    if all_df["Matière_vide"].mean() > 0.05:
        quality_issues.setdefault("__GLOBAL__", []).append("Plus de 5% de lignes ont une 'Matière' vide/invalides.")
    if (all_df["VHP"] <= 0).mean() > 0.10:
        quality_issues.setdefault("__GLOBAL__", []).append("Plus de 10% de lignes ont VHP <= 0 (à vérifier).")

    return all_df, quality_issues

# -----------------------------
# PDF (ReportLab)
# -----------------------------
def build_pdf_report(
    df: pd.DataFrame,
    title: str,
    mois_couverts: List[str],
    thresholds: dict,
    logo_bytes: Optional[bytes] = None,
) -> bytes:
    styles = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=16, spaceAfter=10)
    H2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=12, spaceAfter=6)
    P  = ParagraphStyle("P", parent=styles["BodyText"], fontSize=9, leading=12)
    Small = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=8, leading=10)

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=1.6*cm, rightMargin=1.6*cm, topMargin=1.4*cm, bottomMargin=1.4*cm)

    story = []

    # Couverture
    if logo_bytes:
        try:
            img = RLImage(io.BytesIO(logo_bytes))
            img.drawHeight = 2.0*cm
            img.drawWidth  = 2.0*cm
            story.append(img)
        except:
            pass

    story.append(Paragraph(title, H1))
    story.append(Paragraph(f"Période couverte (heures mensuelles) : <b>{' – '.join(mois_couverts)}</b>", P))
    story.append(Paragraph(f"Date de génération : <b>{dt.datetime.now().strftime('%d/%m/%Y %H:%M')}</b>", P))
    story.append(Spacer(1, 10))

    # KPIs globaux
    total = len(df)
    taux_moy = float(df["Taux"].mean() * 100) if total else 0.0
    nb_term = int((df["Statut_auto"] == "Terminé").sum())
    nb_enc  = int((df["Statut_auto"] == "En cours").sum())
    nb_nd   = int((df["Statut_auto"] == "Non démarré").sum())

    kpi_table = Table(
        [
            ["Matières", "Taux moyen", "Terminées", "En cours", "Non démarrées"],
            [str(total), f"{taux_moy:.1f}%", str(nb_term), str(nb_enc), str(nb_nd)],
        ],
        colWidths=[3.0*cm, 3.0*cm, 3.0*cm, 3.0*cm, 3.4*cm],
    )
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0B3D91")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
        ("BACKGROUND", (0,1), (-1,1), colors.whitesmoke),
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 12))

    # Alertes synthèse
    story.append(Paragraph("Synthèse – alertes clés", H2))
    crit = df[(df["Écart"] <= thresholds["ecart_critique"]) | (df["Statut_auto"] == "Non démarré")].copy()
    if crit.empty:
        story.append(Paragraph("Aucune alerte critique détectée selon les seuils actuels.", P))
    else:
        # Top 12 alertes
        crit = crit.sort_values(["Classe", "Écart"])
        rows = [["Classe", "Matière", "VHP", "VHR", "Écart", "Statut"]]
        for _, r in crit.head(12).iterrows():
            rows.append([str(r["Classe"]), str(r["Matière"])[:45], f"{r['VHP']:.0f}", f"{r['VHR']:.0f}", f"{r['Écart']:.0f}", str(r["Statut_auto"])])
        t = Table(rows, colWidths=[2.4*cm, 8.2*cm, 1.3*cm, 1.3*cm, 1.3*cm, 2.6*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#F0F3F8")),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE", (0,0), (-1,-1), 8),
            ("GRID", (0,0), (-1,-1), 0.25, colors.lightgrey),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ]))
        story.append(t)
        story.append(Paragraph("NB : liste limitée aux 12 premières alertes (tri par écart).", Small))
        
    story.append(PageBreak())

    # Détail par classe
    story.append(Paragraph("Détail par classe", H1))
    for classe, g in df.groupby("Classe"):
        story.append(Paragraph(f"Classe : {classe}", H2))

        # KPIs classe
        total_c = len(g)
        taux_c = float(g["Taux"].mean() * 100) if total_c else 0.0
        nd_c = int((g["Statut_auto"] == "Non démarré").sum())
        enc_c = int((g["Statut_auto"] == "En cours").sum())
        term_c = int((g["Statut_auto"] == "Terminé").sum())
        story.append(Paragraph(f"Matières: <b>{total_c}</b> — Taux moyen: <b>{taux_c:.1f}%</b> — Terminé: <b>{term_c}</b> — En cours: <b>{enc_c}</b> — Non démarré: <b>{nd_c}</b>", P))
        story.append(Spacer(1, 6))

        # Table compacte (top retards)
        gg = g.sort_values("Écart").copy()
        rows = [["Matière", "VHP", "VHR", "Écart", "Taux", "Statut"]]
        for _, r in gg.head(15).iterrows():
            rows.append([str(r["Matière"])[:45], f"{r['VHP']:.0f}", f"{r['VHR']:.0f}", f"{r['Écart']:.0f}", f"{(r['Taux']*100):.0f}%", str(r["Statut_auto"])])
        t = Table(rows, colWidths=[8.6*cm, 1.3*cm, 1.3*cm, 1.3*cm, 1.3*cm, 2.2*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0B3D91")),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE", (0,0), (-1,-1), 8),
            ("GRID", (0,0), (-1,-1), 0.25, colors.lightgrey),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ]))
        story.append(t)
        story.append(Spacer(1, 8))

    doc.build(story)
    return out.getvalue()

# -----------------------------
# UI
# -----------------------------
st.title("📊 IAID — Suivi mensuel des enseignements")
st.caption("Département IA & Ingénierie des Données (IAID) — Consolidation automatique à partir des feuilles Excel (1 feuille = 1 classe).")


with st.sidebar:
    st.header("Import & Paramètres")

    # --- MODE STREAMLIT CLOUD (URL auto + upload manuel) ---
    import_mode = st.radio("Mode d'import", ["URL (auto)", "Upload (manuel)"], index=0)

    file_bytes = None
    source_label = None

    # Auto-refresh
    st.subheader("Auto-refresh")
    auto_refresh = st.checkbox("Rafraîchir automatiquement (URL)", value=True)
    refresh_sec = st.slider("Intervalle (secondes)", 30, 900, 120, 30)

    if import_mode == "URL (auto)":
        st.caption("Recommandé Streamlit Cloud : lien direct vers un fichier .xlsx")
        default_url = st.secrets.get("IAID_EXCEL_URL", "")
        url = st.text_input("URL du fichier Excel (.xlsx)", value=default_url)

        if url.strip():
            try:
                r = requests.get(url.strip(), timeout=30)
                r.raise_for_status()
                file_bytes = r.content
                source_label = "URL"
            except Exception as e:
                st.error(f"Erreur téléchargement: {e}")

    else:
        uploaded = st.file_uploader("Importer le fichier Excel (.xlsx)", type=["xlsx"])
        if uploaded is not None:
            file_bytes = uploaded.getvalue()
            source_label = f"Upload: {uploaded.name}"

    st.caption("Chaque feuille = une classe. Colonnes attendues : Matière, VHP, Oct..Mai (au minimum).")

    # Période
    st.subheader("Période couverte")
    mois_min, mois_max = st.select_slider(
        "Mois (de → à)",
        options=MOIS_COLS,
        value=("Oct", "Mai"),
    )
    mois_couverts = MOIS_COLS[MOIS_COLS.index(mois_min): MOIS_COLS.index(mois_max) + 1]

    # Seuils
    st.subheader("Seuils d’alerte")
    taux_vert = st.slider("Seuil Vert (Terminé/OK)", 0.50, 1.00, float(DEFAULT_THRESHOLDS["taux_vert"]), 0.05)
    taux_orange = st.slider("Seuil Orange (Attention)", 0.10, 0.95, float(DEFAULT_THRESHOLDS["taux_orange"]), 0.05)
    ecart_critique = st.slider("Écart critique (heures)", -40, 0, int(DEFAULT_THRESHOLDS["ecart_critique"]), 1)

    # Branding
    st.subheader("Branding")
    logo = st.file_uploader("Logo (PNG/JPG) pour le PDF", type=["png", "jpg", "jpeg"])

    st.divider()
    st.subheader("Export")
    export_prefix = st.text_input("Préfixe nom fichier export", value="Suivi_Classes")


thresholds = {"taux_vert": taux_vert, "taux_orange": taux_orange, "ecart_critique": ecart_critique}


if file_bytes is None:
    st.info("➡️ Fournis une source (URL auto via Secrets ou Upload manuel).")
    st.stop()

st.caption(f"Source active : **{source_label}**")

df, quality = load_excel_all_sheets(file_bytes)

# Auto-refresh uniquement en mode URL
# if import_mode == "URL (auto)" and auto_refresh:
#     time.sleep(refresh_sec)
#     st.rerun()

if df.empty:
    st.error("Aucune feuille exploitable. Vérifie que chaque feuille contient au minimum 'Matière' et 'VHP'.")
    if quality:
        st.write("### Détails qualité")
        st.json(quality)
    st.stop()

# Appliquer période couverte (recalcul VHR/Taux sur sous-ensemble)
df_period = df.copy()
df_period["VHR"] = df_period[mois_couverts].sum(axis=1)
df_period["Écart"] = df_period["VHR"] - df_period["VHP"]
df_period["Taux"] = np.where(df_period["VHP"] == 0, 0, df_period["VHR"] / df_period["VHP"])
df_period["Statut_auto"] = np.where(df_period["VHR"] <= 0, "Non démarré", np.where(df_period["VHR"] < df_period["VHP"], "En cours", "Terminé"))

# -----------------------------
# Filtres avancés
# -----------------------------
st.sidebar.header("Filtres")

# -----------------------------
# Filtre Semestre (liste déroulante, défaut = S1)
# -----------------------------
# -----------------------------
# Filtre Semestre (robuste)
# -----------------------------
def normalize_semestre_value(x) -> str:
    if pd.isna(x):
        return ""
    s = str(x).strip().upper()
    s = s.replace("SEMESTRE", "S").replace("SEM", "S").replace(" ", "")
    if s in ["S1", "S2"]:
        return s
    if s.isdigit():
        return f"S{int(s)}"
    return ""


if "Semestre" in df_period.columns:
    df_period["Semestre_norm"] = df_period["Semestre"].apply(normalize_semestre_value)
else:
    df_period["Semestre_norm"] = ""

if (df_period["Semestre_norm"] != "").any():
    semestres = sorted([s for s in df_period["Semestre_norm"].unique().tolist() if s])

    def sem_key(s):
        m = re.search(r"(\d+)$", s)
        return int(m.group(1)) if m else 999

    semestres = sorted(semestres, key=sem_key)
    default_index = semestres.index("S1") if "S1" in semestres else 0
    selected_semestre = st.sidebar.selectbox("Semestre", semestres, index=default_index)
else:
    selected_semestre = None



classes = sorted(df_period["Classe"].dropna().unique().tolist())
selected_classes = st.sidebar.multiselect("Classes", classes, default=classes)


status_opts = ["Non démarré", "En cours", "Terminé"]
selected_status = st.sidebar.multiselect("Statuts", status_opts, default=status_opts)

search_matiere = st.sidebar.text_input("Recherche Matière (regex)", value="")
show_only_delay = st.sidebar.checkbox("Uniquement retards (Écart < 0)", value=False)
min_vhp = st.sidebar.number_input("VHP min", min_value=0.0, value=0.0, step=1.0)

filtered = df_period[
    df_period["Classe"].isin(selected_classes)
    & df_period["Statut_auto"].isin(selected_status)
    & (df_period["VHP"] >= min_vhp)
].copy()

# Application du filtre semestre si applicable
if selected_semestre is not None:
    filtered = filtered[filtered["Semestre_norm"] == selected_semestre]



if search_matiere.strip():
    try:
        filtered = filtered[filtered["Matière"].str.contains(search_matiere, case=False, regex=True, na=False)]
    except re.error:
        st.sidebar.warning("Regex invalide — recherche ignorée.")

if show_only_delay:
    filtered = filtered[filtered["Écart"] < 0]

# -----------------------------
# Onglets (Ultra)
# -----------------------------
tab_overview, tab_classes, tab_matieres, tab_mensuel, tab_alertes, tab_qualite, tab_export = st.tabs(
    ["Vue globale", "Par classe", "Par matière", "Analyse mensuelle", "Alertes", "Qualité des données", "Exports"]
)

# ====== VUE GLOBALE ======
with tab_overview:
    st.subheader("KPIs globaux (période sélectionnée)")

    c1, c2, c3, c4, c5 = st.columns(5)
    total = len(filtered)
    taux_moy = float(filtered["Taux"].mean() * 100) if total else 0.0
    nb_term = int((filtered["Statut_auto"] == "Terminé").sum())
    nb_enc  = int((filtered["Statut_auto"] == "En cours").sum())
    nb_nd   = int((filtered["Statut_auto"] == "Non démarré").sum())
    retard_total = float(filtered.loc[filtered["Écart"] < 0, "Écart"].sum()) if total else 0.0

    c1.metric("Matières", f"{total}")
    c2.metric("Taux moyen", f"{taux_moy:.1f}%")
    c3.metric("Terminées", f"{nb_term}")
    c4.metric("En cours", f"{nb_enc}")
    c5.metric("Retard cumulé (h)", f"{retard_total:.0f}")

    st.divider()

    st.write("### Avancement moyen par classe")
    g = filtered.groupby("Classe")["Taux"].mean().sort_values(ascending=False).reset_index()
    g["Taux (%)"] = (g["Taux"] * 100).round(1)
    st.dataframe(
    g[["Classe","Taux (%)"]],
    use_container_width=True,
    column_config={
        "Taux (%)": st.column_config.ProgressColumn(
            "Taux (%)", min_value=0.0, max_value=100.0, format="%.1f"
        )
    }
    )

    fig = px.bar(g, x="Classe", y="Taux (%)", title="Avancement moyen (%) par classe")
    fig.update_layout(height=420, margin=dict(l=10,r=10,t=60,b=10))
    st.plotly_chart(fig, use_container_width=True)


    st.write("### Répartition des statuts")
    stat = filtered["Statut_auto"].value_counts().reset_index()
    stat.columns = ["Statut", "Nombre"]
    fig = px.pie(stat, names="Statut", values="Nombre", title="Répartition des statuts")
    fig.update_layout(height=420, margin=dict(l=10,r=10,t=60,b=10))
    st.plotly_chart(fig, use_container_width=True)

    st.write("### Top retards (Écart le plus négatif)")
    top_retards = filtered.sort_values("Écart").head(20)[
        ["Classe", "Matière", "VHP", "VHR", "Écart", "Taux", "Statut_auto", "Observations"]
    ].copy()

    # garder Taux numérique pour la coloration
    top_retards_badged = add_badges(top_retards)

    # Affichage "pro" avec badge (HTML)
    st.markdown(
        top_retards_badged[["Classe","Matière","VHP","VHR","Écart","Taux","Statut_badge","Observations"]]
        .to_html(escape=False, index=False),
        unsafe_allow_html=True
    )

    # + une version dataframe colorée (optionnel, très utile)
    st.dataframe(
    top_retards[["Classe","Matière","VHP","VHR","Écart","Taux","Statut_auto","Observations"]],
    use_container_width=True,
    column_config={
        "Taux": st.column_config.ProgressColumn("Taux", min_value=0.0, max_value=1.0, format="%.0f%%"),
        "Écart": st.column_config.NumberColumn("Écart (h)", format="%.0f"),
        "VHP": st.column_config.NumberColumn("VHP", format="%.0f"),
        "VHR": st.column_config.NumberColumn("VHR", format="%.0f"),
        "Statut_auto": st.column_config.TextColumn("Statut"),
    }
)



# ====== PAR CLASSE ======
with tab_classes:
    st.subheader("Drilldown par classe + comparaison")

    colA, colB = st.columns([2, 1])
    with colB:
        cls1 = st.selectbox("Comparer classe A", classes, index=0)
        cls2 = st.selectbox("avec classe B", classes, index=min(1, len(classes)-1) if len(classes) > 1 else 0)

    with colA:
        st.write("### Tableau synthèse par classe")
        synth = filtered.groupby("Classe").agg(
            Matieres=("Matière", "count"),
            Taux_moy=("Taux", "mean"),
            VHP_total=("VHP", "sum"),
            VHR_total=("VHR", "sum"),
            Retard_h=("Écart", lambda s: float(s[s < 0].sum())),
            Terminees=("Statut_auto", lambda s: int((s == "Terminé").sum())),
            Non_demarre=("Statut_auto", lambda s: int((s == "Non démarré").sum())),
        ).reset_index()# Garde taux numérique pour gradient
    synth_view = synth.copy()
    # ici synth_view["Taux_moy"] est encore 0..1 (car "mean" sur Taux)
    synth_view["Taux (%)"] = (synth_view["Taux_moy"] * 100).round(1)

    show = synth_view[["Classe","Matieres","Taux (%)","VHP_total","VHR_total","Retard_h","Terminees","Non_demarre"]].copy()
    st.dataframe(
    show,
    use_container_width=True,
    column_config={
        "Taux (%)": st.column_config.ProgressColumn("Taux (%)", min_value=0.0, max_value=100.0, format="%.1f%%"),
        "Retard_h": st.column_config.NumberColumn("Retard (h)", format="%.0f"),
        "VHP_total": st.column_config.NumberColumn("VHP total", format="%.0f"),
        "VHR_total": st.column_config.NumberColumn("VHR total", format="%.0f"),
        "Matieres": st.column_config.NumberColumn("Matières", format="%d"),
        "Terminees": st.column_config.NumberColumn("Terminées", format="%d"),
        "Non_demarre": st.column_config.NumberColumn("Non démarré", format="%d"),
    }
)


    st.divider()
    st.write("### Détails classe A vs B (KPIs)")
    A = filtered[filtered["Classe"] == cls1].copy()
    B = filtered[filtered["Classe"] == cls2].copy()

    def kpis(one: pd.DataFrame):
        return {
            "Matières": len(one),
            "Taux moyen": float(one["Taux"].mean()*100) if len(one) else 0.0,
            "Retard (h)": float(one.loc[one["Écart"] < 0, "Écart"].sum()) if len(one) else 0.0,
            "Non démarré": int((one["Statut_auto"]=="Non démarré").sum()),
        }
    kA, kB = kpis(A), kpis(B)
    comp = pd.DataFrame({"Indicateur": list(kA.keys()), cls1: list(kA.values()), cls2: list(kB.values())})
    st.dataframe(comp, use_container_width=True)

    st.write("### Retards (Top 15) — Classe A")
    tA = A.sort_values("Écart").head(15)[["Matière","VHP","VHR","Écart","Taux","Statut_auto","Observations"]].copy()
    tA["Taux (%)"] = (tA["Taux"] * 100).round(1)
    st.dataframe(
        tA[["Matière","VHP","VHR","Écart","Taux (%)","Statut_auto","Observations"]],
        use_container_width=True,
        column_config={
            "Taux (%)": st.column_config.ProgressColumn("Taux (%)", min_value=0.0, max_value=100.0, format="%.1f%%"),
            "Écart": st.column_config.NumberColumn("Écart (h)", format="%.0f"),
            "VHP": st.column_config.NumberColumn("VHP", format="%.0f"),
            "VHR": st.column_config.NumberColumn("VHR", format="%.0f"),
        }
    )


    st.write("### Retards (Top 15) — Classe B")
    tB = B.sort_values("Écart").head(15)[["Matière","VHP","VHR","Écart","Taux","Statut_auto","Observations"]].copy()
    tB["Taux"] = (tB["Taux"]*100).round(1).astype(str)+"%"
    st.dataframe(tB, use_container_width=True)

# ====== PAR MATIÈRE ======
with tab_matieres:
    st.subheader("Analyse par matière (toutes classes)")

    # Agrégations
    mat = filtered.groupby("Matière").agg(
        Classes=("Classe", "nunique"),
        VHP=("VHP", "sum"),
        VHR=("VHR", "sum"),
        Taux=("Taux", "mean"),
        Retard=("Écart", lambda s: float(s[s < 0].sum())),
        Non_demarre=("Statut_auto", lambda s: int((s=="Non démarré").sum())),
    ).reset_index()
    mat["Taux (%)"] = (mat["Taux"]*100).round(1)
    st.dataframe(mat.sort_values(["Taux (%)","Retard"], ascending=[True, True]), use_container_width=True)

    st.write("### Matières en alerte (seuils)")
    al = mat[(mat["Taux"] < thresholds["taux_orange"]) | (mat["Retard"] <= thresholds["ecart_critique"])].copy()
    if al.empty:
        st.success("Aucune matière globale en alerte selon les seuils.")
    else:
        st.dataframe(al.sort_values("Taux (%)").head(30), use_container_width=True)

# ====== ANALYSE MENSUELLE ======
with tab_mensuel:
    st.subheader("Analyse mensuelle — heures réalisées & tendances")

    long = unpivot_months(df_period)
    # Appliquer filtres classes/statuts à la table longue via merge index
    key_cols = ["Classe","Matière"]
    base_keys = filtered[key_cols].drop_duplicates()
    long_f = long.merge(base_keys, on=key_cols, how="inner")

    # Heures par mois (total)
    monthly = long_f.groupby("Mois").agg(Heures=("Heures","sum")).reindex(MOIS_COLS).fillna(0)
    st.write("### Heures totales par mois (filtre actif)")
    st.line_chart(monthly)

    # Heures par classe et mois (heat-like table)
    st.write("### Matrice Classe × Mois (heures)")
    pivot = long_f.pivot_table(index="Classe", columns="Mois", values="Heures", aggfunc="sum", fill_value=0).reindex(columns=MOIS_COLS)
    st.dataframe(style_table(pivot.reset_index()), use_container_width=True)

    fig = px.imshow(pivot.values, x=pivot.columns, y=pivot.index, aspect="auto",
                    title="Heatmap — Heures par classe et par mois")
    st.plotly_chart(fig, use_container_width=True)


    st.write("### Classe la plus active par mois")
    top_by_month = pivot.idxmax(axis=0).to_frame(name="Classe top").T
    st.dataframe(top_by_month, use_container_width=True)

# ====== ALERTES ======
with tab_alertes:
    st.subheader("Alertes intelligentes (paramétrables)")

    # Scoring simple d'alerte
    tmp = filtered.copy()
    tmp["Niveau"] = np.where(tmp["Taux"] >= thresholds["taux_vert"], "Vert",
                     np.where(tmp["Taux"] >= thresholds["taux_orange"], "Orange", "Rouge"))
    tmp["Critique"] = (tmp["Écart"] <= thresholds["ecart_critique"]) | (tmp["Statut_auto"]=="Non démarré")
    tmp = tmp.sort_values(["Critique","Niveau","Écart"], ascending=[False, True, True])

    c1, c2 = st.columns(2)
    with c1:
        st.write("### Liste des alertes (priorisées)")
        alerts = tmp.loc[tmp["Critique"] | (tmp["Niveau"]=="Rouge"), ["Classe","Matière","VHP","VHR","Écart","Taux","Statut_auto","Niveau","Observations"]].copy()
        alerts["Taux"] = (alerts["Taux"]*100).round(1).astype(str)+"%"
        st.dataframe(alerts.head(50), use_container_width=True)

    with c2:
        st.write("### Alerte “Non démarré” par classe")
        nd = filtered[filtered["Statut_auto"]=="Non démarré"].groupby("Classe").size().sort_values(ascending=False)
        st.bar_chart(nd)

        st.write("### Alerte “Retards critiques” par classe")
        crit = filtered[filtered["Écart"] <= thresholds["ecart_critique"]].groupby("Classe").size().sort_values(ascending=False)
        st.bar_chart(crit)

# ====== QUALITÉ DES DONNÉES ======
with tab_qualite:
    st.subheader("Contrôles qualité & hygiène des données")
    if quality:
        st.write("### Alertes structurelles (lecture/colonnes)")
        st.json(quality)
    else:
        st.success("Aucune alerte structurelle détectée.")

    st.write("### Statistiques de complétude")
    qc = pd.DataFrame({
        "Champ": ["Matière vide", "VHP <= 0", "Valeurs mois manquantes (moyenne)"],
        "Taux": [
            float(df_period["Matière_vide"].mean()),
            float((df_period["VHP"] <= 0).mean()),
            float(df_period[MOIS_COLS].isna().mean().mean()),
        ],
    })
    qc["Taux"] = (qc["Taux"]*100).round(2).astype(str) + "%"
    st.dataframe(qc, use_container_width=True)

    st.write("### Lignes suspectes (à corriger)")
    suspects = df_period[df_period["Matière_vide"] | (df_period["VHP"]<=0)].head(100)
    st.dataframe(suspects[["Classe","Matière","VHP"] + MOIS_COLS], use_container_width=True)

# ====== EXPORTS ======
with tab_export:
    st.subheader("Exports (Excel consolidé + PDF officiel)")

    st.write("### Export Excel consolidé")
    export_df = filtered[["Classe","Matière","VHP"] + MOIS_COLS + ["VHR","Écart","Taux","Statut_auto","Observations"]].copy()
    export_df["Taux"] = (export_df["Taux"]*100).round(2)

    synth_class = filtered.groupby("Classe").agg(
        Matieres=("Matière","count"),
        Taux_moy=("Taux","mean"),
        VHP_total=("VHP","sum"),
        VHR_total=("VHR","sum"),
        Retard_h=("Écart", lambda s: float(s[s<0].sum()))
    ).reset_index()
    synth_class["Taux_moy"] = (synth_class["Taux_moy"]*100).round(2)

    xbytes = df_to_excel_bytes({
        "Consolidé": export_df,
        "Synthese_Classes": synth_class,
    })

    st.download_button(
        "⬇️ Télécharger Excel consolidé",
        data=xbytes,
        file_name=f"{export_prefix}_consolide.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    st.divider()
    st.write("### Export PDF (rapport mensuel officiel)")

    pdf_title = st.text_input(
        "Titre du rapport PDF",
        value="Rapport mensuel — Suivi des enseignements (IAID) | Département IA & Ingénierie des Données"
    )
    logo_bytes = logo.getvalue() if logo else None

    if st.button("Générer le PDF"):
        pdf = build_pdf_report(
            df=filtered[["Classe","Matière","VHP"] + mois_couverts + ["VHR","Écart","Taux","Statut_auto","Statut","Observations"]].copy(),
            title=pdf_title,
            mois_couverts=mois_couverts,
            thresholds=thresholds,
            logo_bytes=logo_bytes,
        )
        st.download_button(
            "⬇️ Télécharger le PDF",
            data=pdf,
            file_name=f"{export_prefix}_rapport.pdf",
            mime="application/pdf",
        )

st.caption("✅ Astuce : standardise les colonnes sur toutes les feuilles. L’app calcule automatiquement VHR/Écart/Taux/Statut selon la période sélectionnée.")
# 🔄 Auto-refresh propre (Streamlit Cloud)
if import_mode == "URL (auto)" and auto_refresh:
    st_autorefresh(interval=refresh_sec * 1000, key="iaid_refresh")
