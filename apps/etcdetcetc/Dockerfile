FROM rust:1-alpine AS builder
RUN apk add --no-cache build-base protobuf-dev
WORKDIR /build

COPY Cargo.toml Cargo.lock ./
# Strip xtask from workspace — it's a dev tool, not shipped in the image.
RUN sed -i '/xtask/d' Cargo.toml
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/target \
    mkdir src && echo 'fn main() {}' > src/main.rs && \
    cargo build --release && \
    rm -rf src target/release/.fingerprint/etcdetcetc-*

COPY src/ src/
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/target \
    cargo build --release && \
    cp target/release/etcdetcetc /etcdetcetc

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=builder /etcdetcetc /
ENTRYPOINT ["/etcdetcetc"]
