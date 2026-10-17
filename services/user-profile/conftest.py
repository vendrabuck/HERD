import os

os.environ["DB_SCHEMA"] = ""
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["SECRET_KEY"] = "3fcbb4bf16b0e162be24d2eb59065316c1e84194669ebf9270203280b18e18f7"
os.environ["AUTH_SERVICE_URL"] = "http://test-auth:8000"
os.environ["INTERNAL_API_TOKEN"] = "test-internal-token"
