FROM alpine:3.22 AS builder

RUN apk add alpine-sdk cmd:useradd sudo
RUN useradd -m -G abuild user
RUN echo "%abuild ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/abuild

USER user
WORKDIR /home/user

RUN abuild-keygen -nai

ADD petname.APKBUILD .
RUN APKBUILD=petname.APKBUILD abuild -r

RUN find packages

FROM alpine:3.22

# (Temporarily) enable testing repo to install butane.
RUN echo '@testing https://dl-cdn.alpinelinux.org/alpine/edge/testing' >> /etc/apk/repositories

RUN --mount=type=bind,from=builder,source=/home/user/packages/home,target=/pkg \
    apk add --allow-untrusted \
        /pkg/*/*.apk \
        bash \
        butane@testing \
        curl \
        fcgiwrap \
        jq \
        kubectl \
        nginx \
        sops

COPY nginx.conf /etc/nginx/conf.d/nginx.conf
COPY nginx-http.conf /etc/nginx/http.d/default.conf

COPY run.sh /bin/run
COPY generate-ignition.sh /usr/local/bin/generate-ignition
COPY boot-request.sh /usr/local/bin/boot-request

RUN chmod +x /bin/run /usr/local/bin/generate-ignition /usr/local/bin/boot-request
ENTRYPOINT ["/bin/run"]
