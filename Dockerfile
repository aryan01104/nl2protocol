FROM python:3.12-slim
# Pinned to 3.12 (not 3.13) because opentrons pulls numpy 1.26.4, which
# has prebuilt manylinux wheels for 3.12 but not 3.13 — building numpy
# from source needs meson + a C toolchain, bloats the image, and slows
# the deploy by minutes. pyproject's `requires-python = ">=3.10"` so
# this is in-bounds.

WORKDIR /app

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
