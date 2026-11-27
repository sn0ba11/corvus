FROM rust:1.88-slim-bookworm@sha256:38bc5a86d998772d4aec2348656ed21438d20fcdce2795b56ca434cf21430d89 AS builder
ARG METASEARCH2_REPO=https://github.com/mat-1/metasearch2.git
ARG METASEARCH2_COMMIT=33c0b4b330e2f0cb13161a80cd80bed9f2c3008e
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates clang cmake g++ git golang libclang-dev llvm make perl pkg-config \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --filter=blob:none "$METASEARCH2_REPO" metasearch2 \
    && cd metasearch2 \
    && git checkout --detach "$METASEARCH2_COMMIT"
COPY metasearch2/engine-selection.patch /tmp/engine-selection.patch
WORKDIR /src/metasearch2
RUN sed -i 's/\r$//' /tmp/engine-selection.patch \
    && git apply --check /tmp/engine-selection.patch \
    && git apply /tmp/engine-selection.patch
ENV RUST_BACKTRACE=1
RUN cargo build --locked --release

FROM gcr.io/distroless/cc-debian13:nonroot@sha256:29ee70762214fe8190cea04ef1c0a84a8365879e047ce8f911ae5a5ab2355fa2
WORKDIR /app
COPY --from=builder /src/metasearch2/target/release/metasearch /usr/local/bin/metasearch
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --chown=65532:65532 metasearch2/config.toml /app/config.toml
USER 65532:65532
EXPOSE 28019
ENTRYPOINT ["/usr/local/bin/metasearch", "/app/config.toml"]
