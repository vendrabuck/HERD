import os

os.environ["DB_SCHEMA"] = ""
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["AUTH_SERVICE_URL"] = "http://test-auth:8000"
os.environ["INTERNAL_API_TOKEN"] = "test-internal-token"
