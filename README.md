# YT Cortex

Interface web : colle des URL YouTube → transcription (sous-titres, sinon Whisper) → fiche de formation (Claude) avec liens → « Transformer en skill » → .skill / dépôt Dropbox (AIOS). Abonnements à des chaînes (vidéos des 2 derniers mois, sélection manuelle).

- Stack : FastAPI + SQLite, yt-dlp (+ bgutil PO token, Deno), faster-whisper, API Anthropic.
- Déploiement : `deploy/compose.yml` (Traefik, volume `cortex_data`, sidecar `bgutil`), variables : APP_USER, APP_PASSWORD, ANTHROPIC_API_KEY, APP_B64/APP_SHA (code en tar.gz base64).
- Tests : `pytest tests`.
