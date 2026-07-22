FROM rust:1-alpine@sha256:3c38f3f82c2f3d73da3b38e18d279393a04cb43ddded0e35088a8c3324d40900 AS builder
RUN apk add --no-cache build-base protobuf-dev
WORKDIR /build

COPY Cargo.toml Cargo.lock ./
# Strip xtask from workspace — it's a dev tool, not shipped in the image.
RUN sed -i '/xtask/d' Cargo.toml
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/target \
    mkdir src && echo 'fn main() {}' > src/main.rs && \
    cargo build --release --locked && \
    rm -rf src target/release/.fingerprint/etcdetcetc-*

COPY src/ src/
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/target \
    cargo build --release --locked && \
    cp target/release/etcdetcetc /etcdetcetc

FROM gcr.io/distroless/static-debian12:nonroot@sha256:f5b485ea962d9bd1186b2f6b3a061191539b905b82ec395de78cbfae51f20e35
COPY --from=builder /etcdetcetc /
ENTRYPOINT ["/etcdetcetc"]
