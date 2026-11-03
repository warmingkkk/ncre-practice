FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home app
COPY --chown=app:app app.py importers.py ./
COPY --chown=app:app templates templates
COPY --chown=app:app static static
COPY --chown=app:app seed seed
COPY --chown=app:app scripts scripts
RUN mkdir -p /app/data && chown app:app /app/data
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["waitress-serve", "--host=0.0.0.0", "--port=8080", "--threads=8", "--call", "app:create_app"]
