FROM alpine:3.22

RUN apk add --no-cache \
    bash \
    kubectl \
    openssh-client \
    sshpass

COPY join.sh /usr/local/bin/join
RUN chmod +x /usr/local/bin/join

ENTRYPOINT ["/usr/local/bin/join"]
