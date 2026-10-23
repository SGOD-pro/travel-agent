import asyncio
from src.config.db import engine
from src.config.base_model import Base
from src.functions.api.models import JobModel, OutboxModel

async def init_models():
    async with engine.begin() as conn:
        print("Creating local database tables...")
        await conn.run_sync(Base.metadata.create_all)
        print("Done!")

if __name__ == "__main__":
    asyncio.run(init_models())
