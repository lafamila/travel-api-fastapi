from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

try:
    from .config import TRAVEL_ALLOWED_ORIGINS, TRAVEL_ENABLE_PLAYWRIGHT_FALLBACK
    from .connectors import init_db
    from .routers.courses import router as courses_router
    from .routers.friends import router as friends_router
    from .routers.imports import router as imports_router
    from .routers.places import router as places_router
    from .routers.session import router as session_router
    from .routers.uploads import router as uploads_router
    from .services.storage import ensure_bucket
except ImportError:  # pragma: no cover
    from config import TRAVEL_ALLOWED_ORIGINS, TRAVEL_ENABLE_PLAYWRIGHT_FALLBACK
    from connectors import init_db
    from routers.courses import router as courses_router
    from routers.friends import router as friends_router
    from routers.imports import router as imports_router
    from routers.places import router as places_router
    from routers.session import router as session_router
    from routers.uploads import router as uploads_router
    from services.storage import ensure_bucket


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_bucket()

    browser = None
    playwright = None
    if TRAVEL_ENABLE_PLAYWRIGHT_FALLBACK:
        try:
            from playwright.async_api import async_playwright

            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=True)
        except Exception:
            logging.getLogger(__name__).exception(
                "Playwright fallback is unavailable; static map parsers remain active"
            )
    app.state.browser = browser
    app.state.playwright = playwright
    print("Travel API Server started successfully")

    yield

    if browser:
        await browser.close()
    if playwright:
        await playwright.stop()


app = FastAPI(title="Travel API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=TRAVEL_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(places_router)
app.include_router(courses_router)
app.include_router(uploads_router)
app.include_router(friends_router)
app.include_router(session_router)
app.include_router(imports_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8010)
