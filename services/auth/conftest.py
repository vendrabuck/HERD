import os

# Set DB_SCHEMA to empty so models use no schema prefix (SQLite compatible)
os.environ["DB_SCHEMA"] = ""
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"
