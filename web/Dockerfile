# syntax=docker/dockerfile:1

# Stage 1: build the PWA to static files. Its source is the central_node_pwa
# repo, handed in by docker-compose as the `pwa` build context. Only the
# files the build needs are copied, so a local node_modules/ or .next/ never
# ends up in the image.
FROM node:22-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1

COPY --from=pwa package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY --from=pwa next.config.ts tsconfig.json postcss.config.mjs ./
COPY --from=pwa src ./src
COPY --from=pwa public ./public
# Google Fonts are downloaded here and bundled into out/, so the app is fully
# self-hosted and works on the Pi's hotspot without internet access.
RUN npm run build

# Stage 2: NGINX serves the built app and proxies /api/ to ingest.
FROM nginx:alpine

COPY nginx/pwa-locations.conf nginx/http.conf nginx/https.conf /etc/nginx/
COPY --chmod=755 nginx/40-choose-http-or-https.sh /docker-entrypoint.d/
COPY --from=build /app/out/ /usr/share/nginx/html/
