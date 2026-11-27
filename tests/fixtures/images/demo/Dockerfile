# The container sandbox's image for the definition-of-done run (ADR-019), when
# tests/integration/test_demo.py runs it with [sandbox].backend = "container": Python, and
# the pytest and ruff that uv.lock pins, so it gives the same answers as the local run.
# Nothing else: the container has no network to install anything in, and the repository's
# code is mounted, not copied. CI builds it and names it in MSA_DEMO_IMAGE:
#
#     docker build --tag msa-demo-sandbox tests/fixtures/images/demo
#     MSA_DEMO_IMAGE=msa-demo-sandbox uv run pytest tests/integration/test_demo.py
#
# requirements.txt is generated from uv.lock, with every dependency and its hashes, and
# tests/unit/test_demo_image.py keeps it so. The base is pinned by digest, so a rebuilt tag
# changes nothing; to move it, `docker buildx imagetools inspect python:3.13-slim`.
FROM python:3.13-slim@sha256:7249d5587b78209a006dc4a3b5340678f84ea729239193aa559962cd8c1fce50

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
