"""ASGI entry point (Upsun: `uvicorn app.asgi:application --port $PORT`)."""

from app.main import create_app

application = create_app()
