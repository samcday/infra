//! etcd client construction helpers.

use anyhow::{Context, Result, anyhow, bail};
use etcd_client::{Certificate, Client, ConnectOptions, Identity, TlsOptions};
use k8s_openapi::api::core::v1::Secret;
use kube::ResourceExt;

/// Builds an authenticated etcd client from endpoint URLs and Secret data.
///
/// The auth mode is inferred from Secret keys:
/// - TLS client auth: `tls.crt`, `tls.key`, `ca.crt`
/// - Basic auth: `username`, `password`, `ca.crt`
pub async fn build_client(endpoints: &[String], secret: &Secret) -> Result<Client> {
    if endpoints.is_empty() {
        bail!("etcd endpoints are empty");
    }

    let data = secret
        .data
        .as_ref()
        .ok_or_else(|| anyhow!("secret {} has no data", secret.name_any()))?;

    let ca_cert = data
        .get("ca.crt")
        .ok_or_else(|| anyhow!("secret {} missing required key ca.crt", secret.name_any()))?;

    let tls_options = TlsOptions::new().ca_certificate(Certificate::from_pem(ca_cert.0.clone()));

    let mut connect_options = match (data.get("tls.crt"), data.get("tls.key")) {
        (Some(cert), Some(key)) => {
            let identity = Identity::from_pem(cert.0.clone(), key.0.clone());
            ConnectOptions::new().with_tls(tls_options.identity(identity))
        }
        (Some(_), None) => {
            bail!(
                "secret {} missing required key tls.key for TLS auth",
                secret.name_any()
            );
        }
        (None, Some(_)) => {
            bail!(
                "secret {} missing required key tls.crt for TLS auth",
                secret.name_any()
            );
        }
        (None, None) => {
            let username = data.get("username").ok_or_else(|| {
                anyhow!(
                    "secret {} missing required keys for auth; expected either [tls.crt, tls.key, ca.crt] or [username, password, ca.crt]",
                    secret.name_any()
                )
            })?;
            let password = data.get("password").ok_or_else(|| {
                anyhow!(
                    "secret {} missing required key password for basic auth",
                    secret.name_any()
                )
            })?;

            let username = std::str::from_utf8(&username.0)
                .context("username is not valid UTF-8")?
                .to_owned();
            let password = std::str::from_utf8(&password.0)
                .context("password is not valid UTF-8")?
                .to_owned();

            ConnectOptions::new()
                .with_tls(tls_options)
                .with_user(username, password)
        }
    };

    connect_options = connect_options
        .with_keep_alive(
            std::time::Duration::from_secs(15),
            std::time::Duration::from_secs(5),
        )
        .with_keep_alive_while_idle(true);

    Client::connect(endpoints, Some(connect_options))
        .await
        .context("failed to connect to etcd")
}
