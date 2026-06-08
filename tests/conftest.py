"""Test bootstrap.

`utils.pti_processor` imports `data.config` and `loader` at module load — those
read env vars and construct an aiogram Bot. Provide harmless, well-formed dummies
(no real secrets, no network) so the pure-function tests can import the module.
"""
import os

os.environ.setdefault("BOT_TOKEN", "123456:AAH-fake-token-for-tests-only")
os.environ.setdefault("ADMINS", "123456789")
os.environ.setdefault("ip", "127.0.0.1")
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")
