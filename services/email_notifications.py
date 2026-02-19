from __future__ import annotations

import datetime as dt
import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import List, Optional

import pandas as pd

REMINDER_DIR = Path(".streamlit")
REMINDER_DIR.mkdir(parents=True, exist_ok=True)

# Préfixe par département pour éviter les collisions sur Streamlit Cloud
# (plusieurs instances dans le même dossier)
_dept_prefix = os.getenv("APP_DEPT_PROFILE", "IAID").upper()
REMINDER_FILE = REMINDER_DIR / f"last_reminder_{_dept_prefix}.json"
LOCK_FILE = REMINDER_DIR / f"last_reminder_{_dept_prefix}.lock"


def get_last_reminder_month() -> Optional[str]:
    if REMINDER_FILE.exists():
        try:
            return json.loads(REMINDER_FILE.read_text()).get("month")
        except Exception:
            return None
    return None


def set_last_reminder_month(month_key: str) -> None:
    REMINDER_FILE.write_text(json.dumps({"month": month_key}))


def lock_is_active(month_key: str) -> bool:
    if not LOCK_FILE.exists():
        return False
    try:
        payload = json.loads(LOCK_FILE.read_text())
        return payload.get("month") == month_key and payload.get("status") == "sending"
    except Exception:
        return False


def set_lock(month_key: str) -> None:
    LOCK_FILE.write_text(
        json.dumps({"month": month_key, "status": "sending", "ts": dt.datetime.now().isoformat()})
    )


def clear_lock() -> None:
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass


