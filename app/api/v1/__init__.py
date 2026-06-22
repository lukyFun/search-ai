from fastapi import APIRouter
from app.api.v1 import chat, ingest

api_router = APIRouter()
api_router.include_router(chat.router, tags=["chat"])
api_router.include_router(ingest.router, tags=["ingest"])
