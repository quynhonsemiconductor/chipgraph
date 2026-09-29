"""A local plugin that fails at import time, to test error handling."""

raise RuntimeError("boom at import time")
