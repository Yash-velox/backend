# Image Enhancement - Backend (FastAPI)

API + Postgres-backed Shopify product-image processing queue (asyncio worker).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Set DATABASE_URL (PostgreSQL preferred; SQLite works for local/dev)
mkdir -p storage
alembic upgrade head
```

## Run

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

The lifespan worker starts automatically when `AUTO_PROCESSING_ENABLED=true`.

Health check: [http://127.0.0.1:8080/health](http://127.0.0.1:8080/health)

## Tests

```bash
pytest
```

## Queue notes

- Enqueue only via Shopify product IDs (`POST /api/processing-queue/shopify-products`).
- Originals stay on Shopify CDN (temp download at process time).
- Processed outputs go to `PROCESSING_OUTPUT_DIRECTORY` (local filesystem fallback).
- No Shopify media write/publish in this phase.
- Dev Admin token fallback: `SHOPIFY_DEV_ACCESS_TOKEN` when `APP_ENV=dev`.

## Cloudflare tunnel

```bash
cloudflared tunnel --url http://127.0.0.1:8080
```

Copy the `*.trycloudflare.com` URL into:

- `ReactFrontend/.env` → `VITE_API_BASE_URL`
- `ShopifyApp/image-enhancement/shopify.app.toml` → `[app_proxy].url`
- `Backend/.env` → add that frontend ngrok origin to `CORS_ORIGINS`
