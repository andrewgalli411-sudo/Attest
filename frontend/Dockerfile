FROM node:22-alpine AS build
RUN corepack enable
WORKDIR /web
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
RUN pnpm build

FROM node:22-alpine
RUN corepack enable && addgroup -S web && adduser -S web -G web
WORKDIR /web
ENV NODE_ENV=production
COPY --from=build --chown=web:web /web ./
USER web
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1
CMD ["pnpm", "start", "--hostname", "0.0.0.0"]
