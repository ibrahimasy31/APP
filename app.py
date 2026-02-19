
"""
Dashboard Ultra Évolué - Suivi mensuel des classes (Excel multi-feuilles)
Auteur: ChatGPT
Usage:
    pip install -r requirements.txt
    streamlit run app.py
"""

from __future__ import annotations

import hashlib
import io
import os
import re
try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    def st_autorefresh(*args, **kwargs):
        return 0
import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional
import time
import requests
import smtplib
from email.message import EmailMessage
import json
from pathlib import Path
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

import base64
import plotly.io as pio

from config.departments import get_department_config
from services.email_notifications import (
    build_prof_email_html,
    clear_lock,
    lock_is_active,
    send_email_reminder,
    set_last_reminder_month,
    set_lock,
)
from ui.components import (
    niveau_from_statut,
    render_badged_table,
    sidebar_card,
    sidebar_card_end,
    statut_badge_text,
    style_table,
)
from utils.data_pipeline import (
    DEFAULT_THRESHOLDS,
    MOIS_COLS,
    df_to_excel_bytes,
    fetch_excel_if_changed,
    fetch_headers,
    load_excel_all_sheets,
    make_long,
    normalize_semestre_value,
)

# Choix du profil via APP_DEPT_PROFILE: IAID (défaut), KM, DRS
CFG = get_department_config(os.getenv("APP_DEPT_PROFILE", "IAID"))


_tpl_name = CFG["dept_code"].lower()

pio.templates[_tpl_name] = dict(
    layout=dict(
        colorway=CFG["plotly_colorway"],
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(17,27,46,0.60)",
        font=dict(color="#E8EDF5", family="Arial, sans-serif"),
        xaxis=dict(gridcolor="rgba(255,255,255,0.06)", linecolor="rgba(255,255,255,0.10)"),
        yaxis=dict(gridcolor="rgba(255,255,255,0.06)", linecolor="rgba(255,255,255,0.10)"),
        title=dict(font=dict(color="#E8EDF5")),
        legend=dict(bgcolor="rgba(17,27,46,0.70)", bordercolor="rgba(255,255,255,0.08)"),
    )
)

pio.templates.default = "plotly_dark+" + _tpl_name





st.set_page_config(
    page_title=CFG["page_title"],
    layout="wide",
    page_icon=CFG["page_icon"],
)


def safe_secret(key: str, default=""):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def _get_smtp_config() -> dict:
    """Lit et valide la configuration SMTP depuis st.secrets. Lève RuntimeError si incomplète."""
    smtp_host = str(safe_secret("SMTP_HOST", "")).strip()
    smtp_port_raw = str(safe_secret("SMTP_PORT", "")).strip()
    smtp_user = str(safe_secret("SMTP_USER", "")).strip()
    smtp_pass = str(safe_secret("SMTP_PASS", "")).strip()
    smtp_from = str(safe_secret("SMTP_FROM", "")).strip()

    if not all([smtp_host, smtp_port_raw, smtp_user, smtp_pass, smtp_from]):
        raise RuntimeError(
            "Secrets SMTP manquants: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM"
        )
    try:
        smtp_port = int(smtp_port_raw)
    except ValueError as exc:
        raise RuntimeError("SMTP_PORT invalide (entier attendu).") from exc

    return {
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_user": smtp_user,
        "smtp_pass": smtp_pass,
        "smtp_from": smtp_from,
    }


# ==============================
# ✅ RESUME IA (OPENAI) — OBSERVATIONS
# ==============================
from openai import OpenAI

def _build_obs_payload(df_obs: pd.DataFrame, max_lines: int = 300) -> str:
    """
    Transforme les observations en texte court, lisible par un LLM.
    On limite le nombre de lignes pour éviter les prompts énormes.
    """
    cols_needed = ["Classe", "Matière", "Écart", "Statut_auto", "Observations"]
    for c in cols_needed:
        if c not in df_obs.columns:
            df_obs[c] = ""

    d = df_obs.copy()
    d["Observations"] = (
        d["Observations"].astype(str)
        .replace({"nan": "", "None": ""})
        .fillna("")
        .str.strip()
    )
    d = d[d["Observations"].str.len() > 0].copy()

    # Prioriser : retards les plus critiques d'abord
    if "Écart" in d.columns:
        d["Écart"] = pd.to_numeric(d["Écart"], errors="coerce").fillna(0)
        d = d.sort_values("Écart", ascending=True)

    d = d.head(max_lines)

    lines = []
    for _, r in d.iterrows():
        lines.append(
            f"- Classe: {str(r.get('Classe','')).strip()} | "
            f"Matière: {str(r.get('Matière','')).strip()} | "
            f"Statut: {str(r.get('Statut_auto','')).strip()} | "
            f"Écart(h): {r.get('Écart', 0)} | "
            f"Obs: {str(r.get('Observations','')).strip()}"
        )
    return "\n".join(lines)


def summarize_observations_with_openai(
    df_filtered: pd.DataFrame,
    mois_min: str,
    mois_max: str,
    cfg: dict,
    model: str = "gpt-4.1-mini",
    max_lines: int = 300
) -> str:
    """
    Retourne un résumé DG-ready des observations.
    """
    api_key = str(safe_secret("OPENAI_API_KEY", "")).strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY manquant dans .streamlit/secrets.toml")

    client = OpenAI(api_key=api_key)

    # garder uniquement observations
    df_obs = df_filtered.copy()
    if "Observations" not in df_obs.columns:
        df_obs["Observations"] = ""

    payload = _build_obs_payload(df_obs, max_lines=max_lines)
    if not payload.strip():
        return "Aucune observation renseignée sur la période sélectionnée."

    system = (
        "Tu es un assistant de pilotage académique. "
        "Tu dois produire un résumé professionnel, clair, actionnable, style Direction Générale. "
        "Ne divulgue aucune donnée sensible (emails, infos perso)."
    )

    user = f"""
Contexte:
- Département: {cfg.get('department_long','')}
- Période: {mois_min} → {mois_max}

Données (observations consolidées):
{payload}

Tâche:
1) Résumé exécutif (5–8 lignes)
2) Points critiques récurrents (5–10 puces)
3) Actions recommandées (3–7 actions)
4) Synthèse par classe (1–2 lignes par classe max)
Format: Markdown.
""".strip()

    # API Responses (recommandée)
    resp = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.output_text



