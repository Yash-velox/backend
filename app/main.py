from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings

app = FastAPI(
    title="Image Enhancement API",
    version="0.1.0",
    description="FastAPI backend for Shopify Image Enhancement (skeleton)",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Allow any *.trycloudflare.com / *.ngrok-free.app origin in local dev
    allow_origin_regex=r"https://.*\.(trycloudflare\.com|ngrok-free\.(app|dev)|ngrok\.io)",
)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "image-enhancement-api",
        "env": settings.app_env,
    }


@app.get("/tenant/checkConfig")
def tenant_check_config():
    """
    Placeholder for Retention Hub-style first-install bootstrap.
    Will validate Shopify session JWT + provision shop later.
    """
    return {
        "status": "ok",
        "installed": False,
        "message": "Skeleton only — install/bootstrap not implemented yet",
    }
