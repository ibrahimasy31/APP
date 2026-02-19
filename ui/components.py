from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd
import streamlit as st


def add_badges(df: pd.DataFrame, status_col: str = "Statut_auto") -> pd.DataFrame:
    out = df.copy()
    if status_col not in out.columns:
        if "Statut_auto" in out.columns:
            status_col = "Statut_auto"
        elif "Statut" in out.columns:
            status_col = "Statut"
        else:
            out["Statut_badge"] = ""
            return out

    def badge(statut: str) -> str:
        s = str(statut).strip()
        if s == "Terminé":
            return '<span class="badge badge-ok">✅ Terminé</span>'
        if s == "En cours":
            return '<span class="badge badge-warn">🟠 En cours</span>'
        return '<span class="badge badge-bad">🔴 Non démarré</span>'

    out["Statut_badge"] = out[status_col].apply(badge)
    return out


def style_table(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "Taux" in out.columns and np.issubdtype(out["Taux"].dtype, np.number):
        out["Taux (%)"] = (out["Taux"] * 100).round(1)
    return out


def statut_badge_text(s: str) -> str:
    s = str(s).strip()
    if s == "Terminé":
        return "✅ Terminé"
    if s == "En cours":
        return "🟠 En cours"
    return "🔴 Non démarré"


def niveau_from_statut(s: str) -> str:
    s = str(s).strip()
    if s == "Terminé":
        return "OK"
    if s == "En cours":
        return "ATTENTION"
    return "CRITIQUE"


def render_badged_table(df: pd.DataFrame, columns: List[str], title: str = "") -> None:
    if title:
        st.write(title)
    tmp = add_badges(df)
    html = tmp[columns].to_html(escape=False, index=False, classes="iaid-table")
    st.markdown(f'<div class="table-wrap">{html}</div>', unsafe_allow_html=True)


def sidebar_card(title: str) -> None:
    st.markdown(
        f'<div class="sidebar-card"><div style="font-weight:950;font-size:14px;margin-bottom:10px;">{title}</div>',
        unsafe_allow_html=True,
    )


def sidebar_card_end() -> None:
    st.markdown("</div>", unsafe_allow_html=True)
