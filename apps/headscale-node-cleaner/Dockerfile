FROM ghcr.io/juanfont/headscale:v0.28.0 AS headscale

FROM ghcr.io/flant/shell-operator:v1.4.16

COPY --from=headscale /ko-app/headscale /usr/local/bin/headscale
RUN chmod +x /usr/local/bin/headscale
