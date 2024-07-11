######### BASE Image #########
FROM python:3.12-slim AS base
LABEL maintainer="Iain Rauch <6860163+Yanson@users.noreply.github.com>"

ARG APP_NAME=octograph
ARG VERSION=dev

ENV APP_NAME=${APP_NAME}
ENV VERSION=${VERSION}

ENV POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN mkdir -p "/opt/$APP_NAME"
WORKDIR "/opt/$APP_NAME"

COPY poetry.lock pyproject.toml ./
COPY app app

RUN python -m pip install --no-cache-dir --upgrade pip poetry wheel && \
    poetry install --only main --no-root

######### TEST Image #########
FROM base AS test
COPY tests tests
RUN poetry install
RUN poetry run pytest -v tests/unit

######### PRODUCTION Image #########
FROM base AS production
RUN poetry install --only main

ENTRYPOINT ["python", "/opt/octograph/app/octopus_to_influxdb.py"]
