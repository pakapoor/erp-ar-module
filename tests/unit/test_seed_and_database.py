import unittest
from unittest.mock import AsyncMock, Mock, patch

from src import database, seed_data


class AsyncContext:
    def __init__(self, value): self.value = value
    async def __aenter__(self): return self.value
    async def __aexit__(self, *_args): return False


class SeedAndDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_seed_executes_all_idempotent_groups(self):
        connection = AsyncMock()
        engine = Mock()
        engine.begin.return_value = AsyncContext(connection)
        engine.dispose = AsyncMock()
        with patch.object(seed_data, "engine", engine), patch("builtins.print"):
            await seed_data.seed_data()
        self.assertEqual(connection.execute.await_count, 6)
        engine.dispose.assert_awaited_once()

    async def test_get_db_yields_and_closes_session(self):
        session = AsyncMock()
        factory = Mock(return_value=AsyncContext(session))
        with patch.object(database, "AsyncSessionLocal", factory):
            values = [value async for value in database.get_db()]
        self.assertEqual(values, [session])

    async def test_transaction_commit_and_rollback(self):
        session = AsyncMock()
        factory = Mock(return_value=AsyncContext(session))
        with patch.object(database, "AsyncSessionLocal", factory):
            values = [value async for value in database.get_db_transaction()]
        self.assertEqual(values, [session])
        session.commit.assert_awaited_once()

        failing = AsyncMock()
        factory = Mock(return_value=AsyncContext(failing))
        with patch.object(database, "AsyncSessionLocal", factory):
            generator = database.get_db_transaction("serializable")
            self.assertIs(await anext(generator), failing)
            with self.assertRaises(RuntimeError):
                await generator.athrow(RuntimeError("transaction failure"))
        failing.rollback.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
