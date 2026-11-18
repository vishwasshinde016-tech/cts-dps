"""Document Processing Service (DPS)."""

import os

# CrewAI's anonymous usage telemetry and tracing send data to crewai.com. Document data and job details stay in the
# bank: both are off for every DPS process before CrewAI is imported (also set in the Dockerfile).
for _flag in ("CREWAI_DISABLE_TELEMETRY", "CREWAI_DISABLE_TRACKING"):
    os.environ.setdefault(_flag, "true")
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")

__version__ = "0.1.0"
