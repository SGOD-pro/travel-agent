from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

# Using SQLite temporarily so you don't have to install PostgreSQL immediately to test it.
engine = create_async_engine("sqlite+aiosqlite:///./planner.db", echo=True)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)