def send_email_reminder(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_pass: str,
    sender: str,
    recipients: List[str],
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
        s.starttls()
        s.login(smtp_user, smtp_pass)
        s.send_message(msg)


def build_prof_email_html(
    prof: str,
    lot_label: str,
    mois_min: str,
    mois_max: str,
    thresholds: dict,
    gprof: pd.DataFrame,
    cfg: dict,
) -> str:
    def statut_chip_html(statut: str) -> str:
        s = str(statut).strip()
        if s == "Terminé":
            return '<span style="display:inline-block;padding:6px 10px;border-radius:999px;font-weight:900;font-size:12px;background:rgba(30,142,62,0.12);color:#1E8E3E;border:1px solid rgba(30,142,62,0.25);">✅ Terminé</span>'
        if s == "En cours":
            return '<span style="display:inline-block;padding:6px 10px;border-radius:999px;font-weight:900;font-size:12px;background:rgba(242,153,0,0.14);color:#B26A00;border:1px solid rgba(242,153,0,0.30);">🟠 En cours</span>'
        return '<span style="display:inline-block;padding:6px 10px;border-radius:999px;font-weight:900;font-size:12px;background:rgba(217,48,37,0.12);color:#D93025;border:1px solid rgba(217,48,37,0.25);">🔴 Non démarré</span>'

    lignes_html = ""
    gshow = gprof.copy()

    for c in ["Classe", "Semestre", "Type", "Matière", "VHP", "VHR", "Écart", "Statut_auto", "Raison_alerte"]:
        if c not in gshow.columns:
            gshow[c] = ""

    gshow = gshow.sort_values(["Écart"], ascending=True)

    for _, r in gshow.iterrows():
        classe = str(r.get("Classe", ""))
        sem = str(r.get("Semestre", ""))
        typ = str(r.get("Type", ""))
        mat = str(r.get("Matière", ""))[:80]
        vhp = int(float(r.get("VHP", 0) or 0))
        vhr = int(float(r.get("VHR", 0) or 0))
        ec = int(float(r.get("Écart", 0) or 0))
        statut = str(r.get("Statut_auto", ""))
        raison = str(r.get("Raison_alerte", ""))

        ec_color = "#D93025" if ec <= thresholds["ecart_critique"] else "#0F172A"

        lignes_html += f"""
        <tr>
          <td style="padding:10px;border-bottom:1px solid #E3E8F0;">{classe}</td>
          <td style="padding:10px;border-bottom:1px solid #E3E8F0;">{sem}</td>
          <td style="padding:10px;border-bottom:1px solid #E3E8F0;">{typ}</td>
          <td style="padding:10px;border-bottom:1px solid #E3E8F0;">{mat}</td>
          <td style="padding:10px;border-bottom:1px solid #E3E8F0;text-align:center;">{vhp}</td>
          <td style="padding:10px;border-bottom:1px solid #E3E8F0;text-align:center;">{vhr}</td>
          <td style="padding:10px;border-bottom:1px solid #E3E8F0;text-align:center;font-weight:900;color:{ec_color};">{ec}</td>
          <td style="padding:10px;border-bottom:1px solid #E3E8F0;">{statut_chip_html(statut)}</td>
          <td style="padding:10px;border-bottom:1px solid #E3E8F0;">{raison}</td>
        </tr>
        """

    now_str = dt.datetime.now().strftime("%d/%m/%Y %H:%M")

    return f"""
    <!doctype html>
    <html>
    <body style="margin:0;padding:0;background:#0B3D91;">
    <div style="background:linear-gradient(180deg,#0B3D91 0%,#134FA8 100%);padding:34px 12px;">
      <div style="max-width:900px;margin:0 auto;background:#FFFFFF;border-radius:20px;
                  box-shadow:0 20px 50px rgba(0,0,0,0.25);overflow:hidden;
                  font-family:Arial,Helvetica,sans-serif;color:#0F172A;">
        <div style="padding:22px 26px;background:linear-gradient(90deg,#0B3D91,#1F6FEB);color:#FFFFFF;">
          <div style="font-size:18px;font-weight:900;">{cfg["dept_code"]} — Notification Enseignant</div>
          <div style="margin-top:6px;font-size:13px;font-weight:700;opacity:.95;">
            {lot_label} • Période : {mois_min} → {mois_max}
          </div>
          <div style="margin-top:6px;font-size:12px;font-weight:700;opacity:.9;">Mise à jour : {now_str}</div>
        </div>
        <div style="padding:26px;line-height:1.55;">
          <p style="margin-top:0;">Bonjour <b>{prof}</b>,</p>
          <p>Vous avez <b>{len(gprof)} élément(s)</b> concerné(s) par le lot : <b>{lot_label}</b>.</p>
          <div style="margin:14px 0;background:#F6F8FC;border:1px solid #E3E8F0;border-radius:14px;padding:14px 16px;">
            <div style="font-weight:900;color:#0B3D91;margin-bottom:6px;">📌 Information</div>
            <div style="font-size:13px;">Aucune action n’est requise. Message transmis à titre informatif.</div>
          </div>
          <div style="margin:18px 0;border:1px solid #E3E8F0;border-radius:14px;overflow:hidden;">
            <table style="border-collapse:collapse;width:100%;font-size:13px;">
              <thead>
                <tr style="background:#F6F8FC;">
                  <th style="padding:10px;text-align:left;border-bottom:1px solid #E3E8F0;">Classe</th>
                  <th style="padding:10px;text-align:left;border-bottom:1px solid #E3E8F0;">Sem</th>
                  <th style="padding:10px;text-align:left;border-bottom:1px solid #E3E8F0;">Type</th>
                  <th style="padding:10px;text-align:left;border-bottom:1px solid #E3E8F0;">Matière</th>
                  <th style="padding:10px;text-align:center;border-bottom:1px solid #E3E8F0;">VHP</th>
                  <th style="padding:10px;text-align:center;border-bottom:1px solid #E3E8F0;">VHR</th>
                  <th style="padding:10px;text-align:center;border-bottom:1px solid #E3E8F0;">Écart</th>
                  <th style="padding:10px;text-align:left;border-bottom:1px solid #E3E8F0;">Statut</th>
                  <th style="padding:10px;text-align:left;border-bottom:1px solid #E3E8F0;">Raison</th>
                </tr>
              </thead>
              <tbody>{lignes_html}</tbody>
            </table>
          </div>
          <p style="font-size:13px;color:#475569;">Message généré automatiquement — {cfg["department_long"]}.</p>
        </div>
        <div style="padding:14px 26px;background:#FBFCFF;border-top:1px solid #E3E8F0;
                    font-size:12px;color:#475569;text-align:center;">
                    {cfg["department_long"]}
        </div>
      </div>
    </div>
    </body>
    </html>
    """.strip()
