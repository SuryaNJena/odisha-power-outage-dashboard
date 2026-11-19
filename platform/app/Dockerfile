# Odisha outage dashboard runtime image.
#
# The build context is prepared by scripts/deploy.ps1:
#   platform/app/odisha-backend   linux/amd64 static Go binary
#   platform/app/out/             Next.js static export
#   platform/app/data/mappings/   reference data (fallback)
#   platform/app/data/mart/       latest datamart snapshot (fallback)
FROM alpine:3.20

RUN apk add --no-cache ca-certificates tzdata wget && \
    adduser -D -u 10001 odisha

WORKDIR /app
COPY --chown=odisha:odisha odisha-backend /app/odisha-backend
COPY --chown=odisha:odisha out /app/out
COPY --chown=odisha:odisha data /app/data

ENV PORT=3001 \
    WEB_DIR=/app/out \
    DATA_DIR=/opt/odisha-data \
    TZ=Asia/Kolkata

USER odisha
EXPOSE 3001
ENTRYPOINT ["/app/odisha-backend"]
