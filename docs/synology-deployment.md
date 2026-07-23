# Travel Synology Deployment

`travel-web-next`, `travel-api-fastapi`, and the path-routing Nginx gateway are
deployed as separate containers on the shared external Docker network
`teddy-infra`.

Public routing:

```text
https://map.lafamila.xyz
  -> Synology reverse proxy (127.0.0.1:18043)
  -> travel-gateway-nginx
       /api/*   -> travel-api-fastapi:8010
       /*        -> travel-web-next:3043
```

The commands below assume these source directories on the NAS:

```text
/volume1/www/travel-api-fastapi
/volume1/www/travel-web-next
/volume1/www/travel-gateway-nginx
```

## Prerequisites

Verify the shared network.

```bash
docker network inspect teddy-infra
```

Create the network only when it does not exist.

```bash
docker network create teddy-infra
```

The production API `.env` must use the NAS-native MariaDB through the Docker
host gateway. Do not use `localhost` or `teddy-mysql` for this deployment.

```env
DB_HOST=host.docker.internal
DB_PORT=33306
DB_NAME=travelnote

AUTH_ISSUER_URL=https://auth.lafamila.xyz
AUTH_API_BASE_URL=https://auth.lafamila.xyz
AUTH_JWKS_URL=https://auth.lafamila.xyz/oauth/jwks
AUTH_AUDIENCE=service:travel

TRAVEL_ALLOWED_ORIGINS=https://map.lafamila.xyz
TRAVEL_WEB_BASE_URL=https://map.lafamila.xyz
TRAVEL_OIDC_CLIENT_ID=travel-api
TRAVEL_OIDC_REDIRECT_URI=https://map.lafamila.xyz/api/session/oidc/callback
TRAVEL_SESSION_COOKIE_SECURE=true
TRAVEL_SESSION_COOKIE_SAMESITE=lax
TRAVEL_SESSION_COOKIE_DOMAIN=
TRAVEL_ENABLE_PLAYWRIGHT_FALLBACK=false

TRAVEL_IMPORT_LOCAL_ROOT=
TRAVEL_IMPORT_OUTPUT_ROOT=
TRAVEL_IMPORT_PUBLISH_ENABLED=true
TRAVEL_IMPORT_MAX_UPLOAD_BYTES=2147483648
TRAVEL_IMPORT_MAX_ZIP_FILES=2000
TRAVEL_IMPORT_MAX_ZIP_EXPANDED_BYTES=10737418240
TRAVEL_IMPORT_NOMINATIM_BASE_URL=https://nominatim.openstreetmap.org
TRAVEL_IMPORT_NOMINATIM_USER_AGENT=teddy-travel-import/1.0 (https://map.lafamila.xyz)

STORAGE_BACKEND=r2
S3_ENDPOINT_URL=https://<CLOUDFLARE_ACCOUNT_ID>.r2.cloudflarestorage.com
S3_PUBLIC_BASE_URL=
S3_BUCKET_NAME=teddy-travel-prod
S3_REGION=auto
S3_AUTO_CREATE_BUCKET=false
S3_SIGNED_URL_TTL_SECONDS=600
AWS_ACCESS_KEY_ID=<R2_ACCESS_KEY_ID>
AWS_SECRET_ACCESS_KEY=<R2_SECRET_ACCESS_KEY>
AWS_DEFAULT_REGION=auto
S3_SAVE_STATE_AFTER_UPLOAD=0
S3_STATE_SAVE_STRICT=0
TRAVEL_MEDIA_TEMPORARY_TTL_HOURS=24
```

Keep `TRAVEL_OIDC_CLIENT_SECRET`, `AUTH_SERVICE_KEY_ID`, and
`AUTH_SERVICE_SECRET` in `.env` and never put their values in this document.
The R2 secret access key is also server-only and is displayed only once when
the Cloudflare token is created.

Verify host MariaDB connectivity from a container before starting the API.

```bash
docker run --rm \
  --add-host host.docker.internal:host-gateway \
  alpine:3.20 \
  sh -c 'nc -vz host.docker.internal 33306'
```

## Build Images

