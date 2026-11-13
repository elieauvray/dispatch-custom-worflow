"""Point d'entrée ASGI (Upsun : `uvicorn app.asgi:application --port $PORT`)."""

from app.main import create_app

application = create_app()
