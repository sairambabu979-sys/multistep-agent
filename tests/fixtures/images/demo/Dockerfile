# The container sandbox's image for the definition-of-done run (ADR-019), when
# tests/integration/test_demo.py runs it with [sandbox].backend = "container": Python, and
# the pytest and ruff that uv.lock pins, so it gives the same answers as the local run.
# Nothing else: the container has no network to install anything in, and the repository's
# code is mounted, not copied. CI builds it and names it in MSA_DEMO_IMAGE:
#
#     docker build --tag msa-demo-sandbox tests/fixtures/images/demo
#     MSA_DEMO_IMAGE=msa-demo-sandbox uv run pytest tests/integration/test_demo.py
#
# tests/unit/test_demo_image.py keeps these versions equal to uv.lock's.
FROM python:3.13-slim

RUN pip install --no-cache-dir --only-binary=:all: pytest==9.2.0 ruff==0.17.3
