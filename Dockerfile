# Test and CI environment only.
#
# This container is NOT how the gate runs. VS Code starts the hook process on the HOST, so wrapping
# ocf.py in a container would not put the gate inside it. That is exactly why the hook entry point
# imports nothing outside the standard library: a missing third-party import on the host would make
# the gate silently fail open, which is the failure mode this project exists to remove.
#
# Docker is here so the test suite can run identically anywhere:
#     docker build -t ocf-tests .
#     docker run --rm ocf-tests

FROM python:3.12-slim

WORKDIR /work

# Dependencies first, so a source change does not invalidate the pip layer.
COPY .github/ocf/requirements.txt .github/ocf/requirements-dev.txt .github/ocf/
RUN pip install --no-cache-dir -r .github/ocf/requirements-dev.txt

COPY . .

# Runs the case table (each case through the real hook entry point) plus the structural checks:
# repository ASCII, markdown links, agent cross-references, and gate-file protection.
CMD ["python", "-m", "pytest", ".github/ocf/tests", "-q"]
