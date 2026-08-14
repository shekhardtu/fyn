FROM node:24-alpine AS dependencies
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:24-alpine AS builder
WORKDIR /app

# Vite embeds these public values into the browser bundle at build time.
# VITE_API_URL stays empty so the bundle talks to its own origin and nginx
# relays /api to BACKEND_ORIGIN below.
ARG VITE_API_URL=
ARG VITE_GOOGLE_CLIENT_ID=
ENV VITE_API_URL=$VITE_API_URL
ENV VITE_GOOGLE_CLIENT_ID=$VITE_GOOGLE_CLIENT_ID

COPY --from=dependencies /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.29-alpine AS runner
# The entrypoint substitutes ${BACKEND_ORIGIN} into the template at start,
# so one image serves any deployment: set BACKEND_ORIGIN to the API's
# internal address (e.g. http://api:8000) when running the container.
ENV BACKEND_ORIGIN=http://localhost:8000
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=builder --chown=101:101 /app/dist /usr/share/nginx/html

EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1
