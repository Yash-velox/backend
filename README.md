# Image Enhancement — Backend (FastAPI)

Skeleton API. Tunnel this process with **Cloudflare Tunnel** in local dev.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Run

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

Health check: [http://127.0.0.1:8080/health](http://127.0.0.1:8080/health)

## Cloudflare tunnel

```bash
cloudflared tunnel --url http://127.0.0.1:8080
```

Copy the `*.trycloudflare.com` URL into:

- `ReactFrontend/.env` → `VITE_API_BASE_URL`
- `ShopifyApp/image-enhancement/shopify.app.toml` → `[app_proxy].url`
- `Backend/.env` → add that frontend ngrok origin to `CORS_ORIGINS`
