from fastapi import APIRouter

from app.api.routes import items, login, private, users, utils
from app.core.config import settings
from app.features.global_deduplication import routes as global_deduplication
from app.features.markdown_cleaning import routes as markdown_cleaning
from app.features.structured_extraction import routes as structured_extraction
from app.features.text_classification import routes as text_classification

api_router = APIRouter()
api_router.include_router(login.router)
api_router.include_router(users.router)
api_router.include_router(utils.router)
api_router.include_router(items.router)
api_router.include_router(markdown_cleaning.router)
api_router.include_router(structured_extraction.router)
api_router.include_router(global_deduplication.router)
api_router.include_router(text_classification.router)


if settings.ENVIRONMENT == "local":
    api_router.include_router(private.router)
