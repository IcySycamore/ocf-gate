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
COPY .github/ocf/requirements.txt .github/ocf/
RUN pip install --no-cache-dir -r .github/ocf/requirements.txt

COPY . .

# Runs the case table (each case through the real hook entry point) plus the structural checks: repository
# ASCII, markdown links, section references, agent cross-references, gate-file protection, and the rest
# of the tuple in run.py's CHECKS.
#
# Deliberately the same command a human runs on the host, with no test framework in between: the
# suite needs only CPython, so there is nothing for a framework to arrange. An earlier revision used
# `python -m pytest`, which collected nothing at all, because pytest only collects test_*.py and the
# runner is run.py. Docker found that; nothing on the host would have.
CMD ["python", ".github/ocf/tests/run.py"]
