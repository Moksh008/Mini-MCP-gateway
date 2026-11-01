# syntax=docker/dockerfile:1
# Build context: repository root (the experiment page embeds experiments/results.json).
FROM node:22-alpine AS build
WORKDIR /src/console
COPY console/package.json console/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY console/ ./
COPY experiments/results.json /src/experiments/results.json
ARG VITE_BACKEND_HOST=backends
ENV VITE_BACKEND_HOST=${VITE_BACKEND_HOST}
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY console/nginx.conf /etc/nginx/conf.d/default.conf
COPY console/security-headers.conf console/proxy.conf /etc/nginx/snippets/
COPY --from=build /src/console/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/ >/dev/null || exit 1
