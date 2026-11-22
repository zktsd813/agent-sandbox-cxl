# Code-interpreter sandbox for the footprint measurement (math RL: ReTool, rStar2-Agent, ToRL).
# Built by `ci_traces.py image`; base pinned to the python:3.11-slim index digest of 2026-11-18.
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535
RUN pip install --no-cache-dir numpy==2.2.6 sympy==1.14.0 scipy==1.15.3 mpmath==1.3.0
COPY kernel.py ci.sh /opt/ci/
RUN chmod 755 /opt/ci/kernel.py /opt/ci/ci.sh && mkdir -p /work
WORKDIR /work
