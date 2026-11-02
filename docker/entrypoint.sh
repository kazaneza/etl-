#!/bin/sh
# Start etl+ on the home volume. ETLPLUS_START: ask (the default; the console offers demo data or
# your own data), demo or empty (the server makes that choice itself when nothing is active, the
# demo jobs run in the background). A 0.1.0 project at the volume root is served as it is.
set -e
exec etlplus serve --home "${ETLPLUS_PROJECT:-/project}" --start "${ETLPLUS_START:-ask}" --host 0.0.0.0 --port 8300
