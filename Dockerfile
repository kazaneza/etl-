# etl+ in a container: the API and console on port 8300, the home (both projects) on a volume.
FROM python:3.12-slim-bookworm

# Microsoft ODBC Driver 18 for SQL Server and unixODBC (pyodbc needs them). Debian packages over https.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl gnupg2 ca-certificates apt-transport-https \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
 && apt-get purge -y gnupg2 \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY etlplus ./etlplus
# The common drivers: PostgreSQL, SQL Server, MySQL/MariaDB, Oracle, ClickHouse, MongoDB, SFTP, S3,
# Azure Blob and Excel. Db2, Snowflake, SAP HANA, Teradata and Firebird are heavy or need native
# libraries: pip install etlplus[db2] (and so on) in a derived image.
RUN pip install --no-cache-dir ".[common]"

COPY docker/entrypoint.sh /usr/local/bin/etlplus-start
RUN sed -i 's/\r$//' /usr/local/bin/etlplus-start && chmod 755 /usr/local/bin/etlplus-start

RUN useradd --create-home --uid 10001 etlplus \
 && mkdir -p /project \
 && chown etlplus:etlplus /project
USER etlplus

ENV ETLPLUS_PROJECT=/project \
    ETLPLUS_START=ask \
    ETLPLUS_PORT=8300 \
    PYTHONUNBUFFERED=1
VOLUME ["/project"]
EXPOSE 8300

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8300/health', timeout=4).status == 200 else 1)"

# ETLPLUS_START=ask shows the choice (demo data or your own data) in the console; demo or empty
# make it on the first start. 0.0.0.0 is the container's own interface; compose publishes it on
# the host's 127.0.0.1 only.
ENTRYPOINT ["etlplus-start"]
