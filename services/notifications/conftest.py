import os

os.environ["DB_SCHEMA"] = ""
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["INTERNAL_API_TOKEN"] = "test-internal-token"
os.environ["USER_PROFILE_SERVICE_URL"] = "http://test-user-profile:8000"
os.environ["NATS_URL"] = "nats://test-nats:4222"
