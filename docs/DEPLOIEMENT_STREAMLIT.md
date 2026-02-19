# Guide de déploiement Streamlit Cloud (3 dashboards)

## Objectif
Déployer 3 dashboards à partir du même repo avec la nouvelle configuration:
- IAID (fichier principal `app.py`)
- KM (fichier principal `app_km.py`)
- DRS (fichier principal `app_rx.py`)

## Prérequis
- Le code est push sur GitHub.
- `requirements.txt` est à jour.
- Les secrets ne sont pas commités dans Git.

## Déploiement sur Streamlit Cloud (à répéter 3 fois)
1. Aller sur `https://share.streamlit.io/`
2. Cliquer sur **Create app**
3. Choisir:
   - Repo: ton repo GitHub
   - Branch: `main` (ou ta branche)
   - Main file path: selon le dashboard (voir tableau ci-dessous)
4. Ouvrir **Advanced settings**
5. Coller les secrets de l'app
6. Cliquer **Deploy**

### Mapping des 3 apps
- IAID: `app.py`
- KM: `app_km.py`
- DRS: `app_rx.py`

## Secrets à configurer dans chaque app Cloud
```toml
IAID_EXCEL_URL = "..."
DG_EMAILS = "..."
DASHBOARD_URL = "..."
ADMIN_PIN = "..."

SMTP_HOST = "..."
SMTP_PORT = "587"
SMTP_USER = "..."
SMTP_PASS = "..."
SMTP_FROM = "..."

OPENAI_API_KEY = "..."
```

## Lancer localement les dashboards ici
Depuis le dossier du projet:

```bash
# Installer dépendances
python3 -m pip install -r requirements.txt
```

### Dashboard IAID
```bash
python3 -m streamlit run app.py
```

### Dashboard KM
```bash
python3 -m streamlit run app_km.py
```

### Dashboard DRS
```bash
python3 -m streamlit run app_rx.py
```

## Alternative avec variable d'environnement
Tu peux aussi lancer le même fichier `app.py` avec un profil:

```bash
APP_DEPT_PROFILE=IAID python3 -m streamlit run app.py
APP_DEPT_PROFILE=KM python3 -m streamlit run app.py
APP_DEPT_PROFILE=DRS python3 -m streamlit run app.py
```

## Vérification rapide
- Si erreur `ModuleNotFoundError`: relancer `python3 -m pip install -r requirements.txt`.
- Si erreur secrets: ajouter les clés dans `.streamlit/secrets.toml` en local ou dans les secrets Cloud.
