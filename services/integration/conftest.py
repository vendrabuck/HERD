import os

# Set DB_SCHEMA to empty so models use no schema prefix (SQLite compatible)
os.environ["DB_SCHEMA"] = ""
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["RESERVATIONS_SERVICE_URL"] = "http://test-reservations:8000"
os.environ["INTERNAL_API_TOKEN"] = "test-token"
