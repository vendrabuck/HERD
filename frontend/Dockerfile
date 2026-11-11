# Build stage
FROM node:22.23.4-alpine@sha256:0e1754758c2df4cfa3e68d59e2e1b7374bb7fd9ea6328eaaacf15cc934af8202 AS build

WORKDIR /app

# Deliberately package.json and package-lock.json* only, not the whole
# context (that is the later `COPY . .`): frontend/.npmrc sets
# engine-strict=true (issue #885) so that a local `npm ci`/`npm install`
# refuses on an npm older than package.json's engines.npm floor. This
# image's base, node:22 on Alpine, ships whatever npm was bundled with that
# tag, which is not guaranteed to meet that floor. Keeping .npmrc out of
# the build context until after the install step means engine-strict is
# never in effect here, so the bundled npm only WARNS on an engines
# mismatch (npm's default, non-strict behavior) instead of failing the
# image build. Do not fold .npmrc into this COPY line or move `COPY . .`
# ahead of the install without also pinning a compliant npm here first.
COPY package.json package-lock.json* ./

# Install dependencies (use npm ci if lock file exists, otherwise npm install)
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .

# Vite bakes VITE_* vars at build time. Accept it as a build arg so the
# compose .env value reaches `npm run build` (defaults to false if unset).
ARG VITE_AI_CHAT_ENABLED=false
ENV VITE_AI_CHAT_ENABLED=${VITE_AI_CHAT_ENABLED}

# Build identifier (issue #846), computed on the host by the Makefile and
# passed through docker-compose.yml as HERD_BUILD/HERD_BUILD_DATE (mapped to
# these VITE_-prefixed names here since only VITE_-prefixed vars are baked
# into the Vite build). Placed after npm ci so the dependency install layer
# stays cached across build-string changes.
ARG VITE_HERD_BUILD=dev
ARG VITE_HERD_BUILD_DATE=
ENV VITE_HERD_BUILD=${VITE_HERD_BUILD} VITE_HERD_BUILD_DATE=${VITE_HERD_BUILD_DATE}

RUN npm run build

# Serve stage: the nginx stable line (1.30), not mainline.
FROM nginx:1.30.6-alpine@sha256:583b7f2fce1cdb8da5ad5c39ebd3c33b98b87d493190e548bdee1fa8c619c311

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
