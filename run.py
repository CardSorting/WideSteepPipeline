from hypercorn.config import Config
from hypercorn.asyncio import serve
import asyncio
from app import app

if __name__ == "__main__":
    config = Config()
    config.bind = ["0.0.0.0:8080"]
    config.use_reloader = True
    asyncio.run(serve(app, config))