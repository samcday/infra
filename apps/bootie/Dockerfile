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

FROM alpine:3.22 AS fcos

ARG FCOS_VERSION=44.20260621.3.1
ARG FCOS_ISO_SHA256=ea7cd4e81a5bdd4603865df6bbf0d617c8b69b0aef1ce890746658640f53151a
ARG FCOS_ISO_URL=https://builds.coreos.fedoraproject.org/prod/streams/stable/builds/44.20260621.3.1/x86_64/fedora-coreos-44.20260621.3.1-live-iso.x86_64.iso

RUN apk add --no-cache curl libarchive-tools mtools sbsigntool
RUN set -eux; \
    install -d /out/pxe /out/tftp/EFI/BOOT /work; \
    curl --fail --location --show-error --output /work/fcos.iso "$FCOS_ISO_URL"; \
    echo "$FCOS_ISO_SHA256  /work/fcos.iso" | sha256sum -c; \
    bsdtar -xf /work/fcos.iso -C /work \
      images/efiboot.img \
      images/pxeboot/initrd.img \
      images/pxeboot/rootfs.img \
      images/pxeboot/vmlinuz; \
    install -m 0644 /work/images/pxeboot/vmlinuz \
      "/out/pxe/fedora-coreos-$FCOS_VERSION-live-kernel.x86_64"; \
    install -m 0644 /work/images/pxeboot/initrd.img \
      "/out/pxe/fedora-coreos-$FCOS_VERSION-live-initramfs.x86_64.img"; \
    install -m 0644 /work/images/pxeboot/rootfs.img \
      "/out/pxe/fedora-coreos-$FCOS_VERSION-live-rootfs.x86_64.img"; \
    mcopy -i /work/images/efiboot.img ::/EFI/BOOT/BOOTX64.EFI \
      /out/tftp/EFI/BOOT/BOOTX64.EFI; \
    mcopy -i /work/images/efiboot.img ::/EFI/BOOT/GRUBX64.EFI \
      /out/tftp/EFI/BOOT/grubx64.efi; \
    mcopy -i /work/images/efiboot.img ::/EFI/BOOT/MMX64.EFI \
      /out/tftp/EFI/BOOT/mmx64.efi; \
    chmod 0644 /out/tftp/EFI/BOOT/*; \
    sbverify --list /out/tftp/EFI/BOOT/BOOTX64.EFI 2>&1 | \
      grep -F 'Microsoft Corporation UEFI CA 2011'; \
    sbverify --list /out/tftp/EFI/BOOT/grubx64.efi 2>&1 | \
      grep -F 'Fedora Secure Boot CA'; \
    sbverify --list "/out/pxe/fedora-coreos-$FCOS_VERSION-live-kernel.x86_64" 2>&1 | \
      grep -F 'Fedora Secure Boot CA'; \
    for asset in /out/pxe/*; do \
      sha256sum "$asset" | sed 's#  /out/pxe/#  static/#'; \
    done > /out/SHA256SUMS; \
    for asset in /out/tftp/EFI/BOOT/*; do \
      sha256sum "$asset" | sed 's#  /out/tftp/EFI/BOOT/#  tftp/#'; \
    done >> /out/SHA256SUMS; \
    find /work -mindepth 1 -delete

FROM alpine:3.22

# (Temporarily) enable testing repo to install butane.
RUN echo '@testing https://dl-cdn.alpinelinux.org/alpine/edge/testing' >> /etc/apk/repositories

RUN --mount=type=bind,from=builder,source=/home/user/packages/home,target=/pkg \
    apk add --allow-untrusted \
        /pkg/*/*.apk \
        bash \
        butane@testing \
        curl \
        dnsmasq \
        fcgiwrap \
        jq \
        kubectl \
        nginx \
        nftables \
        sops

COPY nginx.conf /etc/nginx/conf.d/nginx.conf
COPY nginx-http.conf /etc/nginx/http.d/default.conf

COPY run.sh /bin/run
COPY run-cluster.sh /bin/run-cluster
COPY generate-ignition.sh /usr/local/bin/generate-ignition
COPY generate-ignition-day2.sh /usr/local/bin/generate-ignition-day2
COPY boot-request.sh /usr/local/bin/boot-request
COPY boot-request-day2.sh /usr/local/bin/boot-request-day2
COPY serve-custom-initramfs.sh /usr/local/bin/serve-custom-initramfs
COPY cluster-grub.cfg /usr/share/bootie/grub.cfg
COPY --from=fcos /out/pxe /pxe
COPY --from=fcos /out/tftp /usr/share/bootie/tftp
COPY --from=fcos /out/SHA256SUMS /usr/share/bootie/FCOS-SHA256SUMS
COPY day2-service-live.bu /tmp/day2-service-live.bu

RUN set -eux; \
    butane --strict </tmp/day2-service-live.bu \
      >/pxe/fabric-day2-service-live.ign; \
    chmod 0644 /pxe/fabric-day2-service-live.ign; \
    sha256sum /pxe/fabric-day2-service-live.ign | \
      sed 's#  /pxe/#  static/#' >>/usr/share/bootie/FCOS-SHA256SUMS; \
    rm /tmp/day2-service-live.bu

RUN chmod +x \
        /bin/run \
        /bin/run-cluster \
        /usr/local/bin/boot-request \
        /usr/local/bin/boot-request-day2 \
        /usr/local/bin/generate-ignition \
        /usr/local/bin/generate-ignition-day2 \
        /usr/local/bin/serve-custom-initramfs
ENTRYPOINT ["/bin/run"]
