"""FastAPI application for Telegram Mini App."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .router import router as api_router

app = FastAPI(title="Abr Pardaz Mini App", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)

try:
    app.mount("/", StaticFiles(directory="webapp", html=True), name="static")
except RuntimeError:
    pass  # webapp dir not present yet