Build the API image.

```bash
cd /volume1/www/travel-api-fastapi
docker build -t travel-api-fastapi:latest .
```

Build the web image with the same-origin API path embedded in the client
bundle.

```bash
cd /volume1/www/travel-web-next
docker build \
  --build-arg NEXT_PUBLIC_API_URL=/api \
  -t travel-web-next:latest .
```

## Remove And Run Application Containers

Recreate the API container. `--add-host` is required because MariaDB runs on
the NAS host rather than in Docker.

```bash
docker rm -f travel-api-fastapi 2>/dev/null || true

docker run -d \
  --name travel-api-fastapi \
  --restart unless-stopped \
  --network teddy-infra \
  --add-host host.docker.internal:host-gateway \
  --env-file /volume1/www/travel-api-fastapi/.env \
  travel-api-fastapi:latest
```

Run one worker from the same image. It has no published port and shares the
same MariaDB and R2 configuration. A MariaDB advisory lock
prevents a second worker from sending parallel Nominatim requests.

```bash
docker rm -f travel-import-worker 2>/dev/null || true

docker run -d \
  --name travel-import-worker \
  --restart unless-stopped \
  --network teddy-infra \
  --add-host host.docker.internal:host-gateway \
  --env-file /volume1/www/travel-api-fastapi/.env \
  travel-api-fastapi:latest \
  python -m src.import_worker
```

Recreate the web container. It is reachable only through the shared Docker
network and the gateway.

```bash
docker rm -f travel-web-next 2>/dev/null || true

docker run -d \
  --name travel-web-next \
  --restart unless-stopped \
  --network teddy-infra \
  travel-web-next:latest
```

Check both containers before starting the gateway.

```bash
docker ps --filter name=travel-api-fastapi
docker ps --filter name=travel-web-next
docker logs --tail 100 travel-api-fastapi
docker logs --tail 100 travel-import-worker
```

## Nginx Gateway Configuration

Create the gateway configuration.

```bash
mkdir -p /volume1/www/travel-gateway-nginx

cat > /volume1/www/travel-gateway-nginx/default.conf <<'EOF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    # Must be at least TRAVEL_IMPORT_MAX_UPLOAD_BYTES for ZIP imports.
    client_max_body_size 2g;

    location /api/ {
        proxy_pass http://travel-api-fastapi:8010;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_connect_timeout 10s;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }

    location / {
        proxy_pass http://travel-web-next:3043;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_connect_timeout 10s;
        proxy_read_timeout 120s;
    }
}
EOF
```

Validate the configuration before replacing the running gateway.

```bash
docker run --rm \
  --entrypoint nginx \
  --network teddy-infra \
  -v /volume1/www/travel-gateway-nginx/default.conf:/etc/nginx/conf.d/default.conf:ro \
  nginx:1.27-alpine \
  -t
```

Run the gateway. Overriding the entrypoint avoids the read-only warning from
the stock image's configuration mutation scripts.

```bash
docker rm -f travel-gateway-nginx 2>/dev/null || true

docker run -d \
  --name travel-gateway-nginx \
  --restart unless-stopped \
  --network teddy-infra \
  --entrypoint nginx \
  -p 127.0.0.1:18043:80 \
  -v /volume1/www/travel-gateway-nginx/default.conf:/etc/nginx/conf.d/default.conf:ro \
  nginx:1.27-alpine \
  -g 'daemon off;'
```

## Verification

```bash
docker logs --tail 100 travel-gateway-nginx
curl -I http://127.0.0.1:18043/
curl -i http://127.0.0.1:18043/api/session/me
```

The web request should return `200`. An unauthenticated session request should
reach the API and return `401`.

Configure the Synology reverse proxy separately:

```text
Source:      HTTPS / map.lafamila.xyz / 443
Destination: HTTP  / 127.0.0.1       / 18043
```

Assign the `map.lafamila.xyz` certificate to this rule.

## Subsequent Updates

For an application update, rebuild and recreate only the changed application
container. Restart the gateway afterward because stock Nginx resolves Docker
container names when it starts.

```bash
docker restart travel-gateway-nginx
```
