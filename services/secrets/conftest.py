import base64
import os

# Set DB_SCHEMA to empty so models use no schema prefix (SQLite compatible)
os.environ["DB_SCHEMA"] = ""
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ACL_SERVICE_URL"] = "http://test-acl:8000"
os.environ["INTERNAL_API_TOKEN"] = "test-token"
os.environ["SECRETS_KEK"] = base64.b64encode(b"unit-test-kek-32-bytes-exactly!!").decode()
