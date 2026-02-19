# Dashboard Suivi des Classes

## Structure
- `app.py` : point d’entrée principal (profil dynamique).
- `config/departments.py` : profils départementaux (`IAID`, `KM`, `DRS`).
- `utils/data_pipeline.py` : chargement Excel, normalisation, métriques, exports.
- `services/email_notifications.py` : rappels mensuels + envoi emails + template HTML.
- `ui/components.py` : composants UI réutilisables (badges, cartes sidebar, tables).
- `app_km.py` : lance `app.py` avec le profil `KM`.
- `app_rx.py` : lance `app.py` avec le profil `DRS`.

## Lancement
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Choisir un profil
```bash
APP_DEPT_PROFILE=IAID streamlit run app.py
APP_DEPT_PROFILE=KM streamlit run app.py
APP_DEPT_PROFILE=DRS streamlit run app.py
```
