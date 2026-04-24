from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

try:
    from .connectors import init_db
    from .routers.courses import router as courses_router
    from .routers.places import router as places_router
    from .routers.uploads import router as uploads_router
    from .services.storage import ensure_bucket
except ImportError:  # pragma: no cover
    from connectors import init_db
    from routers.courses import router as courses_router
    from routers.places import router as places_router
    from routers.uploads import router as uploads_router
    from services.storage import ensure_bucket


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_bucket()

    # Initialize Playwright browser (persistent, shared across requests)
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    app.state.browser = browser
    app.state.playwright = pw
    print("Travel API Server started successfully (Playwright browser ready)")

    yield

    # Cleanup Playwright
    await browser.close()
    await pw.stop()


app = FastAPI(title="Travel API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(places_router)
app.include_router(courses_router)
app.include_router(uploads_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8010)
