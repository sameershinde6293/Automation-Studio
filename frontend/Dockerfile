# Creator OS frontend image (M5).
#
# Builds the Vite bundle and serves it from nginx. The Electron desktop shell
# is a separate artifact (`npm run electron:build`) and is not part of this
# image, which exists for browser-served deployments.

# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
FROM node:22-alpine AS builder

WORKDIR /build

# Electron ships a large binary that this image never runs; skipping the
# download keeps the build fast and works behind TLS-inspecting proxies.
ENV ELECTRON_SKIP_BINARY_DOWNLOAD=1

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .

# Baked in at build time: Vite inlines VITE_* variables into the bundle, so
# this cannot be changed after the image is built. Override with
# `--build-arg VITE_API_BASE=https://api.example.com/api`.
ARG VITE_API_BASE=/api
ENV VITE_API_BASE=$VITE_API_BASE

# `npm run build` also compiles the Electron main process, which this image
# does not need; tsc + vite build alone keep the image lean.
RUN npx tsc && npx vite build

# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #
FROM nginx:1.27-alpine AS runtime

RUN apk add --no-cache curl

COPY --from=builder /build/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

# nginx's unprivileged variant already runs worker processes as `nginx`; the
# master needs to bind :80 inside the container, which is namespaced.
EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
