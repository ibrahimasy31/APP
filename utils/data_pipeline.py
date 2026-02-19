from __future__ import annotations

import io
import re
from typing import Dict, List, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import numpy as np
import pandas as pd
import requests
import streamlit as st

MOIS_COLS = ["Oct", "Nov", "Déc", "Jan", "Fév", "Mars", "Avril", "Mai", "Juin", "Juil", "Août"]


def normalize_semestre_value(x) -> str:
    """Normalise une valeur de semestre vers le format 'S1', 'S2', etc."""
    if pd.isna(x):
        return ""
    s = str(x).strip().upper()
    if s.isdigit():
        return f"S{int(s)}"
    s = s.replace("SEMESTRE", "S").replace("SEM", "S")
    m = re.search(r"S\s*0*([1-9]\d*)", s)
    if m:
        return f"S{int(m.group(1))}"
    return s
MOIS_ORDER = {m: i for i, m in enumerate(MOIS_COLS, start=1)}

DEFAULT_THRESHOLDS = {
    "taux_vert": 0.90,
    "taux_orange": 0.60,
    "ecart_critique": -6,
    "max_non_demarre": 0.25,
}


def clean_colname(s: str) -> str:
    s = str(s)
    s = s.replace("\n", " ").replace('"', "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [clean_colname(c) for c in df.columns]
    rename_map = {
        "Taux (%)": "Taux_excel",
        "Taux": "Taux_excel",
        "Ecart": "Écart",
        "Écart": "Écart",
        "Vhr": "VHR",
        "VHP ": "VHP",
        "Matiere": "Matière",
        "Matière ": "Matière",
        "Responsable ": "Responsable",
        "Enseignant": "Responsable",
        "Prof": "Responsable",
        "Semestre ": "Semestre",
        "Semester": "Semestre",
        "Observation": "Observations",
        "Observations ": "Observations",
        "Début prévu ": "Début prévu",
        "Debut prevu": "Début prévu",
        "Début": "Début prévu",
        "Fin prévue ": "Fin prévue",
        "Fin prevue": "Fin prévue",
        "Fin": "Fin prévue",
        "Mail": "Email",
        "E-mail": "Email",
        "Email ": "Email",
        "Email enseignant": "Email",
        "Email Enseignant": "Email",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    return df


def ensure_month_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for m in MOIS_COLS:
        if m not in df.columns:
            df[m] = 0
    return df


def to_numeric_safe(s: pd.Series) -> pd.Series:
    def conv(x):
        if pd.isna(x):
            return np.nan
        if isinstance(x, (int, float, np.number)):
            return float(x)
        x = str(x).strip().replace(",", ".")
        x = re.sub(r"[^0-9\.\-]", "", x)
        if x == "":
            return np.nan
        try:
            return float(x)
        except Exception:
            return np.nan

    return s.apply(conv)


def compute_metrics(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ["Semestre", "Observations", "Début prévu", "Fin prévue"]:
        if c not in df.columns:
            df[c] = ""

    if "Responsable" not in df.columns:
        df["Responsable"] = ""
    df["Responsable"] = (
        df["Responsable"]
        .astype(str)
        .replace({"nan": "", "None": ""})
        .fillna("")
        .str.replace("\n", " ", regex=False)
        .str.strip()
    )

    if "Email" not in df.columns:
        df["Email"] = ""
    df["Email"] = (
        df["Email"].astype(str).replace({"nan": "", "None": ""}).fillna("").str.strip().str.lower()
    )

    for c in ["Matière", "Semestre", "Observations"]:
        df[c] = df[c].astype(str).replace({"nan": "", "None": ""}).fillna("").str.strip()

    df["Début prévu"] = (
        df["Début prévu"].astype(str).replace({"nan": "", "None": ""}).fillna("").str.strip()
    )
    df["Fin prévue"] = (
        df["Fin prévue"].astype(str).replace({"nan": "", "None": ""}).fillna("").str.strip()
    )

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

    if "Statut" not in df.columns:
        df["Statut"] = df["Statut_auto"]
    else:
        df["Statut"] = df["Statut"].astype(str).replace({"nan": ""}).fillna("")

    if "Observations" not in df.columns:
        df["Observations"] = ""

    df["Matière"] = df["Matière"].astype(str).str.replace("\n", " ").str.strip()
    df["Matière"] = df["Matière"].str.replace(r"\s+", " ", regex=True)
    df["Matière_vide"] = df["Matière"].eq("") | df["Matière"].str.lower().eq("nan")
    return df


def unpivot_months(df: pd.DataFrame) -> pd.DataFrame:
    id_cols = [
        c
        for c in [
            "_rowid",
            "Classe",
            "Semestre",
            "Matière",
            "Responsable",
            "VHP",
            "VHR",
            "Écart",
            "Taux",
            "Statut_auto",
            "Statut",
            "Observations",
            "Début prévu",
            "Fin prévue",
        ]
        if c in df.columns
    ]
    long = df.melt(id_vars=id_cols, value_vars=MOIS_COLS, var_name="Mois", value_name="Heures")
    long["Mois_idx"] = long["Mois"].map(MOIS_ORDER).fillna(0).astype(int)
    return long


def df_to_excel_bytes(sheets: Dict[str, pd.DataFrame]) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for name, sheet_df in sheets.items():
            sheet_df.to_excel(writer, sheet_name=name[:31], index=False)
    return output.getvalue()


def _with_cachebuster(u: str, cb: str) -> str:
    p = urlparse(u)
    q = dict(parse_qsl(p.query))
    q["_cb"] = cb
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q), p.fragment))


