import aiomysql
import logging
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MySQLConnectionManager:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 3306,
        user: str = "easytrade",
        password: str = "masterkey",
        db: str = "easytrade_db",
        minsize: int = 1,
        maxsize: int = 10,
        pool_recycle: int = 3600,
        autocommit: bool = True,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.db = db
        self.minsize = minsize
        self.maxsize = maxsize
        self.pool_recycle = pool_recycle
        self.autocommit = autocommit
        self.pool: Optional[aiomysql.Pool] = None

    async def connect(self):
        """Initialize the connection pool."""
        if self.pool is None:
            try:
                self.pool = await aiomysql.create_pool(
                    host=self.host,
                    port=self.port,
                    user=self.user,
                    password=self.password,
                    db=self.db,
                    minsize=self.minsize,
                    maxsize=self.maxsize,
                    pool_recycle=self.pool_recycle,
                    autocommit=self.autocommit,
                )
                logger.info("MySQL connection pool created successfully")
            except Exception as e:
                logger.error("Failed to create connection pool: %s", e)
                raise

    async def disconnect(self):
        """Close the connection pool."""
        if self.pool is not None:
            self.pool.close()
            await self.pool.wait_closed()
            self.pool = None
            logger.info("MySQL connection pool closed")

    async def is_connected(self) -> bool:
        """Check if the connection pool is active."""
        if self.pool is None:
            return False
        try:
            async with self.pool.acquire() as conn:
                await conn.ping()
                return True
        except Exception as e:
            logger.error("Connection check failed: %s", e)
            return False

    async def execute(self, query: str, args=None):
        """Execute a query and return results."""
        if self.pool is None:
            raise RuntimeError("Connection pool is not initialized. Call connect() first.")
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(query, args)
                return await cursor.fetchall()

    async def execute_one(self, query: str, args=None):
        """Execute a query and return a single result."""
        if self.pool is None:
            raise RuntimeError("Connection pool is not initialized. Call connect() first.")
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(query, args)
                return await cursor.fetchone()

    async def execute_insert(self, query: str, args=None) -> int:
        """Execute an INSERT and return lastrowid."""
        if self.pool is None:
            raise RuntimeError("Connection pool is not initialized. Call connect() first.")
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(query, args)
                return cursor.lastrowid

    async def execute_many(self, query: str, args_list):
        """Execute a query with multiple parameter sets."""
        if self.pool is None:
            raise RuntimeError("Connection pool is not initialized. Call connect() first.")
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.executemany(query, args_list)
                await conn.commit()
                return cursor.rowcount
