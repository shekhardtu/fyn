FROM node:24-alpine AS dependencies
WORKDIR /app
COPY package.json yarn.lock ./
RUN yarn install --frozen-lockfile

FROM node:24-alpine AS builder
WORKDIR /app

# Vite embeds these public values into the browser bundle at build time.
# VITE_API_URL stays empty so the bundle talks to its own origin and nginx
# relays /api to BACKEND_ORIGIN below.
ARG VITE_API_URL=
ARG VITE_GOOGLE_CLIENT_ID=
ENV VITE_API_URL=$VITE_API_URL
ENV VITE_GOOGLE_CLIENT_ID=$VITE_GOOGLE_CLIENT_ID

# Caps the Vite build's heap. On a small shared host the bundler is the largest
# memory consumer on the box, and an uncapped Node heap grows until the kernel
# OOM killer picks a victim — which may be a neighbouring application, not this
# build. BuildKit ignores `docker build --memory`, so the ceiling has to be set
# inside the build. Empty by default: local builds stay unconstrained.
ARG NODE_OPTIONS=
ENV NODE_OPTIONS=$NODE_OPTIONS

COPY --from=dependencies /app/node_modules ./node_modules
COPY . .
# Nginx cannot import the browser's TypeScript constant, so render its route
# from that same source while Node is still available in the build stage.
RUN API_MOUNT_PATH="$(node --input-type=module -e 'import("./src/config/api-path.ts").then(module => process.stdout.write(module.API_MOUNT_PATH))')" \
  && sed "s|__API_MOUNT_PATH__|${API_MOUNT_PATH}|g" nginx.conf.template > default.conf.template
RUN yarn build

FROM nginxinc/nginx-unprivileged:1.29-alpine AS runner
# The entrypoint substitutes ${BACKEND_ORIGIN} into the template at start,
# so one image serves any deployment: set BACKEND_ORIGIN to the API's
# internal address (e.g. http://api:8000) when running the container.
ENV BACKEND_ORIGIN=http://localhost:8000
COPY --from=builder /app/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=builder --chown=101:101 /app/dist /usr/share/nginx/html

EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1
