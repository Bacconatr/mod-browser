# The mod site for an Aeronautics Minecraft server. Standard library only:
# it is meant to face the Internet, so it carries no third-party
# Python packages. git reads the pack's history; tzdata reads the log's times.
FROM python:3.13.17-alpine3.24

LABEL org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/Bacconatr/mod-browser"

RUN apk add --no-cache git tzdata

WORKDIR /app
COPY mod_browser /app/mod_browser

# The rootfs is read-only at runtime (compose), so no .pyc writes and a HOME
# git can read without complaint.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

# uid 1000 owns the RCON password file on the host (0400); nothing else here
# needs a name.
USER 1000:1000
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-m", "mod_browser.healthcheck"]
CMD ["python", "-m", "mod_browser"]
