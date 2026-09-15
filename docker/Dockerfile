FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml .
COPY billguard ./billguard
COPY skills ./skills
RUN pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python"]
