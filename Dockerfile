FROM python:3.13-slim

WORKDIR /app

# opentrons + some transitive deps may need a C toolchain; install if pip
# wheels aren't available for the target platform. Comment back in if a
# fly.io build fails with "missing compiler" errors.
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     build-essential \
#     && rm -rf /var/lib/apt/lists/*

# Copy project metadata first so Docker can cache the dep install layer
# across edits that only touch source.
COPY pyproject.toml README.md ./
COPY nl2protocol ./nl2protocol
COPY test_cases ./test_cases

# Editable install so Path(__file__).parents[2] resolves to /app at runtime
# (the examples_dir lookup relies on this). Production-fine here since the
# image is single-purpose.
RUN pip install --no-cache-dir -e .

EXPOSE 8080

# Bind 0.0.0.0 (container-exposed); --no-open-browser is mandatory in
# a headless container.
CMD ["nl2protocol", "--serve", "--serve-host", "0.0.0.0", "--serve-port", "8080", "--no-open-browser"]