st.markdown(
"""
<style>
/* =========================================================
   IAID — THÈME DARK EXÉCUTIF (INSPIRÉ DASHBOARD FUTURISTE)
   Navy sombre • Glow effects • Design moderne
   ========================================================= */

/* Force dark mode partout */
:root, html, body, .stApp {
  color-scheme: dark !important;
}

html, body, .stApp {
  zoom: 1 !important;
  font-size: 16px !important;
}

/* -----------------------------
   VARIABLES DARK
------------------------------*/
:root{
  --bg:       #080E1A;
  --bg2:      #0D1526;
  --card:     #111B2E;
  --card2:    #162035;
  --text:     #E8EDF5;
  --muted:    #7A90B8;
  --line:     rgba(255,255,255,0.07);

  --blue:     #1F6FEB;
  --blue2:    #2A80FF;
  --blue3:    #5AA2FF;
  --blue-glow:rgba(31,111,235,0.25);

  --ok:       #00C97A;
  --ok-glow:  rgba(0,201,122,0.20);
  --warn:     #FF9500;
  --warn-glow:rgba(255,149,0,0.20);
  --bad:      #FF3B3B;
  --bad-glow: rgba(255,59,59,0.20);

  --focus:    #5AA2FF;
}

/* -----------------------------
   BACKGROUND & TEXTE GLOBAL
------------------------------*/
html, body, .stApp{
  background: radial-gradient(ellipse at 10% 0%, #0D1E3A 0%, var(--bg) 60%) !important;
}

body, .stApp, p, span, div, label{
  color: var(--text) !important;
  -webkit-font-smoothing: antialiased;
}

/* Titres */
h1, h2, h3, h4, h5{
  color: var(--blue3) !important;
  font-weight: 850 !important;
}

/* Liens */
a, a:visited{
  color: var(--blue3) !important;
  text-decoration: none;
}
a:hover{ text-decoration: underline; }

/* Caption */
.stCaption, small{
  color: var(--muted) !important;
  font-weight: 650;
}

/* -----------------------------
   STREAMLIT LAYOUT
------------------------------*/
.block-container{
  padding-top: .5rem !important;
  padding-bottom: 4.5rem !important;
}
header[data-testid="stHeader"],
div[data-testid="stToolbar"]{
  visibility: hidden !important;
  height: 0px !important;
}

/* Scrollbar dark */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg2); }
::-webkit-scrollbar-thumb { background: rgba(90,162,255,0.25); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: rgba(90,162,255,0.45); }

/* -----------------------------
   SIDEBAR
------------------------------*/
section[data-testid="stSidebar"]{
  background: var(--bg2) !important;
  border-right: 1px solid var(--line);
}
.sidebar-card{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 16px;
  padding: 12px;
  margin-bottom: 10px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.40);
}
/* ---- LOGO SIDEBAR ---- */
.sidebar-logo-wrap{
  display: flex;
  justify-content: center;
  align-items: center;
  margin: 18px 0 20px 0;
}

.sidebar-logo-wrap img{
  width: 170px;
  max-width: 100%;
  height: auto;
  border-radius: 18px;
  border: 1px solid var(--line);
  background: var(--card2);
  padding: 8px;
  box-shadow: 0 0 30px var(--blue-glow), 0 14px 32px rgba(0,0,0,0.30);
}
/* -----------------------------
   INPUTS (dark)
------------------------------*/
div[data-baseweb="input"] > div,
div[data-baseweb="select"] > div{
  background: var(--card2) !important;
  border: 1px solid var(--line) !important;
  border-radius: 14px !important;
}
div[data-baseweb="input"] input,
div[data-baseweb="select"] *{
  color: var(--text) !important;
  font-weight: 700 !important;
}

span[data-baseweb="tag"]{
  background: rgba(31,111,235,0.18) !important;
  border: 1px solid rgba(90,162,255,0.35) !important;
  color: var(--blue3) !important;
  font-weight: 800 !important;
}

/* Focus clavier */
*:focus-visible{
  outline: 2px solid var(--focus) !important;
  outline-offset: 2px !important;
  border-radius: 10px;
}


/* -----------------------------
   HEADER DG — FUTURISTE
------------------------------*/
.iaid-header{
  background: linear-gradient(135deg, #0A1E44 0%, #0F2860 40%, #1A3A80 100%);
  border: 1px solid rgba(90,162,255,0.20);
  color: #FFFFFF !important;
  padding: 20px 26px;
  border-radius: 20px;
  box-shadow:
    0 0 40px rgba(31,111,235,0.15),
    0 20px 48px rgba(0,0,0,0.50),
    inset 0 1px 0 rgba(255,255,255,0.08);
  margin-bottom: 16px;
  position: relative;
  overflow: hidden;
}
.iaid-header::before{
  content:"";
  position:absolute;
  top:-60px; right:-60px;
  width:200px; height:200px;
  background: radial-gradient(circle, rgba(90,162,255,0.15) 0%, transparent 70%);
  pointer-events:none;
}
.iaid-header *{
  color: #FFFFFF !important;
  text-shadow: 0 1px 3px rgba(0,0,0,0.40);
}
.iaid-htitle{ font-size: 20px; font-weight: 950; letter-spacing: -0.3px; }
.iaid-hsub{ font-size: 13px; opacity: .80; margin-top: 4px; }

.iaid-badges{
  margin-top: 12px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.iaid-badge{
  background: rgba(255,255,255,0.08);
  border: 1px solid rgba(255,255,255,0.18);
  padding: 5px 12px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 850;
}

/* -----------------------------
   KPI CARDS — FUTURISTES
------------------------------*/
.kpi-grid{
  display: grid;
  grid-template-columns: repeat(5, minmax(0,1fr));
  gap: 14px;
  margin: 14px 0;
}
.kpi{
  background: linear-gradient(135deg, var(--card) 0%, var(--card2) 100%);
  border: 1px solid var(--line);
  border-radius: 20px;
  padding: 18px 18px 14px 18px;
  box-shadow: 0 8px 32px rgba(0,0,0,0.40);
  position: relative;
  overflow: hidden;
  transition: transform 0.18s ease, box-shadow 0.18s ease;
}
.kpi:hover{
  transform: translateY(-3px);
  box-shadow: 0 16px 48px rgba(0,0,0,0.55);
}
/* Barre colorée en haut */
.kpi:before{
  content:"";
  position:absolute;
  top:0; left:0;
  width:100%; height:3px;
  background: var(--blue);
  border-radius: 20px 20px 0 0;
}
/* Cercle glow en fond */
.kpi:after{
  content:"";
  position:absolute;
  bottom:-30px; right:-30px;
  width:90px; height:90px;
  background: radial-gradient(circle, var(--blue-glow) 0%, transparent 70%);
  pointer-events:none;
}
.kpi-title{
  font-size: 11px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  color: var(--muted) !important;
  margin-bottom: 8px;
}
.kpi-value{
  font-size: 28px;
  font-weight: 950;
  letter-spacing: -0.5px;
  margin-top: 2px;
  line-height: 1;
}
/* Barre verte + glow vert */
.kpi-good:before{ background: linear-gradient(90deg, var(--ok), #00FFA3); }
.kpi-good:after { background: radial-gradient(circle, var(--ok-glow) 0%, transparent 70%); }
.kpi-good .kpi-value{ color: var(--ok) !important; }

/* Barre orange + glow orange */
.kpi-warn:before{ background: linear-gradient(90deg, var(--warn), #FFD060); }
.kpi-warn:after { background: radial-gradient(circle, var(--warn-glow) 0%, transparent 70%); }
.kpi-warn .kpi-value{ color: var(--warn) !important; }

/* Barre rouge + glow rouge */
.kpi-bad:before{ background: linear-gradient(90deg, var(--bad), #FF7070); }
.kpi-bad:after { background: radial-gradient(circle, var(--bad-glow) 0%, transparent 70%); }
.kpi-bad .kpi-value{ color: var(--bad) !important; }

/* -----------------------------
   TABS — DARK
------------------------------*/
button[data-baseweb="tab"]{
  background: var(--card) !important;
  color: var(--muted) !important;
  border-radius: 999px !important;
  padding: 10px 16px !important;
  font-weight: 800 !important;
  border: 1px solid var(--line) !important;
  transition: all 0.15s ease !important;
}
button[data-baseweb="tab"]:hover{
  color: var(--text) !important;
  border-color: rgba(90,162,255,0.30) !important;
}
button[data-baseweb="tab"][aria-selected="true"]{
  background: rgba(31,111,235,0.18) !important;
  color: var(--blue3) !important;
  border: 1px solid rgba(90,162,255,0.40) !important;
  box-shadow: 0 0 14px var(--blue-glow) !important;
}

/* -----------------------------
   DATAFRAMES / TABLES — DARK
------------------------------*/
div[data-testid="stDataFrame"]{
  background: var(--card) !important;
  border: 1px solid var(--line) !important;
  border-radius: 16px !important;
  padding: 6px !important;
  box-shadow: 0 4px 24px rgba(0,0,0,0.30) !important;
}

.table-wrap{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 16px;
  overflow-x: auto;
  box-shadow: 0 4px 24px rgba(0,0,0,0.30);
}

/* -----------------------------
   ALERTES STREAMLIT — DARK
------------------------------*/
div[data-testid="stAlert"]{
  border-radius: 14px !important;
  border: 1px solid var(--line) !important;
  background: var(--card2) !important;
}
div[data-testid="stAlert"] *{
  color: var(--text) !important;
  font-weight: 700 !important;
}

/* =========================================================
   BOUTONS — DARK GLOW
========================================================= */

.stButton button,
.stDownloadButton button,
button[kind="primary"],
button[kind="secondary"]{
  background: linear-gradient(135deg, var(--blue) 0%, #0F4DB5 100%) !important;
  border: 1px solid rgba(90,162,255,0.30) !important;
  border-radius: 14px !important;
  padding: 10px 18px !important;
  color: #FFFFFF !important;
  font-weight: 900 !important;
  box-shadow: 0 0 16px var(--blue-glow), 0 4px 16px rgba(0,0,0,0.40) !important;
  transition: all 0.18s ease !important;
}

.stButton button *,
.stDownloadButton button *,
button[kind="primary"] *,
button[kind="secondary"] *{
  color: #FFFFFF !important;
  fill: #FFFFFF !important;
}

.stButton button:hover,
.stDownloadButton button:hover{
  background: linear-gradient(135deg, var(--blue2) 0%, var(--blue) 100%) !important;
  box-shadow: 0 0 28px rgba(31,111,235,0.45), 0 8px 24px rgba(0,0,0,0.50) !important;
  transform: translateY(-2px) !important;
}

.stDownloadButton a{
  text-decoration: none !important;
}

/* =========================================================
   RESPONSIVE — TOUTES TAILLES D'ÉCRAN
   Breakpoints : 1400 / 1200 / 900 / 700 / 520 / 380 px
========================================================= */

/* ---- ≤ 1400px : grand écran réduit ---- */
@media (max-width: 1400px){
  .kpi-value{ font-size: 24px; }
  .iaid-htitle{ font-size: 18px; }
}

/* ---- ≤ 1200px : tablette paysage / laptop compact ---- */
@media (max-width: 1200px){
  .kpi-grid{
    grid-template-columns: repeat(3, minmax(0,1fr));
    gap: 12px;
  }
  .kpi-value{ font-size: 22px; }
  .iaid-htitle{ font-size: 17px; }
  .iaid-hsub{ font-size: 12px; }
  .block-container{
    padding-left: 1.5rem !important;
    padding-right: 1.5rem !important;
  }
  button[data-baseweb="tab"]{
    padding: 8px 12px !important;
    font-size: 13px !important;
  }
}

/* ---- ≤ 900px : tablette portrait ---- */
@media (max-width: 900px){
  .kpi-grid{
    grid-template-columns: repeat(3, minmax(0,1fr));
    gap: 10px;
  }
  .kpi{
    padding: 14px 14px 10px 14px;
    border-radius: 16px;
  }
  .kpi-value{ font-size: 20px; }
  .kpi-title{ font-size: 10px; }

  .iaid-header{
    padding: 16px 18px;
    border-radius: 16px;
    margin-bottom: 12px;
  }
  .iaid-htitle{ font-size: 15px; font-weight: 900; }
  .iaid-hsub{ font-size: 11px; margin-top: 3px; }
  .iaid-badge{ font-size: 11px; padding: 4px 10px; }
  .iaid-badges{ gap: 6px; margin-top: 10px; }

  .block-container{
    padding-left: 1rem !important;
    padding-right: 1rem !important;
  }

  button[data-baseweb="tab"]{
    padding: 7px 10px !important;
    font-size: 12px !important;
    border-radius: 12px !important;
  }

  .footer-signature{
    font-size: 11px;
    padding: 8px 12px;
  }

  .sidebar-logo-wrap img{
    width: 140px;
  }
}

/* ---- ≤ 700px : grand smartphone paysage / petite tablette ---- */
@media (max-width: 700px){
  html, body, .stApp{ font-size: 15px !important; }

  .kpi-grid{
    grid-template-columns: repeat(2, minmax(0,1fr));
    gap: 10px;
    margin: 10px 0;
  }
  .kpi{
    padding: 12px 12px 10px 12px;
    border-radius: 14px;
  }
  .kpi-value{ font-size: 19px; }
  .kpi-title{ font-size: 10px; letter-spacing: 0.3px; }

  .iaid-header{
    padding: 14px 16px;
    border-radius: 14px;
  }
  .iaid-htitle{ font-size: 14px; }
  .iaid-hsub{ font-size: 11px; }
  .iaid-badges{ gap: 5px; flex-wrap: wrap; }
  .iaid-badge{ font-size: 10px; padding: 4px 9px; }

  .block-container{
    padding-left: .75rem !important;
    padding-right: .75rem !important;
    padding-bottom: 5rem !important;
  }

  button[data-baseweb="tab"]{
    padding: 6px 9px !important;
    font-size: 11px !important;
  }

  .stButton button,
  .stDownloadButton button{
    padding: 8px 14px !important;
    font-size: 13px !important;
    border-radius: 12px !important;
  }

  .footer-signature{
    font-size: 10px;
    padding: 7px 10px;
    line-height: 1.4;
  }

  div[data-testid="stDataFrame"]{
    border-radius: 12px !important;
  }
  .table-wrap{
    border-radius: 12px;
  }

  .sidebar-logo-wrap img{
    width: 120px;
  }
  .sidebar-logo-wrap{
    margin: 12px 0 14px 0;
  }

  .sidebar-card{
    padding: 10px;
    border-radius: 12px;
  }
}

/* ---- ≤ 520px : smartphone portrait ---- */
@media (max-width: 520px){
  html, body, .stApp{ font-size: 14px !important; }

  .kpi-grid{
    grid-template-columns: repeat(2, minmax(0,1fr));
    gap: 8px;
    margin: 8px 0;
  }
  .kpi{
    padding: 10px 10px 8px 10px;
    border-radius: 12px;
  }
  .kpi-value{ font-size: 18px; }
  .kpi-title{ font-size: 9px; }

  .iaid-header{
    padding: 12px 14px;
    border-radius: 12px;
    margin-bottom: 10px;
  }
  .iaid-htitle{ font-size: 13px; }
  .iaid-hsub{ font-size: 10px; }
  .iaid-badge{ font-size: 10px; padding: 3px 8px; }
  .iaid-badges{ margin-top: 8px; gap: 4px; }

  .block-container{
    padding-left: .5rem !important;
    padding-right: .5rem !important;
    padding-bottom: 5.5rem !important;
  }

  button[data-baseweb="tab"]{
    padding: 5px 8px !important;
    font-size: 10px !important;
    border-radius: 10px !important;
  }

  .stButton button,
  .stDownloadButton button{
    padding: 7px 12px !important;
    font-size: 12px !important;
    border-radius: 10px !important;
    width: 100% !important;
  }

  .footer-signature{
    font-size: 10px;
    padding: 6px 8px;
  }

  .badge{ font-size: 10px; padding: 4px 9px; }

  .sidebar-logo-wrap img{
    width: 100px;
  }
  .sidebar-card{
    padding: 8px;
    border-radius: 10px;
    margin-bottom: 8px;
  }

  h1{ font-size: 18px !important; }
  h2{ font-size: 16px !important; }
  h3{ font-size: 14px !important; }
}

/* ---- ≤ 380px : très petit smartphone ---- */
@media (max-width: 380px){
  .kpi-grid{
    grid-template-columns: 1fr;
    gap: 8px;
  }
  .kpi-value{ font-size: 22px; }
  .kpi-title{ font-size: 10px; }

  .iaid-htitle{ font-size: 12px; }
  .iaid-hsub{ font-size: 9px; }

  .block-container{
    padding-left: .35rem !important;
    padding-right: .35rem !important;
  }

  button[data-baseweb="tab"]{
    padding: 4px 6px !important;
    font-size: 9px !important;
  }

  .footer-signature strong{ display: block; }
}

/* -----------------------------
   FOOTER SIGNATURE (FIXE) — DARK
------------------------------*/
.footer-signature{
  position: fixed;
  bottom: 0;
  left: 0;
  width: 100%;
  background: rgba(8,14,26,0.94);
  border-top: 1px solid var(--line);
  padding: 10px 18px;
  font-size: 12px;
  color: var(--muted);
  text-align: center;
  z-index: 999;
  backdrop-filter: blur(12px);
  box-shadow: 0 -4px 24px rgba(0,0,0,0.40);
}
.footer-signature strong{
  color: var(--blue3);
  font-weight: 900;
}
/* =========================
   BADGES STATUT — DARK GLOW
========================= */
.badge{
  display:inline-block;
  padding: 5px 12px;
  border-radius: 999px;
  font-weight: 900;
  font-size: 11px;
  line-height: 1;
  letter-spacing: 0.3px;
}
.badge-ok{
  background: rgba(0,201,122,0.14);
  color: var(--ok);
  border: 1px solid rgba(0,201,122,0.30);
  box-shadow: 0 0 8px rgba(0,201,122,0.15);
}
.badge-warn{
  background: rgba(255,149,0,0.14);
  color: var(--warn);
  border: 1px solid rgba(255,149,0,0.30);
  box-shadow: 0 0 8px rgba(255,149,0,0.15);
}
.badge-bad{
  background: rgba(255,59,59,0.14);
  color: var(--bad);
  border: 1px solid rgba(255,59,59,0.30);
  box-shadow: 0 0 8px rgba(255,59,59,0.15);
}

/* =========================================================
   PATCH BOUTONS — renforcement multi-navigateurs
========================================================= */

.stButton > button,
.stDownloadButton > button{
  background: linear-gradient(135deg, var(--blue) 0%, #0F4DB5 100%) !important;
  color: #FFFFFF !important;
  border: 1px solid rgba(90,162,255,0.25) !important;
  border-radius: 14px !important;
  padding: 10px 18px !important;
  font-weight: 900 !important;
  box-shadow: 0 0 16px var(--blue-glow) !important;
}

.stButton > button *,
.stDownloadButton > button *{
  color: #FFFFFF !important;
  fill: #FFFFFF !important;
  stroke: #FFFFFF !important;
}

.stButton > button:hover,
.stDownloadButton > button:hover{
  box-shadow: 0 0 30px rgba(31,111,235,0.50) !important;
  transform: translateY(-2px) !important;
}

.stDownloadButton a{
  text-decoration: none !important;
}

/* -----------------------------
   HEADER DG — LAYOUT (FIX)
------------------------------*/
.iaid-hrow{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap: 14px;
}

.iaid-hleft{
  display:flex;
  align-items:center;
  gap: 14px;
  min-width: 0;
}

.iaid-logo{
  width: 48px;
  height: 48px;
  border-radius: 14px;
  display:flex;
  align-items:center;
  justify-content:center;
  background: rgba(31,111,235,0.22);
  border: 1px solid rgba(90,162,255,0.40);
  font-weight: 950;
  font-size: 13px;
  flex: 0 0 auto;
  box-shadow: 0 0 16px rgba(31,111,235,0.25);
}

.iaid-meta{
  text-align:right;
  background: transparent !important;
  border: none !important;
  box-shadow: none !important;
  padding: 0 !important;
  border-radius: 0 !important;
  font-weight: 850;
  flex: 0 0 auto;
  min-width: 170px;
}

@media (max-width: 900px){
  .iaid-hrow{
    flex-direction: column;
    align-items: flex-start;
  }
  .iaid-meta{
    text-align:left;
    width: 100%;
  }
}



""",
unsafe_allow_html=True
)




