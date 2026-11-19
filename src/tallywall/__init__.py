import logging


def main() -> None:
    import uvicorn

    from .app import create_app
    from .config import load_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    logging.getLogger(__name__).info("%r", settings)
    # X-Forwarded-For is handled in the app (TRUSTED_PROXIES), not by uvicorn.
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, proxy_headers=False)
