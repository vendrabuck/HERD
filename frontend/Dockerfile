# Build stage
FROM node:22-alpine AS build

WORKDIR /app

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

# Serve stage
FROM nginx:1.25-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
