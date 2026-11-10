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

# .env holds the hash of the app's login password (NEXT_PUBLIC_PASSWORD_HASH),
# which the build bundles into the app. It isn't in git, so it is copied only
# when it's there: "[v]" makes it a pattern, and a pattern may match nothing.
# Without it the app builds, but nobody can log in.
# .env.local is left out on purpose: it holds a developer's own settings, like
# the address of the API on their laptop.
COPY --from=pwa next.config.ts tsconfig.json postcss.config.mjs .en[v] ./
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
