#!/bin/sh
# Build the etl+ image and pack it for a server without internet:
#   dist/etlplus-<version>-offline/ { etlplus-<version>.tar, docker-compose.yml, LOAD_AND_RUN.md }
# and the same folder as a .tar.gz beside it. Run from anywhere: sh scripts/offline-bundle.sh
# --from-image REF packs an image built already (tagged as the version) instead of building.
set -eu

FROM_IMAGE=""
if [ "${1:-}" = "--from-image" ]; then FROM_IMAGE="${2:?--from-image needs an image}"; fi

cd "$(dirname "$0")/.."
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1)
IMAGE="crestedcrane/etlplus:${VERSION}"
OUT="dist/etlplus-${VERSION}-offline"

if [ -n "${FROM_IMAGE}" ]; then
  docker tag "${FROM_IMAGE}" "${IMAGE}"
else
  echo "Building ${IMAGE} ..."
  docker build -t "${IMAGE}" .
fi

rm -rf "${OUT}"
mkdir -p "${OUT}"
echo "Saving the image ..."
docker save "${IMAGE}" -o "${OUT}/etlplus-${VERSION}.tar"

# the compose file without the build line: the server only loads the image
sed '/^    build: \.$/d' docker-compose.yml > "${OUT}/docker-compose.yml"

cat > "${OUT}/LOAD_AND_RUN.md" <<EOF
# etl+ ${VERSION}, offline
1. Copy this folder to the server (Docker with the compose plugin installed).
2. \`docker load -i etlplus-${VERSION}.tar\`
3. \`docker compose up -d\` in this folder.
4. Open http://localhost:8300 on the server (or \`ssh -L 8300:127.0.0.1:8300 <server>\`) and choose demo data or your own data.
EOF

tar -czf "${OUT}.tar.gz" -C dist "etlplus-${VERSION}-offline"
ls -l "${OUT}" "${OUT}.tar.gz"
echo "Done: ${OUT}.tar.gz"
