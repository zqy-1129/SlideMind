$ErrorActionPreference = "Stop"

docker compose config | Select-String "image:|PYTHON_IMAGE|NODE_IMAGE|docker.io|daocloud|quay.io"

