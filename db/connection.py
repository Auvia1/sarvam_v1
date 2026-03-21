import os
import asyncpg
from loguru import logger
from db.queries import cleanup_expired_pending_appointments

async def get_db_pool():
    try:
        pool = await asyncpg.create_pool(os.getenv("DATABASE_URL"))
        logger.info("✅ Database pool created successfully.")
        
        # 🔥 Run the 15-minute cleanup sweep immediately on boot
        await cleanup_expired_pending_appointments(pool)
        
        return pool
    except Exception as e:
        logger.error(f"❌ Failed to connect to database: {e}")
        raise