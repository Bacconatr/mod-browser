# One image, no build step and no runtime dependencies: Node 24 runs the
# TypeScript sources directly (type stripping). git is here because the mod
# list's "added by / added on" is read from the pack repository's history.
FROM node:24-alpine
RUN apk add --no-cache git
WORKDIR /app
COPY package.json ./
COPY src src
ENV NODE_ENV=production PORT=3000 HOST=0.0.0.0
USER node
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:3000/api/health || exit 1
CMD ["node", "src/server.ts"]