# Paramètres + utilitaires déplacés dans `utils/data_pipeline.py`

# -----------------------------
# PDF (ReportLab)
# -----------------------------
def build_pdf_report(
    df: pd.DataFrame,
    title: str,
    mois_couverts: List[str],
    thresholds: dict,
    logo_bytes: Optional[bytes] = None,
    author_name: str = "",
    assistant_name: str = "",
    department: str = "",
    institution: str = "",
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
    # -----------------------------
    # COUVERTURE PRO (HEADER OFFICIEL)
    # -----------------------------
    now_dt = dt.datetime.now()
    date_gen = now_dt.strftime("%d/%m/%Y %H:%M")
    periode_str = " – ".join(mois_couverts) if mois_couverts else "—"

    # Tableau en-tête (logo + infos)
    logo_cell = ""
    if logo_bytes:
        try:
            img = RLImage(io.BytesIO(logo_bytes))
            img.drawHeight = 2.2*cm
            img.drawWidth  = 2.2*cm
            logo_cell = img
        except Exception:
            logo_cell = ""

    header_rows = [
        [
            logo_cell,
            Paragraph(
                f"""
                <b>{institution}</b><br/>
                {department}<br/>
                <font size="9" color="#475569">
                Rapport officiel de suivi des enseignements<br/>
                </font>
                """,
                P
            ),
            Paragraph(
                f"""
                <b>Date :</b> {date_gen}<br/>
                <b>Période :</b> {periode_str}<br/>
                <b>Référence :</b> {department.split('(')[-1].replace(')','').strip() or 'DEPT'}-SUIVI-{now_dt.strftime("%Y%m")}
                """,
                P
            )
        ]
    ]

    header_tbl = Table(header_rows, colWidths=[2.6*cm, 9.4*cm, 4.0*cm])
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("ALIGN", (0,0), (0,0), "LEFT"),
        ("ALIGN", (2,0), (2,0), "RIGHT"),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
    ]))

    story.append(header_tbl)

    # Bandeau titre (style "document officiel")
    banner = Table(
        [[Paragraph(f"<b>{title}</b>", ParagraphStyle("Banner", parent=H1, textColor=colors.white, fontSize=14))]],
        colWidths=[15.9*cm]
    )
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#0B3D91")),
        ("LEFTPADDING", (0,0), (-1,-1), 10),
        ("RIGHTPADDING", (0,0), (-1,-1), 10),
        ("TOPPADDING", (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(banner)

    story.append(Spacer(1, 10))

    # Bloc signatures (Auteur + Assistante)
    sign_tbl = Table(
        [[
            Paragraph(
                f"<b>Auteur :</b> {author_name}<br/>"
                f"<font size='8' color='#475569'>{CFG['author_role']}</font>",
                P,
            ),
            Paragraph(
                f"<b>{CFG['assistant_label']} :</b> {assistant_name}<br/>"
                f"<font size='8' color='#475569'>{CFG['assistant_role']}</font>",
                P,
            ),
        ]],
        colWidths=[7.9*cm, 8.0*cm]
    )
    sign_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#F6F8FC")),
        ("BOX", (0,0), (-1,-1), 0.4, colors.HexColor("#E3E8F0")),
        ("INNERGRID", (0,0), (-1,-1), 0.25, colors.HexColor("#E3E8F0")),
        ("LEFTPADDING", (0,0), (-1,-1), 10),
        ("RIGHTPADDING", (0,0), (-1,-1), 10),
        ("TOPPADDING", (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
    ]))
    story.append(sign_tbl)

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
        for _, r in gg.head(20).iterrows():
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

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#475569"))
        canvas.drawString(1.6*cm, 1.0*cm, f"{department} — Rapport de suivi des enseignements")
        canvas.drawRightString(19.4*cm, 1.0*cm, f"Généré le {dt.datetime.now().strftime('%d/%m/%Y %H:%M')}  |  Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)

    return out.getvalue()
def build_pdf_observations_report(
    df: pd.DataFrame,
    title: str,
    mois_couverts: List[str],
    logo_bytes: Optional[bytes] = None,
    author_name: str = "",
    assistant_name: str = "",
    department: str = "",
    institution: str = "",
    max_rows_per_class: int = 9999,  # mets 18 si tu veux limiter
) -> bytes:
    styles = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=16, spaceAfter=10)
    H2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=12, spaceAfter=6)
    P  = ParagraphStyle("P", parent=styles["BodyText"], fontSize=9, leading=12)
    Small = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=8, leading=10)

    # =========================================================
    # ✅ WRAP PDF (Observations) — Anti chevauchement + texte long
    # =========================================================
    from reportlab.lib.enums import TA_LEFT

    CELL = ParagraphStyle(
        "CELL_OBS",
        parent=styles["BodyText"],
        fontSize=8.2,
        leading=10.5,
        spaceBefore=0,
        spaceAfter=0,
        alignment=TA_LEFT,
        wordWrap="CJK",   # wrap robuste (mots longs)
    )

    HEAD = ParagraphStyle(
        "HEAD_OBS",
        parent=styles["BodyText"],
        fontSize=8.4,
        leading=10,
        spaceBefore=0,
        spaceAfter=0,
        alignment=TA_LEFT,
        textColor=colors.white,
    )

    def _esc(x) -> str:
        s = "" if x is None else str(x)
        return (
            s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
        )

    def Pcell(x, allow_br: bool = True) -> Paragraph:
        """
        Cellule PDF WRAP : support retours ligne via <br/>.
        """
        s = _esc(x).strip()
        if allow_br:
            s = s.replace("\n", "<br/>")
        return Paragraph(s if s else "—", CELL)

    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out,
        pagesize=A4,
        leftMargin=1.6*cm, rightMargin=1.6*cm,
        topMargin=1.4*cm, bottomMargin=1.4*cm
    )

    story = []

    # -----------------------------
    # Filtrage : uniquement lignes avec Observations
    # -----------------------------
    d = df.copy()
    if "Observations" not in d.columns:
        d["Observations"] = ""

    d["Observations"] = (
        d["Observations"].astype(str)
        .replace({"nan": "", "None": ""})
        .fillna("")
        .str.replace("\r", "", regex=False)  # ✅ garder \n (retours ligne)
        .str.strip()
    )

    d = d[d["Observations"].str.len() > 0].copy()

    now_dt = dt.datetime.now()
    date_gen = now_dt.strftime("%d/%m/%Y %H:%M")
    periode_str = " – ".join(mois_couverts) if mois_couverts else "—"

    # -----------------------------
    # Couverture officielle (même style que ton PDF principal)
    # -----------------------------
    logo_cell = ""
    if logo_bytes:
        try:
            img = RLImage(io.BytesIO(logo_bytes))
            img.drawHeight = 2.2*cm
            img.drawWidth  = 2.2*cm
            logo_cell = img
        except Exception:
            logo_cell = ""

    header_rows = [[
        logo_cell,
        Paragraph(
            f"""
            <b>{institution}</b><br/>
            {department}<br/>
            <font size="9" color="#475569">
            Rapport officiel — Suivi des enseignements (Observations)<br/>
            </font>
            """,
            P
        ),
        Paragraph(
            f"""
            <b>Date :</b> {date_gen}<br/>
            <b>Période :</b> {periode_str}<br/>
            <b>Référence :</b> {department.split('(')[-1].replace(')','').strip() or 'DEPT'}-OBS-{now_dt.strftime("%Y%m")}
            """,
            P
        )
    ]]

    header_tbl = Table(header_rows, colWidths=[2.6*cm, 9.4*cm, 4.0*cm])
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("ALIGN", (0,0), (0,0), "LEFT"),
        ("ALIGN", (2,0), (2,0), "RIGHT"),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
    ]))
    story.append(header_tbl)

    banner = Table(
        [[Paragraph(f"<b>{title}</b>", ParagraphStyle("Banner", parent=H1, textColor=colors.white, fontSize=14))]],
        colWidths=[15.9*cm]
    )
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#0B3D91")),
        ("LEFTPADDING", (0,0), (-1,-1), 10),
        ("RIGHTPADDING", (0,0), (-1,-1), 10),
        ("TOPPADDING", (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(banner)
    story.append(Spacer(1, 10))

    sign_tbl = Table(
        [[
            Paragraph(
                f"<b>Auteur :</b> {author_name}<br/>"
                f"<font size='8' color='#475569'>{CFG['author_role']}</font>",
                P,
            ),
            Paragraph(
                f"<b>{CFG['assistant_label']} :</b> {assistant_name}<br/>"
                f"<font size='8' color='#475569'>{CFG['assistant_role']}</font>",
                P,
            ),
        ]],
        colWidths=[7.9*cm, 8.0*cm]
    )
    sign_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#F6F8FC")),
        ("BOX", (0,0), (-1,-1), 0.4, colors.HexColor("#E3E8F0")),
        ("INNERGRID", (0,0), (-1,-1), 0.25, colors.HexColor("#E3E8F0")),
        ("LEFTPADDING", (0,0), (-1,-1), 10),
        ("RIGHTPADDING", (0,0), (-1,-1), 10),
        ("TOPPADDING", (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
    ]))
    story.append(sign_tbl)
    story.append(Spacer(1, 12))

    # -----------------------------
    # Si aucune observation
    # -----------------------------
    if d.empty:
        story.append(Paragraph("Aucune observation renseignée sur la période sélectionnée.", P))
        story.append(Paragraph("Le suivi des enseignements par observations ne peut pas être établi sans commentaires.", Small))

        def _footer(canvas, doc_):
            canvas.saveState()
            canvas.setFont("Helvetica", 8)
            canvas.setFillColor(colors.HexColor("#475569"))
            canvas.drawString(1.6*cm, 1.0*cm, f"{department} — Suivi des enseignements (Observations)")
            canvas.drawRightString(19.4*cm, 1.0*cm, f"Généré le {dt.datetime.now().strftime('%d/%m/%Y %H:%M')}  |  Page {doc_.page}")
            canvas.restoreState()

        doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
        return out.getvalue()

    # -----------------------------
    # Synthèse (KPIs)
    # -----------------------------
    if "Classe" not in d.columns:
        d["Classe"] = "—"
    if "Responsable" not in d.columns:
        d["Responsable"] = "—"

    total_obs = len(d)
    nb_classes = int(d["Classe"].nunique())
    nb_resp = int(d["Responsable"].nunique())

    kpi_table = Table(
        [
            ["Modules avec observation", "Classes concernées", "Responsables concernés"],
            [str(total_obs), str(nb_classes), str(nb_resp)],
        ],
        colWidths=[5.2*cm, 5.2*cm, 5.5*cm],
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
    story.append(Spacer(1, 10))
    story.append(Paragraph("Détail — Observations par classe", H1))

    # -----------------------------
    # Détail par classe
    # -----------------------------
    sort_cols = ["Classe"]
    if "Écart" in d.columns:
        sort_cols += ["Écart"]
    d = d.sort_values(sort_cols, ascending=[True] + ([True] if "Écart" in d.columns else []))

    for classe, g in d.groupby("Classe"):
        story.append(Paragraph(f"Classe : {classe}", H2))

        gg = g.copy()
        if max_rows_per_class and max_rows_per_class > 0:
            gg = gg.head(max_rows_per_class)

        # ✅ Table WRAP : Paragraph dans toutes les cellules texte
        rows = [[
            Paragraph("<b>Sem</b>", HEAD),
            Paragraph("<b>Type</b>", HEAD),
            Paragraph("<b>Matière</b>", HEAD),
            Paragraph("<b>Responsable</b>", HEAD),
            Paragraph("<b>Observation</b>", HEAD),
        ]]

        for _, r in gg.iterrows():
            sem  = r.get("Semestre", "")
            typ  = r.get("Type", "")
            mat  = r.get("Matière", "")
            resp = r.get("Responsable", "")
            obs  = r.get("Observations", "")  # ✅ PAS TRONQUÉ

            rows.append([
                Pcell(sem,  allow_br=False),
                Pcell(typ,  allow_br=False),
                Pcell(mat,  allow_br=True),
                Pcell(resp, allow_br=True),
                Pcell(obs,  allow_br=True),
            ])

        t = Table(
            rows,
            colWidths=[1.0*cm, 1.5*cm, 4.0*cm, 3.1*cm, 6.3*cm],  # total ~ 15.9cm
            repeatRows=1,
            splitByRow=1,  # ✅ découpage multi-pages
        )

        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0B3D91")),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),

            ("FONTSIZE", (0,0), (-1,-1), 8),
            ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#D7DEE8")),
            ("VALIGN", (0,0), (-1,-1), "TOP"),

            ("LEFTPADDING", (0,0), (-1,-1), 6),
            ("RIGHTPADDING", (0,0), (-1,-1), 6),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ]))

        story.append(t)
        story.append(Spacer(1, 10))

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#475569"))
        canvas.drawString(1.6*cm, 1.0*cm, f"{department} — Suivi des enseignements (Observations)")
        canvas.drawRightString(19.4*cm, 1.0*cm, f"Généré le {dt.datetime.now().strftime('%d/%m/%Y %H:%M')}  |  Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return out.getvalue()

with st.sidebar:
    LOGO_JPG = Path(CFG["logo_path"])

    if LOGO_JPG.exists():
        st.markdown('<div class="sidebar-logo-wrap">', unsafe_allow_html=True)
        st.image(str(LOGO_JPG))
        st.markdown('</div>', unsafe_allow_html=True)
    else:
        st.markdown(
            f"""
            <div class="sidebar-logo-wrap" style="font-weight:950;color:#0B3D91;font-size:18px;">
            {CFG["dept_code"]}
            </div>
            """,
            unsafe_allow_html=True
        )


    st.divider()



    # =========================================================
    # 1) IMPORT & PARAMETRES
    # =========================================================
    sidebar_card("Import & Paramètres")

    import_mode = st.radio("Mode d'import", ["URL (auto)", "Upload (manuel)"], index=0)

    file_bytes = None
    source_label = None

    st.caption("Chaque feuille = une classe. Colonnes attendues : Matière, VHP, Oct..Août (au minimum).")
    sidebar_card_end()

    # =========================================================
    # 2) AUTO-REFRESH + CHARGEMENT (URL / UPLOAD)
    # =========================================================
    sidebar_card("Auto-refresh & Source")

    auto_refresh = st.checkbox("Rafraîchir automatiquement (URL)", value=False)  # ✅ OFF par défaut
    refresh_sec = st.slider("Intervalle (secondes)", 30, 900, 300, 30)          # ✅ 300s conseillé

    # 1) Heartbeat de rerun
    tick = 0
    if import_mode == "URL (auto)" and auto_refresh:
        tick = st_autorefresh(interval=refresh_sec * 1000, key="iaid_refresh_tick")


    if st.button("🔄 Rafraîchir maintenant"):
        st.cache_data.clear()
        st.rerun()


    if import_mode == "URL (auto)":
        st.caption("Recommandé Streamlit Cloud : lien direct vers un fichier .xlsx")
        default_url = str(safe_secret(CFG["secrets"]["excel_url"], ""))
        url = st.text_input("URL du fichier Excel (.xlsx)", value=default_url)

        if url.strip():
            try:
                window = int(time.time() // max(1, refresh_sec))
                cache_bust = f"tick={tick}-w={window}"

                h = fetch_headers(url.strip(), cache_bust)

                etag = (h.get("ETag") or "").strip()
                lm   = (h.get("Last-Modified") or "").strip()

                signature = etag or lm or f"w={window}"

                file_bytes = fetch_excel_if_changed(url.strip(), signature)
                source_label = f"URL smart ({signature})"
                digest = hashlib.md5(file_bytes).hexdigest()[:10]
                st.caption(f"📦 URL: {len(file_bytes)/1024:.1f} KB | md5: {digest} | tick={tick}")


            except Exception as e:
                st.error(f"Erreur téléchargement: {e}")




    else:
        uploaded = st.file_uploader("Importer le fichier Excel (.xlsx)", type=["xlsx"])
        if uploaded is not None:
            file_bytes = uploaded.getvalue()
            digest = hashlib.md5(file_bytes).hexdigest()[:10]
            st.caption(f"📦 Fichier: {len(file_bytes)/1024:.1f} KB | md5: {digest}")
            source_label = f"Upload: {uploaded.name}"

    sidebar_card_end()

    # =========================================================
    # 3) PERIODE COUVERTE
    # =========================================================
    sidebar_card("Période couverte")

    mois_min, mois_max = st.select_slider(
    "Mois (de → à)",
    options=MOIS_COLS,
    value=("Oct", "Août"),)

    mois_couverts = MOIS_COLS[MOIS_COLS.index(mois_min): MOIS_COLS.index(mois_max) + 1]

    sidebar_card_end()

    # =========================================================
    # 4) SEUILS D’ALERTE
    # =========================================================
    sidebar_card("Seuils d’alerte")

    taux_vert = st.slider(
        "Seuil Vert (Terminé/OK)",
        0.50, 1.00,
        float(DEFAULT_THRESHOLDS["taux_vert"]),
        0.05
    )
    taux_orange = st.slider(
        "Seuil Orange (Attention)",
        0.10, 0.95,
        float(DEFAULT_THRESHOLDS["taux_orange"]),
        0.05
    )
    ecart_critique = st.slider(
        "Écart critique (heures)",
        -40, 0,
        int(DEFAULT_THRESHOLDS["ecart_critique"]),
        1
    )

    sidebar_card_end()

    # =========================================================
    # 5) BRANDING
    # =========================================================
    sidebar_card("Branding")

    logo = st.file_uploader("Logo (PNG/JPG) pour le PDF", type=["png", "jpg", "jpeg"])

    sidebar_card_end()

    # =========================================================
    # 6) EXPORT
    # =========================================================
    # =========================================================
    # 6) EXPORTS
    # =========================================================
    sidebar_card("Exports")

    st.caption("Nom des fichiers générés (Excel / PDF).")

    export_prefix = st.text_input(
        "Préfixe export",
        value="Suivi_Classes",
    )

    sidebar_card_end()


    # =========================================================
    # 7) RAPPEL DG/DGE (MENSUEL)
    # =========================================================
    sidebar_card("📩 Rappel DG/DGE (mensuel)")

    dashboard_url = str(safe_secret(CFG["secrets"]["dashboard_url"], ""))
    recips_raw = str(safe_secret(CFG["secrets"]["dg_emails"], ""))
    recipients = [x.strip() for x in recips_raw.split(",") if x.strip()]

    today = dt.date.today()
    month_key = today.strftime("%Y-%m")  # ex: 2026-01

    # --- Sécurité admin ---
    pin = st.text_input("Code admin (PIN)", type="password").strip()
    admin_pin = str(safe_secret(CFG["secrets"]["admin_pin"], "")).strip()
    is_admin = (pin != "" and admin_pin != "" and pin == admin_pin)

    # rendre dispo partout (onglets)
    st.session_state["is_admin"] = is_admin


    subject = f"{CFG['email_prefix']} — Rappel mensuel de pilotage des enseignements ({today.strftime('%m/%Y')})"
    body_text = f"""
    {CFG["department_long"]}
    Notification mensuelle — Pilotage des enseignements • {today.strftime('%m/%Y')}
    Mise à jour : {dt.datetime.now().strftime('%d/%m/%Y %H:%M')}

    Madame la Directrice,

    Dans le cadre du suivi mensuel du pilotage académique, nous vous transmettons l’accès au Dashboard {CFG["dept_code"]}, plateforme institutionnelle permettant un suivi consolidé et continu des activités pédagogiques du département.

    Ce tableau de bord permet notamment :
    - Le suivi de l’état d’avancement des enseignements par classe et par matière
    - L’analyse des volumes horaires prévus et réalisés
    - L’identification des situations nécessitant une attention particulière (retards, non démarrés, écarts critiques)
    - L’accès à des indicateurs synthétiques facilitant le pilotage décisionnel
    - La génération de rapports consolidés (PDF officiels et exports Excel)

    Ouvrir le Dashboard {CFG["dept_code"]} →
    {dashboard_url}

    📌 Informations clés
    Période : {today.strftime('%m/%Y')}
    Lien : {dashboard_url}
    """.strip()


    body_html = f"""
    <!doctype html>
    <html>
    <body style="margin:0;padding:0;background:#0B3D91;">
        
        <!-- BACKGROUND BLEU -->
        <div style="
            background:linear-gradient(180deg,#0B3D91 0%,#134FA8 100%);
            padding:40px 12px;
        ">

        <!-- CARTE BLANCHE -->
        <div style="
            max-width:720px;
            margin:0 auto;
            background:#FFFFFF;
            border-radius:20px;
            box-shadow:0 20px 50px rgba(0,0,0,0.25);
            overflow:hidden;
            font-family:Arial,Helvetica,sans-serif;
            color:#0F172A;
        ">

            <!-- EN-TÊTE -->
            <div style="
                padding:22px 26px;
                background:linear-gradient(90deg,#0B3D91,#1F6FEB);
                color:#FFFFFF;
            ">
            <div style="font-size:18px;font-weight:900;">
                {CFG["department_long"]}
            </div>
            <div style="margin-top:6px;font-size:13px;font-weight:700;opacity:.95;">
                Notification mensuelle — Pilotage des enseignements • {today.strftime('%m/%Y')}
            </div>
            <div style="margin-top:6px;font-size:12px;font-weight:700;opacity:.9;">
                Mise à jour : {dt.datetime.now().strftime('%d/%m/%Y %H:%M')}
            </div>
            </div>

      
         <!-- CONTENU -->
            <div style="padding:26px;line-height:1.55;">

            <p style="margin-top:0;">
                Madame la Directrice,
            </p>

            <p>
                Dans le cadre du <b>suivi mensuel du pilotage académique</b>, nous vous transmettons l’accès au
                <b>Dashboard {CFG["dept_code"]}</b>, plateforme institutionnelle permettant un suivi consolidé et continu des activités pédagogiques du département.
            </p>

            <p style="margin:0;">
                Ce tableau de bord permet notamment :
            </p>

            <ul style="margin:10px 0 0 18px;padding:0;">
                <li>Le suivi de l’état d’avancement des enseignements par classe et par matière</li>
                <li>L’analyse des volumes horaires prévus et réalisés (VHP / VHR)</li>
                <li>L’identification des situations nécessitant une attention particulière (retards, non démarrés, écarts critiques)</li>
                <li>L’accès à des indicateurs synthétiques facilitant le pilotage décisionnel</li>
                <li>La génération de rapports consolidés (PDF officiels et exports Excel)</li>
            </ul>

            <!-- BOUTON (bleu) -->
            <div style="margin:22px 0;text-align:center;">
                <a href="{dashboard_url}" style="
                display:inline-block;
                background:#0B3D91;
                color:#FFFFFF !important;
                text-decoration:none;
                padding:14px 22px;
                border-radius:14px;
                font-weight:900;
                font-size:14px;
                box-shadow:0 10px 24px rgba(14,30,37,0.25);
                ">
                Ouvrir le Dashboard {CFG["dept_code"]} →
                </a>
            </div>


            <!-- INFOS CLÉS -->
            <div style="
                margin-top:24px;
                background:#F6F8FC;
                border:1px solid #E3E8F0;
                border-radius:14px;
                padding:14px 16px;
            ">
                <div style="font-weight:900;color:#0B3D91;margin-bottom:8px;">
                📌 Informations clés
                </div>
                <div style="font-size:13px;"><b>Période :</b> {today.strftime('%m/%Y')}</div>
                <div style="font-size:13px;">
                <b>Lien :</b>
                <a href="{dashboard_url}" style="color:#1F6FEB;text-decoration:none;">
                    {dashboard_url}
                </a>
                </div>
            </div>

            </div>

            <!-- FOOTER -->
            <div style="
                padding:14px 26px;
                background:#FBFCFF;
                border-top:1px solid #E3E8F0;
                font-size:12px;
                color:#475569;
                text-align:center;
            ">
            Message automatique — {CFG["department_long"]}
            </div>

        </div>
        </div>
    </body>
    </html>
    """.strip()





    def do_send():
        # 1) lock anti double-envoi
        set_lock(month_key)

        try:
            cfg_smtp = _get_smtp_config()
            send_email_reminder(
                smtp_host=cfg_smtp["smtp_host"],
                smtp_port=cfg_smtp["smtp_port"],
                smtp_user=cfg_smtp["smtp_user"],
                smtp_pass=cfg_smtp["smtp_pass"],
                sender=cfg_smtp["smtp_from"],
                recipients=recipients,
                subject=subject,
                body_text=body_text,
                body_html=body_html,
            )
            # 2) marquer envoyé pour le mois
            set_last_reminder_month(month_key)

        finally:
            # 3) libérer le lock même en cas d'erreur
            clear_lock()


    if st.button("Envoyer le rappel maintenant"):
        if not is_admin:
            st.error("Accès refusé : PIN incorrect.")
        elif not recipients:
            st.error("DG_EMAILS est vide dans st.secrets.")
        elif lock_is_active(month_key):
            st.warning("Un envoi est déjà en cours (anti double-envoi).")
        else:
            try:
                do_send()
                st.success("Rappel envoyé ✅")
            except Exception as e:
                st.error(f"Erreur envoi: {e}")


    sidebar_card_end()


# =========================================================
# ✅ HEADER (CLEAN) — zéro code affiché, zéro string parasite
# =========================================================

now_str = dt.datetime.now().strftime("%d/%m/%Y %H:%M")

st.markdown(
f"""
<div class="iaid-header">
  <div class="iaid-hrow">
    <div class="iaid-hleft">
      <div class="iaid-logo">{CFG["dept_code"]}</div>
      <div>
        <div class="iaid-htitle">{CFG["header_title"]}</div>
        <div class="iaid-hsub">{CFG["header_subtitle"]}</div>
      </div>
    </div>

    
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





st.markdown(
f"""
<div class="footer-signature">
  <strong>{CFG["author_name"]}</strong> — {CFG["author_role"]} • ✉️ {CFG["author_email"]}
  <br/>
  <strong>{CFG["assistant_label"]} :</strong> {CFG["assistant_name"]} • ✉️ {CFG["assistant_email"]}
</div>
""",
unsafe_allow_html=True
)



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

# =========================
# FIX RESPONSABLE (IMPORTANT)
# =========================
df_period["Responsable"] = df_period["Responsable"].astype(str).replace({"nan":"", "None":""}).fillna("").str.strip()
df_period["Responsable"] = df_period["Responsable"].replace({"": "⚠️ Non affecté"})

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

# -----------------------------
# Filtre Responsable (enseignant) — robuste
# -----------------------------
responsables = sorted(df_period["Responsable"].unique().tolist())
selected_responsables = st.sidebar.multiselect(
    "Responsables (enseignants)",
    responsables,
    default=responsables
) if responsables else []



search_matiere = st.sidebar.text_input("Recherche Matière (regex)", value="")
show_only_delay = st.sidebar.checkbox("Uniquement retards (Écart < 0)", value=False)
min_vhp = st.sidebar.number_input("VHP min", min_value=0.0, value=0.0, step=1.0)
# -----------------------------
# Dataset BASE : ne dépend PAS des filtres Enseignant/Type
# -----------------------------
filtered_base = df_period[
    df_period["Classe"].isin(selected_classes)
    & df_period["Statut_auto"].isin(selected_status)
    & (df_period["VHP"] >= min_vhp)
].copy()

# Appliquer le filtre Responsable seulement si l’utilisateur a réduit la sélection
if selected_responsables and set(selected_responsables) != set(responsables):
    filtered_base = filtered_base[filtered_base["Responsable"].isin(selected_responsables)]



# Semestre
if selected_semestre is not None:
    filtered_base = filtered_base[filtered_base["Semestre_norm"] == selected_semestre]

# Recherche matière
if search_matiere.strip():
    try:
        filtered_base = filtered_base[
            filtered_base["Matière"].str.contains(search_matiere, case=False, regex=True, na=False)
        ]
    except re.error:
        st.sidebar.warning("Regex invalide — recherche ignorée.")

# Retards seulement
if show_only_delay:
    filtered_base = filtered_base[filtered_base["Écart"] < 0]

# -----------------------------
# Dataset final (sans Enseignant/Type)
# -----------------------------
filtered = filtered_base.copy()


# ✅ Classes réellement disponibles après filtres (important pour l'onglet "Par classe")
classes_filtered = sorted(filtered["Classe"].dropna().unique().tolist())
if not classes_filtered:
    # fallback si filtre vide
    classes_filtered = sorted(df_period["Classe"].dropna().unique().tolist())


# -----------------------------
# Onglets (Ultra)
# -----------------------------
tab_overview, tab_classes, tab_matieres, tab_enseignants, tab_mensuel, tab_alertes, tab_qualite, tab_export = st.tabs(
    ["Vue globale", "Par classe", "Par matière", "Par enseignant", "Analyse mensuelle", "Alertes", "Qualité des données", "Exports"]
)


# ====== VUE GLOBALE ======
with tab_overview:
    st.subheader("KPIs globaux (période sélectionnée)")

    # ----- Calculs KPI (DOIT être AVANT le HTML) -----
    total = int(len(filtered))
    taux_moy = float(filtered["Taux"].mean() * 100) if total else 0.0
    nb_term = int((filtered["Statut_auto"] == "Terminé").sum())
    nb_enc  = int((filtered["Statut_auto"] == "En cours").sum())
    nb_nd   = int((filtered["Statut_auto"] == "Non démarré").sum())
    retard_total = float(filtered.loc[filtered["Écart"] < 0, "Écart"].sum()) if total else 0.0

    # ----- KPI en cartes HTML -----
    retard_class = "kpi-good"
    if retard_total < 0:
        retard_class = "kpi-bad"
    elif retard_total == 0:
        retard_class = "kpi-warn"

    st.markdown(
        f"""
        <div class="kpi-grid">
          <div class="kpi kpi-good">
            <div class="kpi-title">Matières</div>
            <div class="kpi-value">{total}</div>
          </div>

          <div class="kpi kpi-warn">
            <div class="kpi-title">Taux moyen</div>
            <div class="kpi-value">{taux_moy:.1f}%</div>
          </div>

          <div class="kpi kpi-good">
            <div class="kpi-title">Terminées</div>
            <div class="kpi-value">{nb_term}</div>
          </div>

          <div class="kpi kpi-warn">
            <div class="kpi-title">En cours</div>
            <div class="kpi-value">{nb_enc}</div>
          </div>

          <div class="kpi {retard_class}">
            <div class="kpi-title">Retard cumulé (h)</div>
            <div class="kpi-value">{retard_total:.0f}</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    st.divider()

    st.write("### Avancement moyen par classe")
    g = filtered.groupby("Classe")["Taux"].mean().sort_values(ascending=False).reset_index()
    g["Taux (%)"] = (g["Taux"] * 100).round(1)

    st.dataframe(
        g[["Classe", "Taux (%)"]],
        use_container_width=True,
        column_config={
            "Taux (%)": st.column_config.ProgressColumn(
                "Taux (%)", min_value=0.0, max_value=100.0, format="%.1f%%"
            )
        }
    )

    fig = px.bar(g, x="Classe", y="Taux (%)", title="Avancement moyen (%) par classe")
    fig.update_layout(height=420, margin=dict(l=10, r=10, t=60, b=10))
    st.plotly_chart(fig, use_container_width=True)

    st.write("### Répartition des statuts")
    stat = filtered["Statut_auto"].value_counts().reset_index()
    stat.columns = ["Statut", "Nombre"]
    fig = px.pie(stat, names="Statut", values="Nombre", title="Répartition des statuts")
    fig.update_layout(height=420, margin=dict(l=10, r=10, t=60, b=10))
    st.plotly_chart(fig, use_container_width=True)

    # =========================================================
    # ✅ TOP RETARDS (st.dataframe + emojis) — VERSION PRO
    # =========================================================
    st.write("### Top retards (Écart le plus négatif)")

    top_retards = filtered.sort_values("Écart").head(20)[
        ["Classe", "Matière", "VHP", "VHR", "Écart", "Taux", "Statut_auto", "Observations"]
    ].copy()

    # ✅ Ajout colonnes lisibles
    top_retards["Taux (%)"] = (top_retards["Taux"] * 100).round(1)
    top_retards["Statut"] = top_retards["Statut_auto"].apply(statut_badge_text)

    st.dataframe(
        top_retards[["Classe", "Matière", "VHP", "VHR", "Écart", "Taux (%)", "Statut", "Observations"]],
        use_container_width=True,
        height=420,
        column_config={
            "Taux (%)": st.column_config.ProgressColumn(
                "Taux (%)", min_value=0.0, max_value=100.0, format="%.1f%%"
            ),
            "Écart": st.column_config.NumberColumn("Écart (h)", format="%.0f"),
            "VHP": st.column_config.NumberColumn("VHP", format="%.0f"),
            "VHR": st.column_config.NumberColumn("VHR", format="%.0f"),
            "Statut": st.column_config.TextColumn("Statut"),
        }
    )





# ====== PAR CLASSE ======
with tab_classes:
    st.subheader("Drilldown par classe + comparaison")

    colA, colB = st.columns([2, 1])
    with colB:
        cls1 = st.selectbox("Comparer classe A", classes_filtered, index=0)
        cls2 = st.selectbox(
            "avec classe B",
            classes_filtered,
            index=min(1, len(classes_filtered) - 1) if len(classes_filtered) > 1 else 0
        )


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
        ).reset_index()

        synth_view = synth.copy()
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
    st.write(f"### Détails — {cls1} vs {cls2} (KPIs)")
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

    st.write(f"### Retards (Top 15) — {cls1}")
    tA = A.sort_values("Écart").head(15)[
    ["Matière","VHP","VHR","Écart","Taux","Statut_auto","Observations"]
    ].copy()

    tA["Taux (%)"] = (tA["Taux"] * 100).round(1)
    tA["Statut"] = tA["Statut_auto"].apply(statut_badge_text)

    st.dataframe(
        tA[["Matière","VHP","VHR","Écart","Taux (%)","Statut","Observations"]],
        use_container_width=True,
        column_config={
            "Taux (%)": st.column_config.ProgressColumn(
                "Taux (%)", min_value=0.0, max_value=100.0, format="%.1f%%"
            ),
            "Écart": st.column_config.NumberColumn("Écart (h)", format="%.0f"),
            "VHP": st.column_config.NumberColumn("VHP", format="%.0f"),
            "VHR": st.column_config.NumberColumn("VHR", format="%.0f"),
            "Statut": st.column_config.TextColumn("Statut"),
        }
    )



    st.write(f"### Retards (Top 15) — {cls2}")
    tB = B.sort_values("Écart").head(15)[
    ["Matière","VHP","VHR","Écart","Taux","Statut_auto","Observations"]
    ].copy()

    tB["Taux (%)"] = (tB["Taux"] * 100).round(1)
    tB["Statut"] = tB["Statut_auto"].apply(statut_badge_text)

    st.dataframe(
        tB[["Matière","VHP","VHR","Écart","Taux (%)","Statut","Observations"]],
        use_container_width=True,
        column_config={
            "Taux (%)": st.column_config.ProgressColumn(
                "Taux (%)", min_value=0.0, max_value=100.0, format="%.1f%%"
            ),
            "Écart": st.column_config.NumberColumn("Écart (h)", format="%.0f"),
            "VHP": st.column_config.NumberColumn("VHP", format="%.0f"),
            "VHR": st.column_config.NumberColumn("VHR", format="%.0f"),
            "Statut": st.column_config.TextColumn("Statut"),
        }
    )




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


# ====== PAR ENSEIGNANT ======
with tab_enseignants:
    st.subheader("Suivi par enseignant (Responsable) — retards & charge")

    tmp = filtered.copy()

    if "Responsable" not in tmp.columns:
        st.warning("La colonne 'Responsable' n'existe pas dans les données.")
    else:
        tmp["Responsable"] = (
            tmp["Responsable"].astype(str)
            .replace({"nan": "", "None": ""})
            .fillna("")
            .str.strip()
        )

        # Inclure les modules non affectés (utile)
        tmp["Responsable"] = tmp["Responsable"].replace({"": "⚠️ Non affecté"})

        # 1) Synthèse par enseignant
        synth_r = tmp.groupby("Responsable").agg(
            Matieres=("Matière", "count"),
            Classes=("Classe", "nunique"),
            VHP_total=("VHP", "sum"),
            VHR_total=("VHR", "sum"),
            Taux_moy=("Taux", "mean"),
            Retard_h=("Écart", lambda s: float(s[s < 0].sum())),
            Non_demarre=("Statut_auto", lambda s: int((s == "Non démarré").sum())),
            En_cours=("Statut_auto", lambda s: int((s == "En cours").sum())),
            Termine=("Statut_auto", lambda s: int((s == "Terminé").sum())),
        ).reset_index()

        synth_r["Taux (%)"] = (synth_r["Taux_moy"] * 100).round(1)

        # tri : retard le plus critique d'abord (plus négatif)
        synth_r = synth_r.sort_values(["Retard_h", "Taux (%)"], ascending=[True, True])

        st.write("### Synthèse par enseignant")
        st.dataframe(
            synth_r[["Responsable","Matieres","Classes","Taux (%)","VHP_total","VHR_total","Retard_h","Non_demarre","En_cours","Termine"]],
            use_container_width=True,
            column_config={
                "Taux (%)": st.column_config.ProgressColumn("Taux (%)", min_value=0.0, max_value=100.0, format="%.1f%%"),
                "Retard_h": st.column_config.NumberColumn("Retard (h)", format="%.0f"),
                "VHP_total": st.column_config.NumberColumn("VHP total", format="%.0f"),
                "VHR_total": st.column_config.NumberColumn("VHR total", format="%.0f"),
            }
        )

        st.divider()

        # 2) Top retards (détails)
        st.write("### Top retards — détails par enseignant")
        top_n = st.slider("Nombre de lignes (Top retards)", 10, 200, 50, 10, key="top_retards_ens")

        top_ret = tmp[tmp["Écart"] < 0].sort_values("Écart").head(top_n)[
            ["Responsable","Classe","Matière","Semestre","VHP","VHR","Écart","Taux","Statut_auto","Observations"]
        ].copy()

        st.dataframe(
            top_ret,
            use_container_width=True,
            column_config={
                "Taux": st.column_config.ProgressColumn("Taux", min_value=0.0, max_value=1.0, format="%.0f%%"),
                "Écart": st.column_config.NumberColumn("Écart (h)", format="%.0f"),
                "VHP": st.column_config.NumberColumn("VHP", format="%.0f"),
                "VHR": st.column_config.NumberColumn("VHR", format="%.0f"),
            }
        )

        st.divider()

        # 3) Non démarrés par enseignant
        st.write("### Non démarrés — par enseignant")
        nd = tmp[tmp["Statut_auto"] == "Non démarré"].groupby("Responsable").size().sort_values(ascending=False)
        if nd.empty:
            st.success("Aucun 'Non démarré' avec les filtres actuels ✅")
        else:
            st.bar_chart(nd)

        st.divider()

        # 4) Charge par enseignant
        st.write("### Charge par enseignant — VHP prévu vs VHR réalisé")
        charge = tmp.groupby("Responsable").agg(
            VHP_total=("VHP", "sum"),
            VHR_total=("VHR", "sum"),
        ).reset_index()
        charge["Écart_total"] = charge["VHR_total"] - charge["VHP_total"]
        charge = charge.sort_values("Écart_total")

        st.dataframe(
            charge,
            use_container_width=True,
            column_config={
                "VHP_total": st.column_config.NumberColumn("VHP total", format="%.0f"),
                "VHR_total": st.column_config.NumberColumn("VHR total", format="%.0f"),
                "Écart_total": st.column_config.NumberColumn("Écart (h)", format="%.0f"),
            }
        )


# ====== ANALYSE MENSUELLE ======
with tab_mensuel:
    st.subheader("Analyse mensuelle — heures réalisées & tendances")

    long = make_long(df_period)
    # Appliquer filtres classes/statuts à la table longue via merge index
    ids = set(filtered["_rowid"].unique())
    long_f = long[long["_rowid"].isin(ids)]



    # Heures par mois (total)
    monthly = long_f.groupby("Mois").agg(Heures=("Heures","sum")).reindex(MOIS_COLS).fillna(0)
    st.write("### Heures totales par mois (filtre actif)")
    st.line_chart(monthly)

    # Heures par classe et mois (heat-like table)
    st.write("### Matrice Classe × Mois (heures)")
    pivot = long_f.pivot_table(index="Classe", columns="Mois", values="Heures", aggfunc="sum", fill_value=0).reindex(columns=MOIS_COLS)
    st.dataframe(style_table(pivot.reset_index()), use_container_width=True)

    cells = pivot.shape[0] * pivot.shape[1]  # nb classes * nb mois
    if cells > 250:
        st.info("Heatmap désactivée (trop de données) → filtre quelques classes.")
    else:
        fig = px.imshow(
            pivot.values,
            x=pivot.columns,
            y=pivot.index,
            aspect="auto",
            title="Heatmap — Heures par classe et par mois"
        )
        st.plotly_chart(fig, use_container_width=True)



    st.write("### Classe la plus active par mois")

    if pivot.empty:
        st.warning("Aucune donnée mensuelle disponible avec les filtres actuels.")
    else:
        pivot_num = pivot.apply(pd.to_numeric, errors="coerce")

        if pivot_num.isna().all().all():
            st.warning("Aucune valeur numérique exploitable pour déterminer la classe top par mois.")
        else:
            top_by_month = pivot_num.idxmax(axis=0).to_frame(name="Classe top").T
            st.dataframe(top_by_month, use_container_width=True)


# ====== ALERTES ======
with tab_alertes:
    st.subheader("Alertes intelligentes (paramétrables)")

    # --- Base calcul alertes ---
    tmp = filtered.copy()

    # Sécurités colonnes (au cas où certaines feuilles n'ont pas ces champs)
    for col in ["Début prévu", "Fin prévue", "Type", "Email"]:
        if col not in tmp.columns:
            tmp[col] = ""

    tmp["Début_dt"] = pd.to_datetime(tmp["Début prévu"], errors="coerce", dayfirst=True)
    tmp["Fin_dt"]   = pd.to_datetime(tmp["Fin prévue"], errors="coerce", dayfirst=True)
    today_dt = pd.Timestamp(dt.date.today())

    # --- Règles ---
    tmp["Alerte_retard_critique"] = (tmp["Écart"] <= thresholds["ecart_critique"])
    tmp["Alerte_non_demarre"] = (tmp["Statut_auto"] == "Non démarré") & (
        tmp["Début_dt"].isna() | (tmp["Début_dt"] <= today_dt)
    )
    tmp["Alerte_fin_depassee"] = (tmp["Statut_auto"] != "Terminé") & tmp["Fin_dt"].notna() & (tmp["Fin_dt"] < today_dt)

    def raison_alerte(row):
        reasons = []
        if bool(row.get("Alerte_fin_depassee", False)):
            reasons.append("⛔ Fin dépassée")
        if bool(row.get("Alerte_retard_critique", False)):
            reasons.append("🔻 Retard critique")
        if bool(row.get("Alerte_non_demarre", False)):
            reasons.append("🛑 Non démarré")
        return " • ".join(reasons)

    tmp["Raison_alerte"] = tmp.apply(raison_alerte, axis=1)
    tmp["En_alerte"] = tmp["Raison_alerte"].ne("")

    # Priorité (fin dépassée > retard critique > non démarré) puis écart
    tmp["_prio"] = (
        tmp["Alerte_fin_depassee"].astype(int) * 3
        + tmp["Alerte_retard_critique"].astype(int) * 2
        + tmp["Alerte_non_demarre"].astype(int) * 1
    )
    tmp = tmp.sort_values(["_prio", "Écart"], ascending=[False, True])

    # --- KPIs alertes ---
    nb_alertes = int(tmp["En_alerte"].sum())
    nb_fin = int(tmp["Alerte_fin_depassee"].sum())
    nb_ret = int(tmp["Alerte_retard_critique"].sum())
    nb_nd  = int(tmp["Alerte_non_demarre"].sum())

    st.markdown(
        f"""
        <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:10px 0 4px 0;">
          <div class="kpi kpi-bad"><div class="kpi-title">Total alertes</div><div class="kpi-value">{nb_alertes}</div></div>
          <div class="kpi kpi-bad"><div class="kpi-title">Fin dépassée</div><div class="kpi-value">{nb_fin}</div></div>
          <div class="kpi kpi-bad"><div class="kpi-title">Retards critiques</div><div class="kpi-value">{nb_ret}</div></div>
          <div class="kpi kpi-warn"><div class="kpi-title">Non démarrés</div><div class="kpi-value">{nb_nd}</div></div>
        </div>
        """,
        unsafe_allow_html=True
    )

    st.caption("💡 Onglet propre : lecture (Vue priorisée) séparée de l’envoi (Par enseignant).")
    st.divider()

    # --- Sous-onglets internes ---
    t1, t2, t3 = st.tabs(["📌 Vue priorisée", "📧 Par enseignant", "📊 Graphiques"])

    # =========================================================
    # 1) VUE PRIORISÉE
    # =========================================================
    with t1:
        st.write("### Liste des alertes (priorisées)")

        alerts = tmp.loc[
            tmp["En_alerte"],
            ["Classe","Matière","VHP","VHR","Écart","Taux","Statut_auto","Raison_alerte","Observations"]
        ].copy()

        alerts["Taux (%)"] = (alerts["Taux"] * 100).round(1)
        alerts["Statut"] = alerts["Statut_auto"].apply(statut_badge_text)

        st.dataframe(
            alerts[["Classe","Matière","VHP","VHR","Écart","Taux (%)","Statut","Raison_alerte","Observations"]],
            use_container_width=True,
            height=520,
            column_config={
                "Taux (%)": st.column_config.ProgressColumn("Taux (%)", min_value=0.0, max_value=100.0, format="%.1f%%"),
                "Écart": st.column_config.NumberColumn("Écart (h)", format="%.0f"),
                "VHP": st.column_config.NumberColumn("VHP", format="%.0f"),
                "VHR": st.column_config.NumberColumn("VHR", format="%.0f"),
            }
        )

        st.caption("✅ Ici : lecture uniquement (pas de boutons d’envoi).")

    # =========================================================
    # 2) PAR ENSEIGNANT (LOT + SELECTION + ENVOI)
    # =========================================================
    # =========================================================
    # 2) PAR ENSEIGNANT (LOT + SELECTION + ENVOI) — HTML POUR TOUS LES LOTS ✅
    # =========================================================
    with t2:
        st.write("### Préparation : notifications par enseignant (1 email / enseignant)")

        # ---------------------------------------------------------
        # 0) Sécurités colonnes
        # ---------------------------------------------------------
        for col in ["Email", "Type", "Semestre", "Observations"]:
            if col not in tmp.columns:
                tmp[col] = ""

        tmp["Email"] = (
            tmp["Email"].astype(str)
            .replace({"nan": "", "None": ""})
            .fillna("")
            .str.strip()
            .str.lower()
        )

        st.caption("✅ 1 email par enseignant (Email).")

        # ---------------------------------------------------------
        # 1) Choix du lot
        # ---------------------------------------------------------
        st.write("### 🎯 Choisir le lot à envoyer")

        lot = st.selectbox(
            "Type d'envoi",
            [
                "🚨 Toutes les alertes (Non démarré + Retard critique + Fin dépassée)",
                "🛑 Seulement Non démarré",
                "🔻 Seulement Retard critique",
                "⛔ Seulement Fin dépassée",
                "📌 Information : En cours (pas alerte)",
                "✅ Information : Terminé (pas alerte)",
            ],
            index=0,
            key="lot_prof"
        )

        # ---------------------------------------------------------
        # 2) Construire alerts_send (IMPORTANT : base = tmp)
        # ---------------------------------------------------------
        # Validation basique : garder uniquement les emails qui contiennent "@"
        base = tmp[tmp["Email"].str.contains("@", na=False)].copy()

        cols_keep = [
            "Responsable", "Email", "Classe", "Matière", "Semestre", "Type",
            "VHP", "VHR", "Écart", "Taux", "Statut_auto",
            "Raison_alerte", "Observations",
            "Alerte_non_demarre", "Alerte_retard_critique", "Alerte_fin_depassee"
        ]
        for c in cols_keep:
            if c not in base.columns:
                base[c] = ""

        if lot.startswith("🚨"):
            alerts_send = base[base["En_alerte"]].copy()
        elif lot.startswith("🛑"):
            alerts_send = base[base["Alerte_non_demarre"]].copy()
        elif lot.startswith("🔻"):
            alerts_send = base[base["Alerte_retard_critique"]].copy()
        elif lot.startswith("⛔"):
            alerts_send = base[base["Alerte_fin_depassee"]].copy()
        elif lot.startswith("📌"):
            alerts_send = base[base["Statut_auto"] == "En cours"].copy()
        else:  # ✅ Terminé
            alerts_send = base[base["Statut_auto"] == "Terminé"].copy()

        alerts_send = alerts_send[cols_keep].copy()

        # Nettoyage texte
        for c in ["Responsable", "Classe", "Matière", "Semestre", "Type", "Raison_alerte", "Observations"]:
            alerts_send[c] = (
                alerts_send[c].astype(str)
                .replace({"nan": "", "None": ""})
                .fillna("")
                .str.replace("\n", " ", regex=False)
                .str.strip()
            )

        # ---------------------------------------------------------
        # 3) Si vide -> on affiche ET ON N'ARRETE PAS L'APP
        # ---------------------------------------------------------
        if alerts_send.empty:
            st.info("Aucune ligne à envoyer pour ce lot (ou emails manquants).")
            st.caption("➡️ Vérifie que les enseignants ont bien une colonne Email renseignée.")
        else:
            # ---------------------------------------------------------
            # 4) Synthèse par enseignant (sur le lot choisi)
            # ---------------------------------------------------------
            synth_prof = alerts_send.groupby(["Responsable", "Email"]).agg(
                Nb_lignes=("Matière", "count"),
                Nb_non_demarre=("Statut_auto", lambda s: int((s == "Non démarré").sum())),
                Nb_en_cours=("Statut_auto", lambda s: int((s == "En cours").sum())),
                Nb_termine=("Statut_auto", lambda s: int((s == "Terminé").sum())),
            ).reset_index().sort_values("Nb_lignes", ascending=False)

            st.write("### Synthèse (lot sélectionné)")
            st.dataframe(synth_prof, use_container_width=True, height=260)

            # ---------------------------------------------------------
            # 5) Sélection des enseignants (IMPORTANT : basé sur alerts_send)
            # ---------------------------------------------------------
            st.write("### 👥 Choisir les enseignants (avant envoi)")

            profs_dispo = sorted([p for p in alerts_send["Responsable"].unique().tolist() if str(p).strip() != ""])

            profs_sel = st.multiselect(
                "Enseignants à notifier",
                options=profs_dispo,
                default=profs_dispo,
                key="profs_sel"
            )

            alerts_send_sel = alerts_send[alerts_send["Responsable"].isin(profs_sel)].copy()
            alerts_send_sel["Statut"] = alerts_send_sel["Statut_auto"].apply(statut_badge_text)


            st.caption(f"📌 Enseignants sélectionnés : {len(profs_sel)} | Lignes à envoyer : {len(alerts_send_sel)}")

            st.write("Aperçu (lot sélectionné) :")
            st.dataframe(
                alerts_send_sel[["Responsable","Email","Classe","Semestre","Type","Matière","Écart","Statut","Raison_alerte","Observations"]].head(80),
                use_container_width=True,
                height=320
            )

            st.divider()

            # ---------------------------------------------------------
            # 6) Envoi (admin)
            # ---------------------------------------------------------
            st.write("### 🚀 Envoyer (admin)")

            if st.button("📩 Envoyer maintenant aux enseignants", key="send_prof_alerts"):
                if not st.session_state.get("is_admin", False):
                    st.error("Accès refusé : PIN incorrect.")
                    st.stop()

                if alerts_send_sel.empty:
                    st.warning("Aucune ligne à envoyer (vérifie lot + sélection).")
                    st.stop()

                sent, errors = 0, 0
                grp = alerts_send_sel.groupby(["Responsable", "Email"])

                for (prof, mail), gprof in grp:
                    # Texte fallback
                    lignes_txt = []
                    for _, r in gprof.sort_values(["Statut_auto", "Écart"]).iterrows():
                        lignes_txt.append(
                            f"- {r.get('Classe','')} | {r.get('Semestre','')} | {r.get('Type','')} | {r.get('Matière','')} | "
                            f"VHP={int(float(r.get('VHP',0) or 0))} VHR={int(float(r.get('VHR',0) or 0))} "
                            f"Écart={int(float(r.get('Écart',0) or 0))} | {r.get('Statut_auto','')} | {r.get('Raison_alerte','')}"
                        )

                    body_text_prof = (
                        f"{CFG['dept_code']} — Notification de suivi des enseignements\n"
                        f"Période : {mois_min} → {mois_max}\n\n"
                        f"Bonjour {prof},\n\n"
                        f"Lot : {lot}\n"
                        f"Éléments concernés : {len(gprof)}\n\n"
                        + "\n".join(lignes_txt)
                        + f"\n\n{CFG['department_long']}\n"
                    )

                    # ✅ HTML : tu as déjà build_prof_email_html global, on l’utilise ici
                    body_html_prof = build_prof_email_html(
                        prof=prof,
                        lot_label=lot,
                        mois_min=mois_min,
                        mois_max=mois_max,
                        thresholds=thresholds,
                        gprof=gprof,
                        cfg=CFG,
                    )

                    subject_prof = f"{CFG['dept_code']} — Notification ({mois_min}→{mois_max}) : {lot.split(' ',1)[1]} — {len(gprof)} élément(s)"
                    try:
                        cfg_smtp = _get_smtp_config()
                        send_email_reminder(
                            smtp_host=cfg_smtp["smtp_host"],
                            smtp_port=cfg_smtp["smtp_port"],
                            smtp_user=cfg_smtp["smtp_user"],
                            smtp_pass=cfg_smtp["smtp_pass"],
                            sender=cfg_smtp["smtp_from"],
                            recipients=[mail],
                            subject=subject_prof,
                            body_text=body_text_prof,
                            body_html=body_html_prof,
                        )
                        sent += 1
                    except Exception as e:
                        errors += 1
                        st.error(f"Erreur envoi à {prof} ({mail}) : {e}")

                if sent:
                    st.success(f"✅ Emails envoyés à {sent} enseignant(s).")
                if errors:
                    st.warning(f"⚠️ {errors} envoi(s) en échec.")


    # =========================================================
    # 3) GRAPHIQUES
    # =========================================================
    with t3:
        st.write("### Non démarré — par classe")
        nd = tmp[tmp["Alerte_non_demarre"]].groupby("Classe").size().sort_values(ascending=False)
        st.bar_chart(nd)

        st.write("### Retards critiques — par classe")
        crit = tmp[tmp["Alerte_retard_critique"]].groupby("Classe").size().sort_values(ascending=False)
        st.bar_chart(crit)

        st.write("### Fin dépassée — par classe")
        fin = tmp[tmp["Alerte_fin_depassee"]].groupby("Classe").size().sort_values(ascending=False)
        st.bar_chart(fin)


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

    # === PATCH OPENAI DOWNLOAD ONLY ===
    if "obs_ai_md" not in st.session_state:
        st.session_state["obs_ai_md"] = None

    st.subheader("Exports (Excel consolidé + PDF officiel)")
    st.caption("Les exports respectent les filtres actifs + la période sélectionnée.")

    col1, col2 = st.columns(2)

    # =========================================================
    # 1) EXCEL CONSOLIDÉ
    # =========================================================
    with col1:
        st.write("### Export Excel consolidé")

        export_df = filtered[
            ["Classe","Semestre","Matière","Début prévu","Fin prévue","VHP"]
            + MOIS_COLS
            + ["VHR","Écart","Taux","Statut_auto","Observations"]
        ].copy()

        export_df["Taux"] = (export_df["Taux"]*100).round(2)

        synth_class = filtered.groupby("Classe").agg(
            Matieres=("Matière","count"),
            Taux_moy=("Taux","mean"),
            VHP_total=("VHP","sum"),
            VHR_total=("VHR","sum"),
            Retard_h=("Écart", lambda s: float(s[s<0].sum()))
        ).reset_index()
        synth_class["Taux_moy"] = (synth_class["Taux_moy"]*100).round(2)

        synth_resp = filtered.groupby("Responsable").agg(
            Matieres=("Matière","count"),
            Classes=("Classe","nunique"),
            VHP_total=("VHP","sum"),
            VHR_total=("VHR","sum"),
            Taux_moy=("Taux","mean"),
            Retard_h=("Écart", lambda s: float(s[s<0].sum())),
            Non_demarre=("Statut_auto", lambda s: int((s=="Non démarré").sum())),
        ).reset_index()
        synth_resp["Taux_moy"] = (synth_resp["Taux_moy"]*100).round(2)

        xbytes = df_to_excel_bytes({
            "Consolidé": export_df,
            "Synthese_Classes": synth_class,
            "Synthese_Responsables": synth_resp,
        })

        st.download_button(
            "⬇️ Télécharger l’Excel consolidé",
            data=xbytes,
            file_name=f"{export_prefix}_consolide.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="dl_excel"
        )

    # =========================================================
    # 2) PDF + OPENAI
    # =========================================================
    with col2:

        # ---------- PDF PRINCIPAL ----------
        st.write("### Export PDF (rapport mensuel officiel)")

        pdf_title = st.text_input(
            "Titre du rapport PDF",
            value=f"Rapport mensuel — Suivi des enseignements ({CFG['dept_code']}) | {CFG['department_long']}",
            key="pdf_title_export"
        )

        logo_bytes = logo.getvalue() if logo else None

        if st.button("Générer le PDF", key="btn_pdf_main"):
            pdf = build_pdf_report(
                df=filtered[
                    ["Classe","Semestre","Matière","Début prévu","Fin prévue","VHP"]
                    + mois_couverts
                    + ["VHR","Écart","Taux","Statut_auto","Observations"]
                ].copy(),
                title=pdf_title,
                mois_couverts=mois_couverts,
                thresholds=thresholds,
                logo_bytes=logo_bytes,
                author_name=CFG["author_name"],
                assistant_name=CFG["assistant_name"],
                department=CFG["department_long"],
                institution=CFG["institution"],
            )

            st.download_button(
                "⬇️ Télécharger le PDF",
                data=pdf,
                file_name=f"{export_prefix}_rapport.pdf",
                mime="application/pdf",
                key="dl_pdf_main"
            )

        st.divider()

        # ---------- PDF OBSERVATIONS ----------
        st.write("### Export PDF Observations")

        pdf_obs_title = st.text_input(
            "Titre PDF Observations",
            value=f"Suivi des enseignements — Observations ({CFG['dept_code']})",
            key="pdf_obs_title"
        )

        if st.button("Générer le PDF Observations", key="btn_pdf_obs"):
            pdf_obs = build_pdf_observations_report(
                df=filtered[
                    ["Classe","Semestre","Type","Matière","Responsable","VHP","VHR","Écart","Taux","Statut_auto","Observations"]
                ].copy(),
                title=pdf_obs_title,
                mois_couverts=mois_couverts,
                logo_bytes=logo_bytes,
                author_name=CFG["author_name"],
                assistant_name=CFG["assistant_name"],
                department=CFG["department_long"],
                institution=CFG["institution"],
            )

            st.download_button(
                "⬇️ Télécharger PDF Observations",
                data=pdf_obs,
                file_name=f"{export_prefix}_observations.pdf",
                mime="application/pdf",
                key="dl_pdf_obs"
            )

        st.divider()

        # =========================================================
        # OPENAI RESUME — TELECHARGEABLE (SEULE MODIF)
        # =========================================================
        st.subheader("🧠 Résumé IA — Observations")

        if not st.session_state.get("is_admin", False):
            st.info("🔒 Réservé Admin")
        else:

            max_lines_llm = st.slider(
                "Nombre max observations envoyées à l'IA",
                50, 800, 300, 50,
                key="slider_ai"
            )

            if st.button("🧠 Générer résumé IA", key="btn_ai_obs"):
                try:
                    with st.spinner("Analyse IA en cours..."):
                        st.session_state["obs_ai_md"] = summarize_observations_with_openai(
                            df_filtered=filtered,
                            mois_min=mois_min,
                            mois_max=mois_max,
                            cfg=CFG,
                            model="gpt-4.1-mini",
                            max_lines=int(max_lines_llm),
                        )
                except Exception as e:
                    st.session_state["obs_ai_md"] = None
                    st.error(f"Erreur IA : {e}")

            # ===== AFFICHAGE + DOWNLOAD =====
            if st.session_state["obs_ai_md"]:

                st.markdown(st.session_state["obs_ai_md"])

                md_bytes = st.session_state["obs_ai_md"].encode("utf-8")

                st.download_button(
                    "⬇️ Télécharger résumé IA (.md)",
                    data=md_bytes,
                    file_name=f"{export_prefix}_resume_IA_{mois_min}_{mois_max}.md",
                    mime="text/markdown",
                    key="dl_ai_md"
                )

                st.download_button(
                    "⬇️ Télécharger résumé IA (.txt)",
                    data=md_bytes,
                    file_name=f"{export_prefix}_resume_IA_{mois_min}_{mois_max}.txt",
                    mime="text/plain",
                    key="dl_ai_txt"
                )

        



st.caption("✅ Astuce : standardise les colonnes sur toutes les feuilles. L’app calcule automatiquement VHR/Écart/Taux/Statut selon la période sélectionnée.")