@st.cache_data(show_spinner=False, max_entries=20)
def fetch_excel_from_url(url: str, cache_bust: str) -> bytes:
    headers = {
        "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    final_url = _with_cachebuster(url.strip(), cache_bust)
    r = requests.get(final_url, timeout=45, headers=headers)
    r.raise_for_status()
    return r.content


@st.cache_data(show_spinner=False)
def make_long(df_period: pd.DataFrame) -> pd.DataFrame:
    return unpivot_months(df_period)


@st.cache_data(show_spinner=False, max_entries=50)
def fetch_headers(url: str, cache_bust: str) -> dict:
    headers = {
        "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    r = requests.head(url.strip(), timeout=20, headers=headers, allow_redirects=True)
    r.raise_for_status()
    return dict(r.headers)


@st.cache_data(show_spinner=False, max_entries=20)
def fetch_excel_if_changed(url: str, etag_or_lm: str) -> bytes:
    return fetch_excel_from_url(url, etag_or_lm)


@st.cache_data(show_spinner=False)
def load_excel_all_sheets(file_bytes: bytes) -> Tuple[pd.DataFrame, Dict[str, List[str]]]:
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
        missing = [col for col in ["Matière", "VHP"] if col not in df.columns]
        if missing:
            quality_issues.setdefault(sheet, []).append(f"Colonnes manquantes: {', '.join(missing)}")
            continue

        df = ensure_month_cols(df)
        if df.columns.duplicated().any():
            quality_issues.setdefault(sheet, []).append("Colonnes dupliquées détectées.")
        if df["Matière"].isna().mean() > 0.20:
            quality_issues.setdefault(sheet, []).append(
                "Beaucoup de valeurs manquantes dans 'Matière' (>20%)."
            )

        df["Classe"] = sheet
        frames.append(df)

    if not frames:
        return pd.DataFrame(), quality_issues

    all_df = pd.concat(frames, ignore_index=True)
    all_df = compute_metrics(all_df)
    all_df["_rowid"] = np.arange(len(all_df))

    if all_df["Matière_vide"].mean() > 0.05:
        quality_issues.setdefault("__GLOBAL__", []).append(
            "Plus de 5% de lignes ont une 'Matière' vide/invalides."
        )
    if (all_df["VHP"] <= 0).mean() > 0.10:
        quality_issues.setdefault("__GLOBAL__", []).append(
            "Plus de 10% de lignes ont VHP <= 0 (à vérifier)."
        )

    return all_df, quality_issues
