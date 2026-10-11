# Agnes Storyboard 🎬

Génère une vidéo d'**1 minute** à partir de **1 à 5 images** + un thème :
scénario auto (modifiable), montage, **voix française**, musique de fond et
sous-titres incrustés. Propulsé par l'API **Agnes AI**.

## Architecture

- **`index.html`** — frontend (servi par GitHub Pages).
- **`server.py`** — backend (à faire tourner sur un serveur, car il détient la clé
  Agnes + fait le montage ffmpeg + la voix TTS).

Le frontend appelle le backend via HTTP. Par défaut, le backend tourne sur
`http://<IP>:8647`.

## Démarrage du backend

```bash
pip install edge-tts
python3 server.py
```

Variables requises (dans `.env` du profil Hermes) :
- `AGNES_API_KEY` — clé Agnes AI
- `DEEPSEEK_API_KEY` — pour l'écriture du scénario

## Utilisation

1. Ouvre le frontend (Pages ou `http://<IP>:8647/`).
2. ⚙️ → renseigne l'URL du serveur + la clé d'accès (générée dans `token.txt`).
3. Ajoute 1 à 5 images, décris un thème, génère le scénario (modifiable).
4. Choisis le ratio, la voix, la musique, les sous-titres.
5. Génère (~10-15 min) puis télécharge la vidéo.
