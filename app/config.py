import os

from dotenv import load_dotenv

load_dotenv()  # never overrides a variable that is already set

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/billing")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

if STRIPE_SECRET_KEY and not STRIPE_SECRET_KEY.startswith(("sk_test_", "rk_test_")):
    # The brief: test mode only. A live key here is refused, not used.
    raise SystemExit("STRIPE_SECRET_KEY must be a TEST key (sk_test_...). Live keys are refused.")
