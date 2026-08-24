import os

os.environ["DB_SCHEMA"] = ""
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["INTERNAL_API_TOKEN"] = "test-token"
# Unroutable off-host, so a test that forgets to patch the published-schema
# resolver never depends on host DNS/network behavior (issue #567). Loopback
# with nothing listening, not an RFC 5737 TEST-NET address: TEST-NET addresses
# are typically blackholed, so every unpatched call blocks for the full httpx
# connect timeout (measured ~10s each) rather than failing closed immediately;
# several tests hit this per suite run, which turned "hermetic" into "hermetic
# but several minutes slower." A refused loopback connection is just as
# unroutable off-host and fails in milliseconds via ConnectionRefusedError.
os.environ["EXECUTION_SERVICE_URL"] = "http://127.0.0.1:1"
