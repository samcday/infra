//! etcd client construction helpers.

use anyhow::{Context, Result, anyhow, bail};
use etcd_client::{Certificate, Client, ConnectOptions, Identity, TlsOptions};
use k8s_openapi::api::core::v1::Secret;
use kube::ResourceExt;

const REQUEST_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(10);
const CONNECT_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(5);

#[derive(Clone, PartialEq, prost::Message)]
struct AuthStatusRequest {}

#[derive(Clone, PartialEq, prost::Message)]
struct ResponseHeader {
    #[prost(uint64, tag = "1")]
    cluster_id: u64,
    #[prost(uint64, tag = "2")]
    member_id: u64,
    #[prost(int64, tag = "3")]
    revision: i64,
    #[prost(uint64, tag = "4")]
    raft_term: u64,
}

#[derive(Clone, PartialEq, prost::Message)]
struct AuthStatusResponse {
    #[prost(message, optional, tag = "1")]
    header: Option<ResponseHeader>,
    #[prost(bool, tag = "2")]
    enabled: bool,
    #[prost(uint64, tag = "3")]
    auth_revision: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct AdminAuthStatus {
    pub cluster_id: u64,
    pub member_id: u64,
    pub revision: i64,
    pub enabled: bool,
    pub auth_revision: u64,
}

/// Builds an authenticated etcd client from endpoint URLs, Secret credentials,
/// and the independently resolved physical etcd server CA.
///
/// The admin Secret must contain a TLS client identity in `tls.crt` and
/// `tls.key`. Password-based admin authentication is deliberately rejected:
/// etcd auth mutations advance the global auth revision, while a connected
/// client's one-shot password token can become stale partway through a
/// multi-step tenant transaction.
pub async fn build_client(
    endpoints: &[String],
    secret: &Secret,
    physical_server_ca: &[u8],
) -> Result<Client> {
    if endpoints.is_empty() {
        bail!("etcd endpoints are empty");
    }

    if physical_server_ca.is_empty() {
        bail!("physical etcd server CA is empty");
    }

    let tls_options = TlsOptions::new().ca_certificate(Certificate::from_pem(physical_server_ca));

    let (cert, key) = admin_tls_material(secret)?;
    let identity = Identity::from_pem(cert, key);
    let mut connect_options = ConnectOptions::new().with_tls(tls_options.identity(identity));

    connect_options = configure_transport(connect_options);

    Client::connect(endpoints, Some(connect_options))
        .await
        .context("failed to connect to etcd")
}

fn admin_tls_material(secret: &Secret) -> Result<(&[u8], &[u8])> {
    let data = secret
        .data
        .as_ref()
        .ok_or_else(|| anyhow!("admin secret {} has no data", secret.name_any()))?;
    let cert = data.get("tls.crt").ok_or_else(|| {
        anyhow!(
            "admin secret {} missing required TLS client certificate key tls.crt; password-based admin auth is not supported",
            secret.name_any()
        )
    })?;
    let key = data.get("tls.key").ok_or_else(|| {
        anyhow!(
            "admin secret {} missing required TLS client private key tls.key",
            secret.name_any()
        )
    })?;
    if cert.0.is_empty() || key.0.is_empty() {
        bail!(
            "admin secret {} has an empty tls.crt or tls.key",
            secret.name_any()
        );
    }
    Ok((&cert.0, &key.0))
}

/// Calls etcd 3.6's non-mutating AuthStatus RPC directly. The upstream Rust
/// etcd client still ships an older Auth service protobuf, so this deliberately
/// small wire-compatible client keeps the release gate explicit and local.
pub(crate) async fn fetch_auth_status(
    endpoint: &str,
    secret: &Secret,
    physical_server_ca: &[u8],
) -> Result<AdminAuthStatus> {
    if physical_server_ca.is_empty() {
        bail!("physical etcd server CA is empty");
    }
    let (cert, key) = admin_tls_material(secret)?;
    let tls = tonic::transport::ClientTlsConfig::new()
        .ca_certificate(tonic::transport::Certificate::from_pem(physical_server_ca))
        .identity(tonic::transport::Identity::from_pem(cert, key));
    let channel = tonic::transport::Endpoint::from_shared(endpoint.to_string())
        .with_context(|| format!("invalid etcd endpoint URI {endpoint:?}"))?
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(REQUEST_TIMEOUT)
        .tls_config(tls)
        .with_context(|| format!("failed to configure TLS for etcd endpoint {endpoint}"))?
        .connect()
        .await
        .with_context(|| format!("failed to connect directly to etcd endpoint {endpoint}"))?;

    let mut grpc = tonic::client::Grpc::new(channel);
    grpc.ready()
        .await
        .with_context(|| format!("etcd AuthStatus service at {endpoint} is not ready"))?;
    let path =
        tonic::codegen::http::uri::PathAndQuery::from_static("/etcdserverpb.Auth/AuthStatus");
    let response: tonic::Response<AuthStatusResponse> = grpc
        .unary(
            tonic::Request::new(AuthStatusRequest {}),
            path,
            tonic::codec::ProstCodec::default(),
        )
        .await
        .with_context(|| format!("AuthStatus RPC failed for etcd endpoint {endpoint}"))?;
    let response = response.into_inner();
    let header = response
        .header
        .ok_or_else(|| anyhow!("AuthStatus response from {endpoint} has no header"))?;
    Ok(AdminAuthStatus {
        cluster_id: header.cluster_id,
        member_id: header.member_id,
        revision: header.revision,
        enabled: response.enabled,
        auth_revision: response.auth_revision,
    })
}

fn configure_transport(connect_options: ConnectOptions) -> ConnectOptions {
    connect_options
        .with_timeout(REQUEST_TIMEOUT)
        .with_connect_timeout(CONNECT_TIMEOUT)
        .with_keep_alive(
            std::time::Duration::from_secs(15),
            std::time::Duration::from_secs(5),
        )
        .with_keep_alive_while_idle(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use k8s_openapi::{ByteString, apimachinery::pkg::apis::meta::v1::ObjectMeta};
    use std::collections::BTreeMap;

    #[test]
    fn transport_has_bounded_connect_and_request_timeouts() {
        let configured = format!("{:?}", configure_transport(ConnectOptions::new()));
        assert!(configured.contains("timeout: Some(10s)"));
        assert!(configured.contains("connect_timeout: Some(5s)"));
    }

    #[tokio::test]
    async fn password_based_admin_auth_is_rejected_before_connect() {
        let secret = Secret {
            metadata: ObjectMeta {
                name: Some("legacy-basic-admin".to_string()),
                ..ObjectMeta::default()
            },
            data: Some(BTreeMap::from([
                ("username".to_string(), ByteString(b"root".to_vec())),
                ("password".to_string(), ByteString(b"secret".to_vec())),
            ])),
            ..Secret::default()
        };

        let error = build_client(
            &["https://etcd.invalid:2379".to_string()],
            &secret,
            b"not-empty",
        )
        .await
        .err()
        .expect("password-based admin authentication must fail before connecting");
        assert!(
            error
                .to_string()
                .contains("password-based admin auth is not supported")
        );
    }
}
