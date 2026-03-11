import os
import asyncpg
from loguru import logger

async def get_db_pool():
    try:
        pool = await asyncpg.create_pool(os.getenv("DATABASE_URL"))
        logger.info("✅ Database pool created successfully.")
        return pool
    except Exception as e:
        logger.error(f"❌ Failed to connect to database: {e}")
        raise